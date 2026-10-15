using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class UninstallProgram
    {
        private const string RemoveCommand = "/remove";
        private const string SilentFlag = "/silent";
        private const string ParentPrefix = "/parent=";
        private const string EventPrefix = "/event=";
        private const int StepsWaitMs = 180 * 1000;
        private const int ParentExitWaitMs = 30 * 1000;
        private const int ExitOk = 0;
        private const int ExitCancelled = 1;
        private const int ExitIncomplete = 2;
        private const string Title = "Удаление " + AppIdentity.Name;

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            args = CommandLine.Normalize(args);
            bool silent = Array.IndexOf(args, SilentFlag) >= 0;
            if (args.Length >= 2 && args[0] == RemoveCommand) return Remove(args[1], silent, Value(args, ParentPrefix), Value(args, EventPrefix));
            return Confirm(Path.GetDirectoryName(Application.ExecutablePath), silent);
        }

        private static string Value(string[] args, string prefix)
        {
            foreach (string arg in args)
            {
                if (arg.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return arg.Substring(prefix.Length);
            }
            return null;
        }

        private static int Confirm(string installDirectory, bool silent)
        {
            if (!silent && Ask() != DialogResult.Yes) return ExitCancelled;
            string worker = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "Uninstall-" + Guid.NewGuid().ToString("N") + ".exe");
            File.Copy(Application.ExecutablePath, worker, true);
            string eventName = AppIdentity.Name + ".UninstallSteps." + Guid.NewGuid().ToString("N");
            using (var stepsDone = new EventWaitHandle(false, EventResetMode.ManualReset, eventName))
            {
                string arguments = RemoveCommand + " \"" + installDirectory + "\" " + ParentPrefix + Process.GetCurrentProcess().Id
                    + " " + EventPrefix + eventName + (silent ? " " + SilentFlag : "");
                Process.Start(new ProcessStartInfo(worker, arguments) { UseShellExecute = false }).Dispose();
                if (!silent) return ExitOk;
                if (!stepsDone.WaitOne(StepsWaitMs)) return ExitIncomplete;
                return UninstallLog.StepsSucceeded() ? ExitOk : ExitIncomplete;
            }
        }

        private static DialogResult Ask()
        {
            return MessageBox.Show(
                "Удалить " + AppIdentity.Name + "?" + Environment.NewLine + Environment.NewLine
                + "Удалятся программа, её настройки и данные, автозапуск и запись в «Приложениях». "
                + UninstallProfile.ConfirmDetails,
                Title, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
        }

        private static int Remove(string installDirectory, bool silent, string parentPid, string eventName)
        {
            var problems = new List<string>();
            UninstallLog.Start(installDirectory);
            if (!RunningApp.Stop()) problems.Add(AppIdentity.Name + " не закрылся");
            UninstallProfile.Remove(installDirectory, problems);
            string registered = UninstallRegistration.InstalledLocation();
            bool ownsRegistration = registered == null || string.Equals(Path.GetFullPath(registered).TrimEnd('\\'),
                Path.GetFullPath(installDirectory).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
            UninstallLog.Append("registered location: " + (registered ?? "none") + ", owns registration: " + ownsRegistration);
            if (ownsRegistration)
            {
                string autostartProblem = Autostart.Disable();
                if (autostartProblem != null) problems.Add(autostartProblem);
                UninstallRegistration.Unregister();
                string dataProblem = FolderRemoval.Delete(AppIdentity.DataDirectory, null);
                if (dataProblem != null) problems.Add("папка данных удалена не полностью (" + dataProblem + ")");
            }
            UninstallLog.Steps(problems);
            SignalSteps(eventName);
            WaitForParent(parentPid);
            if (File.Exists(Path.Combine(installDirectory, AppIdentity.ExecutableName)) || File.Exists(Path.Combine(installDirectory, AppIdentity.UninstallerName)))
            {
                string folderProblem = FolderRemoval.Delete(installDirectory, null);
                UninstallLog.Append("install folder: " + (folderProblem ?? "removed"));
                if (folderProblem != null) problems.Add("папка программы удалена не полностью (" + folderProblem + ")");
            }
            if (!silent && problems.Count == 0)
            {
                MessageBox.Show(AppIdentity.Name + " удалён.", Title, MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            else if (!silent)
            {
                MessageBox.Show("Удаление закончено не полностью:" + Environment.NewLine + "• " + string.Join(Environment.NewLine + "• ", problems),
                    Title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            ScheduleSelfDelete();
            return problems.Count == 0 ? ExitOk : ExitIncomplete;
        }

        private static void SignalSteps(string eventName)
        {
            if (eventName == null) return;
            EventWaitHandle stepsDone;
            if (EventWaitHandle.TryOpenExisting(eventName, out stepsDone))
            {
                using (stepsDone) stepsDone.Set();
            }
        }

        private static void WaitForParent(string parentPid)
        {
            int pid;
            if (parentPid == null || !int.TryParse(parentPid, NumberStyles.Integer, CultureInfo.InvariantCulture, out pid)) return;
            try
            {
                using (Process parent = Process.GetProcessById(pid))
                {
                    parent.WaitForExit(ParentExitWaitMs);
                }
            }
            catch (ArgumentException)
            {
            }
            catch (InvalidOperationException)
            {
            }
        }

        private static void ScheduleSelfDelete()
        {
            Process.Start(new ProcessStartInfo("cmd.exe", "/c ping -n 3 127.0.0.1 >nul & del /f /q \"" + Application.ExecutablePath + "\"")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden
            });
        }
    }
}
