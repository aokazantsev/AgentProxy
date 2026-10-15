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
            bool silent = HasFlag(args, SilentFlag);
            if (args.Length >= 2 && args[0] == RemoveCommand) return Remove(args[1], silent, Value(args, ParentPrefix), Value(args, EventPrefix));
            return Confirm(Path.GetDirectoryName(Application.ExecutablePath), silent);
        }

        private static bool HasFlag(string[] args, string flag)
        {
            foreach (string arg in args)
            {
                if (string.Equals(arg, flag, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
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
            try
            {
                return StartWorker(installDirectory, silent);
            }
            catch (Exception error)
            {
                UninstallLog.Start(installDirectory);
                UninstallLog.Append("uninstall could not start: " + error);
                if (!silent) MessageBox.Show("Удаление не запустилось: " + error.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return ExitIncomplete;
            }
        }

        private static int StartWorker(string installDirectory, bool silent)
        {
            string worker = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "Uninstall-" + Guid.NewGuid().ToString("N") + ".exe");
            File.Copy(Application.ExecutablePath, worker, true);
            string eventName = AppIdentity.Name + ".UninstallSteps." + Guid.NewGuid().ToString("N");
            using (var stepsDone = new EventWaitHandle(false, EventResetMode.ManualReset, eventName))
            {
                string arguments = RemoveCommand + " \"" + installDirectory + "\" " + ParentPrefix + Process.GetCurrentProcess().Id
                    + " " + EventPrefix + eventName + (silent ? " " + SilentFlag : "");
                Process.Start(new ProcessStartInfo(worker, arguments) { UseShellExecute = false, WorkingDirectory = Path.GetTempPath() }).Dispose();
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
            Directory.SetCurrentDirectory(Path.GetTempPath());
            var problems = new List<string>();
            UninstallLog.Start(installDirectory);
            string registered = UninstallRegistration.InstalledLocation();
            bool ownsRegistration = registered == null || string.Equals(Path.GetFullPath(registered).TrimEnd('\\'),
                Path.GetFullPath(installDirectory).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
            UninstallLog.Append("registered location: " + (registered ?? "none") + ", owns registration: " + ownsRegistration);
            if (!ownsRegistration)
            {
                if (!EngineProcesses.StopAll(installDirectory)) problems.Add("движок прежней установки не закрылся");
            }
            else
            {
                if (!RunningApp.Stop()) problems.Add(AppIdentity.Name + " не закрылся");
                UninstallProfile.Remove(installDirectory, problems);
                string autostartProblem = Autostart.Disable();
                if (autostartProblem != null) problems.Add(autostartProblem);
                UninstallRegistration.Unregister();
                string dataProblem = FolderRemoval.Delete(AppIdentity.DataDirectory, null);
                if (dataProblem != null) problems.Add("папка данных удалена не полностью (" + dataProblem + ")");
            }
            if (LooksInstalled(installDirectory))
            {
                string contentProblem = FolderRemoval.DeleteContentsExcept(installDirectory, AppIdentity.UninstallerName);
                if (contentProblem != null) problems.Add("файлы программы удалены не полностью (" + contentProblem + ")");
            }
            else
            {
                UninstallLog.Append("install folder skipped: no " + AppIdentity.ExecutableName + " in it");
            }
            UninstallLog.Steps(problems);
            SignalSteps(eventName);
            WaitForParent(parentPid);
            if (IsOurFolder(installDirectory))
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

        private static bool LooksInstalled(string directory)
        {
            return File.Exists(Path.Combine(directory, AppIdentity.ExecutableName))
                || (File.Exists(Path.Combine(directory, AppIdentity.UninstallerName)) && Directory.Exists(Path.Combine(directory, "runtime")));
        }

        private static bool IsOurFolder(string directory)
        {
            if (!Directory.Exists(directory)) return false;
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                if (!string.Equals(Path.GetFileName(entry), AppIdentity.UninstallerName, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
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
