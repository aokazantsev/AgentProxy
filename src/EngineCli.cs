using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;

namespace AgentProxy
{
    internal static class EngineCli
    {
        private const int TimeoutMs = 180 * 1000;

        public static string PythonPath(string installDirectory)
        {
            return Path.Combine(installDirectory, "runtime", "python", "python.exe");
        }

        public static ProcessStartInfo StartInfo(string installDirectory, string arguments)
        {
            return new ProcessStartInfo(PythonPath(installDirectory), "-I -X utf8 " + arguments)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = installDirectory
            };
        }

        public static EngineCliResult Run(string installDirectory, string cliArguments)
        {
            var result = new EngineCliResult();
            if (!File.Exists(PythonPath(installDirectory)))
            {
                result.Problem = "нет " + PythonPath(installDirectory);
                return result;
            }
            try
            {
                using (Process process = Process.Start(StartInfo(installDirectory, "-m agentproxy.cli " + cliArguments)))
                {
                    var output = new StringBuilder();
                    var error = new StringBuilder();
                    process.OutputDataReceived += (sender, e) => { if (e.Data != null) lock (output) output.AppendLine(e.Data); };
                    process.ErrorDataReceived += (sender, e) => { if (e.Data != null) lock (error) error.AppendLine(e.Data); };
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    if (!process.WaitForExit(TimeoutMs))
                    {
                        Kill(process);
                        result.Problem = "agentproxy " + cliArguments + " не завершился за " + TimeoutMs / 1000 + " с";
                        return result;
                    }
                    process.WaitForExit();
                    result.ExitCode = process.ExitCode;
                    lock (output) result.Output = output.ToString();
                    lock (error) result.Errors = error.ToString();
                }
            }
            catch (Win32Exception failure)
            {
                result.Problem = failure.Message;
            }
            catch (IOException failure)
            {
                result.Problem = failure.Message;
            }
            return result;
        }

        private static void Kill(Process process)
        {
            try
            {
                using (Process tree = Process.Start(new ProcessStartInfo("taskkill.exe", "/T /F /PID " + process.Id)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true
                }))
                {
                    tree.WaitForExit(10000);
                }
                if (!process.HasExited) process.Kill();
            }
            catch (InvalidOperationException)
            {
            }
            catch (Win32Exception)
            {
            }
        }
    }
}
