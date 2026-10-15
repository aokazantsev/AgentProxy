using System.Diagnostics;
using System.Drawing;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class JournalForm : Form
    {
        private const int RefreshMs = 1000;
        private const int TailBytes = 256 * 1024;

        private static JournalForm openForm;

        private readonly TabControl tabs = new TabControl { Dock = DockStyle.Fill };
        private readonly Timer timer = new Timer { Interval = RefreshMs };

        public static void ShowSingle()
        {
            if (openForm != null)
            {
                if (openForm.WindowState == FormWindowState.Minimized) openForm.WindowState = FormWindowState.Normal;
                openForm.Activate();
                return;
            }
            openForm = new JournalForm();
            openForm.FormClosed += (sender, e) => openForm = null;
            openForm.Show();
        }

        private JournalForm()
        {
            Text = "Журнал " + AppIdentity.Name;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = SystemFonts.MessageBoxFont;
            Size = new Size(980, 620);
            MinimumSize = new Size(560, 320);
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

            AddTab("Программа", AppLog.FilePath);
            AddTab("Движок", EngineLogTail.FilePath);
            AddTab("Командная строка", UserDataPaths.File("cli.log"));

            var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(6) };
            var close = new Button { Text = "Закрыть", AutoSize = true };
            close.Click += (sender, e) => Close();
            var copy = new Button { Text = "Скопировать вкладку", AutoSize = true };
            copy.Click += (sender, e) => CopyCurrent();
            var folder = new Button { Text = "Открыть папку журналов", AutoSize = true };
            folder.Click += (sender, e) => Process.Start(new ProcessStartInfo(SystemTools.Explorer, "\"" + UserDataPaths.Directory + "\"") { UseShellExecute = false });
            buttons.Controls.Add(close);
            buttons.Controls.Add(copy);
            buttons.Controls.Add(folder);
            Controls.Add(tabs);
            Controls.Add(buttons);
            CancelButton = close;

            timer.Tick += (sender, e) => RefreshAll(false);
            Shown += (sender, e) =>
            {
                RefreshAll(true);
                timer.Start();
            };
            FormClosed += (sender, e) => timer.Dispose();
        }

        private void AddTab(string title, string path)
        {
            var box = new TextBox
            {
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Both,
                WordWrap = false,
                Dock = DockStyle.Fill,
                Font = new Font("Consolas", 9f),
                BackColor = SystemColors.Window
            };
            var page = new TabPage(title) { Tag = new JournalSource(path, box) };
            page.Controls.Add(box);
            tabs.TabPages.Add(page);
        }

        private void RefreshAll(bool force)
        {
            foreach (TabPage page in tabs.TabPages) ((JournalSource)page.Tag).Refresh(force, TailBytes);
        }

        private void CopyCurrent()
        {
            var source = (JournalSource)tabs.SelectedTab.Tag;
            if (source.Box.TextLength > 0) Clipboard.SetText(source.Box.Text);
        }
    }
}
