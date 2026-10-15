using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Timer = System.Windows.Forms.Timer;

namespace AgentProxy
{
    internal sealed class EngineSupervisor : IDisposable
    {
        private const int HealthIntervalMs = 5000;
        private const int StopWaitMs = 6000;
        private const int StderrTailChars = 4000;
        private static readonly TimeSpan WarmUp = TimeSpan.FromSeconds(30);

        private readonly string installDirectory;
        private readonly SynchronizationContext ui;
        private readonly Timer healthTimer = new Timer { Interval = HealthIntervalMs };
        private readonly Timer restartTimer = new Timer();
        private readonly RestartPolicy policy = new RestartPolicy();
        private readonly StringBuilder stderrTail = new StringBuilder();
        private Process process;
        private DateTime startedAt;
        private long logOffset;
        private bool stopping;
        private bool disposed;
        private int healthInFlight;
        private int crashReported;

        public EngineState State { get; private set; }
        public string Detail { get; private set; }
        public EngineStatus LastStatus { get; private set; }
        public EngineSettings Settings { get; private set; }

        public event EventHandler Changed;

        public EngineSupervisor(string installDirectory)
        {
            this.installDirectory = installDirectory;
            ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
            healthTimer.Tick += (sender, e) => CheckHealth();
            restartTimer.Tick += (sender, e) =>
            {
                restartTimer.Stop();
                Launch();
            };
            State = EngineState.Stopped;
        }

        public void Start()
        {
            stopping = false;
            restartTimer.Stop();
            policy.Reset();
            Launch();
            healthTimer.Start();
        }

        public void Restart()
        {
            AppLog.Append("engine restart requested");
            StopProcess();
            Start();
        }

        public void Stop()
        {
            stopping = true;
            healthTimer.Stop();
            restartTimer.Stop();
            StopProcess();
            SetState(EngineState.Stopped, null);
        }

        private void Launch()
        {
            if (disposed || stopping || process != null) return;
            Settings = EngineSettings.Load();
            string python = EngineCli.PythonPath(installDirectory);
            if (!File.Exists(python))
            {
                SetState(EngineState.Crashed, "нет движка: " + python);
                AppLog.Append("engine missing: " + python);
                return;
            }
            ProcessStartInfo info = EngineStartInfo(installDirectory, Process.GetCurrentProcess().Id);
            lock (stderrTail) stderrTail.Length = 0;
            logOffset = EngineLogTail.Length();
            try
            {
                process = new Process { StartInfo = info, EnableRaisingEvents = true };
                process.ErrorDataReceived += OnStderr;
                process.Exited += OnExited;
                process.Start();
                process.BeginErrorReadLine();
            }
            catch (Win32Exception error)
            {
                AppLog.Append("engine start failed: " + error.Message);
                process = null;
                startedAt = DateTime.UtcNow;
                SetState(EngineState.Crashed, error.Message);
                ScheduleRestart(-1);
                return;
            }
            startedAt = DateTime.UtcNow;
            AppLog.Append("engine started: pid " + process.Id + ", proxy port " + Settings.ProxyPort + ", api port " + Settings.ApiPort);
            SetState(EngineState.Starting, null);
        }

        public static ProcessStartInfo EngineStartInfo(string installDirectory, int parentPid)
        {
            ProcessStartInfo info = EngineCli.StartInfo(installDirectory, "-m agentproxy --parent-pid " + parentPid);
            info.RedirectStandardOutput = false;
            info.StandardOutputEncoding = null;
            return info;
        }

        private void OnStderr(object sender, DataReceivedEventArgs e)
        {
            if (e.Data == null) return;
            lock (stderrTail)
            {
                stderrTail.AppendLine(e.Data);
                if (stderrTail.Length > StderrTailChars) stderrTail.Remove(0, stderrTail.Length - StderrTailChars);
            }
        }

        private void OnExited(object sender, EventArgs e)
        {
            var exited = (Process)sender;
            int code;
            try
            {
                code = exited.ExitCode;
            }
            catch (InvalidOperationException)
            {
                code = -1;
            }
            ui.Post(state => HandleExit(exited, code), null);
        }

        private void HandleExit(Process exited, int code)
        {
            if (!ReferenceEquals(exited, process)) return;
            process = null;
            exited.Dispose();
            TimeSpan ranFor = DateTime.UtcNow - startedAt;
            string stderr;
            lock (stderrTail) stderr = stderrTail.ToString().Trim();
            AppLog.Append("engine exited: code " + code + " after " + (int)ranFor.TotalSeconds + " s" + (stderr.Length > 0 ? ", stderr: " + stderr : ""));
            if (stopping || disposed) return;
            EngineState next = RestartPolicy.StateAfterExit(code);
            if (next == EngineState.PortBusy)
            {
                SetState(next, "порт " + Settings.ProxyPort + " или " + Settings.ApiPort + " занят другой программой");
            }
            else
            {
                string crash = EngineLogTail.LastCrashSince(logOffset);
                SetState(next, code == RestartPolicy.ExitDataDir ? "движок не может писать в " + UserDataPaths.Directory : "движок упал (код " + code + ")");
                ReportCrash(crash ?? ("exit code " + code + (stderr.Length > 0 ? Environment.NewLine + stderr : "")));
            }
            ScheduleRestart(code);
        }

        private void ScheduleRestart(int code)
        {
            TimeSpan delay = policy.NextDelay(code, DateTime.UtcNow - startedAt);
            AppLog.Append("engine restart in " + (int)delay.TotalSeconds + " s (failures " + policy.Failures + ")");
            restartTimer.Interval = (int)delay.TotalMilliseconds;
            restartTimer.Start();
        }

        private void ReportCrash(string error)
        {
            if (!CrashReportConsent.IsGiven || Interlocked.Exchange(ref crashReported, 1) != 0) return;
            ThreadPool.QueueUserWorkItem(state =>
            {
                string problem = CrashUploader.Send("engine", error, EngineLogTail.FilePath);
                AppLog.Append(problem == null ? "отчёт о сбое движка отправлен" : "отчёт о сбое движка не отправлен: " + problem);
            });
        }

        private void CheckHealth()
        {
            if (process == null || Interlocked.Exchange(ref healthInFlight, 1) != 0) return;
            int port = Settings.ApiPort;
            ThreadPool.QueueUserWorkItem(state =>
            {
                EngineStatus status = EngineStatus.Fetch(port);
                ui.Post(ignored =>
                {
                    Interlocked.Exchange(ref healthInFlight, 0);
                    ApplyHealth(status);
                }, null);
            });
        }

        private void ApplyHealth(EngineStatus status)
        {
            if (process == null || disposed) return;
            LastStatus = status;
            bool warmingUp = DateTime.UtcNow - startedAt < WarmUp;
            if (status != null && status.ProxyRunning) SetState(EngineState.Running, null);
            else if (warmingUp) SetState(EngineState.Starting, null);
            else if (status == null) SetState(EngineState.NotResponding, "API не отвечает на порту " + Settings.ApiPort);
            else SetState(EngineState.ProxyNotListening, "прокси не слушает порт " + Settings.ProxyPort);
        }

        private void StopProcess()
        {
            Process current = process;
            process = null;
            if (current == null) return;
            try
            {
                if (!current.HasExited)
                {
                    EngineRuntimeInfo.RequestShutdown(Settings.ApiPort, EngineRuntimeInfo.FilePath);
                    if (!current.WaitForExit(StopWaitMs))
                    {
                        AppLog.Append("engine did not stop in time, killing");
                        current.Kill();
                        current.WaitForExit(StopWaitMs);
                    }
                }
            }
            catch (InvalidOperationException)
            {
            }
            catch (Win32Exception error)
            {
                AppLog.Append("engine stop failed: " + error.Message);
            }
            finally
            {
                current.Dispose();
            }
        }

        private void SetState(EngineState state, string detail)
        {
            bool changed = State != state || Detail != detail;
            if (changed && state != EngineState.Starting) AppLog.Append("engine state: " + state + (detail != null ? " (" + detail + ")" : ""));
            State = state;
            Detail = detail;
            EventHandler handler = Changed;
            if (handler != null) handler(this, EventArgs.Empty);
        }

        public void Dispose()
        {
            if (disposed) return;
            Stop();
            disposed = true;
            healthTimer.Dispose();
            restartTimer.Dispose();
        }
    }
}
