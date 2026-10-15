using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class UninstallProgram
    {
        private const string RemoveCommand = "/remove";
        private const string SilentFlag = "/silent";
        private const int DeleteAttempts = 20;
        private const int DeleteRetryMs = 500;
        private const string Title = "Удаление " + AppIdentity.Name;

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            args = CommandLine.Normalize(args);
            bool silent = Array.IndexOf(args, SilentFlag) >= 0;
            if (args.Length >= 2 && args[0] == RemoveCommand) return Remove(args[1], silent);
            return Confirm(Path.GetDirectoryName(Application.ExecutablePath), silent);
        }

        private static int Confirm(string installDirectory, bool silent)
        {
            if (!silent && Ask() != DialogResult.Yes) return 1;
            string worker = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "Uninstall-" + Guid.NewGuid().ToString("N") + ".exe");
            File.Copy(Application.ExecutablePath, worker, true);
            string arguments = RemoveCommand + " \"" + installDirectory + "\"" + (silent ? " " + SilentFlag : "");
            using (Process started = Process.Start(new ProcessStartInfo(worker, arguments) { UseShellExecute = false }))
            {
                if (!silent) return 0;
                started.WaitForExit();
                return started.ExitCode;
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

        private static int Remove(string installDirectory, bool silent)
        {
            var problems = new List<string>();
            if (!RunningApp.Stop()) problems.Add(AppIdentity.Name + " не закрылся");
            UninstallProfile.Remove(problems);
            string autostartProblem = Autostart.Disable();
            if (autostartProblem != null) problems.Add(autostartProblem);
            UninstallRegistration.Unregister();
            string dataProblem = DeleteWithRetries(AppIdentity.DataDirectory);
            if (dataProblem != null) problems.Add("папка данных " + dataProblem);
            if (File.Exists(Path.Combine(installDirectory, AppIdentity.ExecutableName)))
            {
                string folderProblem = DeleteWithRetries(installDirectory);
                if (folderProblem != null) problems.Add("папка программы " + folderProblem);
            }
            UninstallLog.Write(problems);
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
            return problems.Count == 0 ? 0 : 2;
        }

        private static string DeleteWithRetries(string directory)
        {
            if (!Directory.Exists(directory)) return null;
            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    Directory.Delete(directory, true);
                    return null;
                }
                catch (IOException error)
                {
                    if (attempt >= DeleteAttempts) return "удалена не полностью (" + error.Message + ")";
                }
                catch (UnauthorizedAccessException error)
                {
                    if (attempt >= DeleteAttempts) return "удалена не полностью (" + error.Message + ")";
                }
                Thread.Sleep(DeleteRetryMs);
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
