using System;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class AboutForm : Form
    {
        private const string GitHubUrl = AppIdentity.GitHubUrl;
        private const string SiteUrl = AppIdentity.SiteUrl;
        private const string ChangelogUrl = GitHubUrl + "/blob/main/CHANGELOG.md";
        private const string ReleasesUrl = GitHubUrl + "/releases/latest";

        private static AboutForm openForm;

        public static void ShowSingle()
        {
            if (openForm != null)
            {
                openForm.Activate();
                return;
            }
            openForm = new AboutForm();
            openForm.FormClosed += (sender, e) => openForm = null;
            openForm.Show();
        }

        private AboutForm()
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            string product = Attribute<AssemblyProductAttribute>(assembly, a => a.Product, AppIdentity.Name);
            string version = Attribute<AssemblyInformationalVersionAttribute>(assembly, a => a.InformationalVersion, assembly.GetName().Version.ToString());
            string description = Attribute<AssemblyDescriptionAttribute>(assembly, a => a.Description, "");
            string copyright = Attribute<AssemblyCopyrightAttribute>(assembly, a => a.Copyright, "");

            Text = "О приложении " + product;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = SystemFonts.MessageBoxFont;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Icon appIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            Icon = appIcon;

            var layout = new TableLayoutPanel { AutoSize = true, ColumnCount = 2, Padding = new Padding(16), Dock = DockStyle.Fill };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            Controls.Add(layout);

            var picture = new PictureBox
            {
                Image = new Icon(appIcon, 48, 48).ToBitmap(),
                SizeMode = PictureBoxSizeMode.Zoom,
                Size = new Size(48, 48),
                Margin = new Padding(0, 0, 16, 0)
            };
            layout.Controls.Add(picture, 0, 0);
            layout.SetRowSpan(picture, 8);

            var title = new Label { Text = product, AutoSize = true, Font = new Font(Font.FontFamily, Font.Size * 1.5f, FontStyle.Bold) };
            layout.Controls.Add(title, 1, 0);
            layout.Controls.Add(new Label { Text = "Версия " + version, AutoSize = true, Margin = new Padding(3, 2, 3, 8) }, 1, 1);
            layout.Controls.Add(new Label { Text = description, AutoSize = true, MaximumSize = new Size(360, 0), Margin = new Padding(3, 0, 3, 12) }, 1, 2);
            layout.Controls.Add(Link("Страница на сайте", SiteUrl), 1, 3);
            layout.Controls.Add(Link("Исходники на GitHub", GitHubUrl), 1, 4);
            layout.Controls.Add(Link("История версий", ChangelogUrl), 1, 5);
            layout.Controls.Add(Link("Последняя версия", ReleasesUrl), 1, 6);
            layout.Controls.Add(new Label { Text = copyright, AutoSize = true, ForeColor = SystemColors.GrayText, Margin = new Padding(3, 12, 3, 12) }, 1, 7);

            var close = new Button { Text = "Закрыть", AutoSize = true, Anchor = AnchorStyles.Right, DialogResult = DialogResult.OK };
            close.Click += (sender, e) => Close();
            layout.Controls.Add(close, 1, 8);
            AcceptButton = close;
            CancelButton = close;
        }

        private static LinkLabel Link(string text, string url)
        {
            var link = new LinkLabel { Text = text, AutoSize = true, Margin = new Padding(3, 2, 3, 2) };
            link.LinkClicked += (sender, e) =>
            {
                try
                {
                    Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
                }
                catch (System.ComponentModel.Win32Exception)
                {
                    MessageBox.Show("Не удалось открыть " + url + " — открой адрес в браузере вручную.", AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            };
            return link;
        }

        private static string Attribute<T>(Assembly assembly, Func<T, string> read, string fallback) where T : Attribute
        {
            object[] found = assembly.GetCustomAttributes(typeof(T), false);
            if (found.Length == 0) return fallback;
            string value = read((T)found[0]);
            return string.IsNullOrEmpty(value) ? fallback : value;
        }
    }
}
