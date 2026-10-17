using System;
using System.Diagnostics;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class UpdateForm : Form
    {
        private const int ContentWidth = 420;

        private static UpdateForm openForm;

        private readonly Label status = new Label();
        private readonly TextBox pathBox = new TextBox();
        private readonly FlowLayoutPanel fileButtons = new FlowLayoutPanel();
        private readonly Button installNow = new Button();
        private string downloadedPath;
        private UpdateRelease downloadedRelease;

        public static void ShowSingle()
        {
            if (openForm != null)
            {
                openForm.Activate();
                return;
            }
            openForm = new UpdateForm();
            openForm.FormClosed += (sender, e) => openForm = null;
            openForm.Show();
            openForm.Start();
        }

        private UpdateForm()
        {
            Text = "Обновление " + AppIdentity.Name;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = SystemFonts.MessageBoxFont;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

            var layout = new TableLayoutPanel { AutoSize = true, ColumnCount = 1, Padding = new Padding(16) };
            Controls.Add(layout);

            status.AutoSize = true;
            status.MaximumSize = new Size(ContentWidth, 0);
            status.Text = "Проверяю, есть ли версия новее " + AppIdentity.Version + "…";
            layout.Controls.Add(status);

            pathBox.ReadOnly = true;
            pathBox.Width = ContentWidth;
            pathBox.Margin = new Padding(3, 10, 3, 3);
            pathBox.Visible = false;
            layout.Controls.Add(pathBox);

            fileButtons.AutoSize = true;
            fileButtons.Visible = false;
            fileButtons.Margin = new Padding(0, 6, 0, 0);
            installNow.Text = "Установить сейчас";
            installNow.AutoSize = true;
            installNow.Click += (sender, e) => InstallNow();
            fileButtons.Controls.Add(installNow);
            var openFolder = new Button { Text = "Открыть папку", AutoSize = true };
            openFolder.Click += (sender, e) => ShowInExplorer(downloadedPath);
            var copyPath = new Button { Text = "Скопировать путь", AutoSize = true };
            copyPath.Click += (sender, e) => CopyPath();
            fileButtons.Controls.Add(openFolder);
            fileButtons.Controls.Add(copyPath);
            layout.Controls.Add(fileButtons);

            var close = new Button { Text = "Закрыть", AutoSize = true, Anchor = AnchorStyles.Right, Margin = new Padding(3, 12, 3, 0) };
            close.Click += (sender, e) => Close();
            layout.Controls.Add(close);
            CancelButton = close;
        }

        private void Start()
        {
            var worker = new Thread(Check) { IsBackground = true };
            worker.Start();
        }

        private void Check()
        {
            try
            {
                UpdateRelease release = UpdateChecker.FetchLatest();
                Version current = UpdateChecker.CurrentVersion;
                AppLog.Append("update check: installed " + AppIdentity.Version + ", latest " + release.VersionText);
                if (release.Version <= current)
                {
                    Report("Установлена последняя версия — " + AppIdentity.Version + ".", null);
                    return;
                }
                Report("Есть версия " + release.VersionText + ", скачиваю установщик (" + (release.Size / 1024 / 1024.0).ToString("0.0") + " МБ)…", null);
                string path = UpdateChecker.Download(release);
                downloadedRelease = release;
                AppLog.Append("update downloaded: " + path + (release.Sha256 != null ? ", sha256 verified" : ", no sha256 in release"));
                Report("Скачана версия " + release.VersionText
                    + (release.Sha256 != null ? ", контрольная сумма сверена с GitHub" : ", в релизе нет контрольной суммы — файл не сверен")
                    + ". «Установить сейчас» запустит установщик: он поставит её поверх текущей и запустит программу заново, "
                    + AppIdentity.UpdateKeeps + ".", path);
            }
            catch (Exception error)
            {
                AppLog.Append("update check failed: " + error);
                Report("Не удалось проверить обновление: " + error.Message
                    + Environment.NewLine + "Последняя версия всегда есть на " + AppIdentity.GitHubUrl + "/releases/latest", null);
            }
        }

        private void Report(string text, string path)
        {
            try
            {
                BeginInvoke(new Action(() => ShowResult(text, path)));
            }
            catch (InvalidOperationException)
            {
            }
        }

        private void ShowResult(string text, string path)
        {
            status.Text = text;
            if (path == null) return;
            downloadedPath = path;
            pathBox.Text = path;
            pathBox.Visible = true;
            fileButtons.Visible = true;
        }

        public static string InstallStartedText(string setupArguments)
        {
            return setupArguments.Length > 0
                ? "Устанавливаю… Программа закроется и запустится заново."
                : "Установщик запущен: нажми в нём «Установить». Он закроет программу и запустит новую версию.";
        }

        private void InstallNow()
        {
            installNow.Enabled = false;
            try
            {
                UpdateChecker.Verify(downloadedPath, downloadedRelease);
                AppLog.Append("update install started: " + downloadedPath);
                Process setup = Process.Start(new ProcessStartInfo(downloadedPath, AppIdentity.UpdateSetupArguments) { UseShellExecute = true });
                if (setup != null)
                {
                    setup.EnableRaisingEvents = true;
                    setup.Exited += (sender, e) => SetupExited(setup);
                }
                status.Text = InstallStartedText(AppIdentity.UpdateSetupArguments);
            }
            catch (System.IO.IOException error)
            {
                AppLog.Append("update install refused: " + error.Message);
                status.Text = "Установщик не запущен: " + error.Message + ". Закрой это окно и проверь обновление заново.";
            }
            catch (System.ComponentModel.Win32Exception error)
            {
                AppLog.Append("update install failed: " + error.Message);
                status.Text = "Не удалось запустить установщик: " + error.Message;
                installNow.Enabled = true;
            }
        }

        private void SetupExited(Process setup)
        {
            int code = setup.ExitCode;
            setup.Dispose();
            AppLog.Append("update installer exited without restarting the program, code " + code);
            try
            {
                BeginInvoke(new Action(() =>
                {
                    status.Text = "Установщик закрылся, обновление не установлено (код " + code + "). Можно запустить его снова.";
                    installNow.Enabled = true;
                }));
            }
            catch (InvalidOperationException)
            {
            }
        }

        private void CopyPath()
        {
            try
            {
                Clipboard.SetText(downloadedPath);
            }
            catch (System.Runtime.InteropServices.ExternalException error)
            {
                AppLog.Append("clipboard failed: " + error.Message);
            }
        }

        private static void ShowInExplorer(string path)
        {
            try
            {
                Process.Start(new ProcessStartInfo(SystemTools.Explorer, "/select,\"" + path + "\"") { UseShellExecute = false });
            }
            catch (System.ComponentModel.Win32Exception error)
            {
                AppLog.Append("explorer failed: " + error.Message);
            }
        }
    }
}
