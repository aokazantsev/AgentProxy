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
        private const string SilentSetupFlag = "/silent";

        private static UpdateForm openForm;

        private readonly Label status = new Label();
        private readonly TextBox pathBox = new TextBox();
        private readonly FlowLayoutPanel fileButtons = new FlowLayoutPanel();
        private string downloadedPath;

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
            var installNow = new Button { Text = "Установить сейчас", AutoSize = true };
            installNow.Click += (sender, e) => InstallNow();
            fileButtons.Controls.Add(installNow);
            var openFolder = new Button { Text = "Открыть папку", AutoSize = true };
            openFolder.Click += (sender, e) => ShowInExplorer(downloadedPath);
            var copyPath = new Button { Text = "Скопировать путь", AutoSize = true };
            copyPath.Click += (sender, e) => Clipboard.SetText(downloadedPath);
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
                AppLog.Append("update downloaded: " + path);
                Report("Скачана версия " + release.VersionText + ", контрольная сумма сверена с GitHub. «Установить сейчас» поставит её "
                    + "поверх текущей: программа перезапустится, настройки, история запросов и сертификат сохранятся.", path);
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

        private void InstallNow()
        {
            try
            {
                AppLog.Append("update install started: " + downloadedPath);
                Process.Start(new ProcessStartInfo(downloadedPath, SilentSetupFlag) { UseShellExecute = false });
                status.Text = "Устанавливаю… Программа закроется и запустится заново.";
            }
            catch (System.ComponentModel.Win32Exception error)
            {
                AppLog.Append("update install failed: " + error.Message);
                status.Text = "Не удалось запустить установщик: " + error.Message;
            }
        }

        private static void ShowInExplorer(string path)
        {
            try
            {
                Process.Start(new ProcessStartInfo("explorer.exe", "/select,\"" + path + "\"") { UseShellExecute = false });
            }
            catch (System.ComponentModel.Win32Exception error)
            {
                AppLog.Append("explorer failed: " + error.Message);
            }
        }
    }
}
