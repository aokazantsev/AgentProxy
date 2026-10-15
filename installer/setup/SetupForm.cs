using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class SetupForm : Form
    {
        private const int ContentWidth = 520;

        private readonly TextBox folderBox = new TextBox();
        private readonly Button browseButton = new Button();
        private readonly CheckBox autostartBox = new CheckBox();
        private readonly List<KeyValuePair<SetupOption, CheckBox>> optionBoxes = new List<KeyValuePair<SetupOption, CheckBox>>();
        private readonly List<KeyValuePair<SetupField, ComboBox>> fieldBoxes = new List<KeyValuePair<SetupField, ComboBox>>();
        private readonly ProgressBar progress = new ProgressBar();
        private readonly Label status = new Label();
        private readonly Button installButton = new Button();

        public SetupForm()
        {
            Text = "Установка " + AppIdentity.Name + " " + AppIdentity.Version;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = SystemFonts.MessageBoxFont;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

            var layout = new TableLayoutPanel { AutoSize = true, Padding = new Padding(16), ColumnCount = 1 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            Controls.Add(layout);

            layout.Controls.Add(Wrapped(SetupProfile.Intro, new Padding(0, 0, 0, 4)));
            string notice = SetupProfile.Notice();
            if (notice != null) layout.Controls.Add(Wrapped(notice, new Padding(0, 8, 0, 0)));

            layout.Controls.Add(Section("Папка установки"));
            var folderRow = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = Padding.Empty };
            folderBox.Width = ContentWidth - 44;
            folderBox.Text = InstallTarget.DefaultDirectory();
            browseButton.Text = "…";
            browseButton.Width = 36;
            browseButton.Height = folderBox.Height + 2;
            browseButton.Click += (sender, e) => BrowseFolder();
            folderRow.Controls.Add(folderBox);
            folderRow.Controls.Add(browseButton);
            layout.Controls.Add(folderRow);
            layout.Controls.Add(Hint("Удаление стирает эту папку целиком, поэтому подойдёт пустая папка или папка прежней установки."));

            List<SetupField> fields = SetupProfile.Fields();
            if (fields.Count > 0) layout.Controls.Add(Section("Настройки"));
            foreach (SetupField field in fields)
            {
                layout.Controls.Add(new Label { Text = field.Label, AutoSize = true, Margin = new Padding(0, 6, 0, 2) });
                var box = new ComboBox { DropDownStyle = ComboBoxStyle.DropDown, Width = ContentWidth, Text = field.Value };
                foreach (string suggestion in field.Suggestions) box.Items.Add(suggestion);
                box.Text = field.Value;
                fieldBoxes.Add(new KeyValuePair<SetupField, ComboBox>(field, box));
                layout.Controls.Add(box);
                if (field.Hint != null) layout.Controls.Add(Hint(field.Hint));
            }

            layout.Controls.Add(Section("Что сделать"));
            foreach (SetupOption option in SetupProfile.Options())
            {
                var box = new CheckBox
                {
                    Text = option.Text,
                    Checked = option.Checked,
                    Enabled = option.Enabled,
                    AutoSize = true,
                    MaximumSize = new Size(ContentWidth, 0),
                    Margin = new Padding(0, 4, 0, 0)
                };
                optionBoxes.Add(new KeyValuePair<SetupOption, CheckBox>(option, box));
                layout.Controls.Add(box);
                if (option.Hint != null) layout.Controls.Add(Hint(option.Hint));
                if (option.Details != null) AddDetails(layout, option);
            }
            autostartBox.Text = "Запускать при входе в Windows";
            autostartBox.Checked = true;
            autostartBox.AutoSize = true;
            autostartBox.Margin = new Padding(0, 4, 0, 0);
            layout.Controls.Add(autostartBox);

            progress.Width = ContentWidth;
            progress.Margin = new Padding(0, 16, 0, 4);
            layout.Controls.Add(progress);
            status.AutoSize = true;
            status.MaximumSize = new Size(ContentWidth, 0);
            layout.Controls.Add(status);

            installButton.Text = "Установить";
            installButton.AutoSize = true;
            installButton.Anchor = AnchorStyles.Right;
            installButton.Margin = new Padding(0, 8, 0, 0);
            installButton.Click += (sender, e) => StartInstall();
            layout.Controls.Add(installButton);
            AcceptButton = installButton;
        }

        private static Label Wrapped(string text, Padding margin)
        {
            return new Label { Text = text, AutoSize = true, MaximumSize = new Size(ContentWidth, 0), Margin = margin };
        }

        private Label Section(string text)
        {
            return new Label { Text = text, AutoSize = true, Font = new Font(Font, FontStyle.Bold), Margin = new Padding(0, 14, 0, 4) };
        }

        private static Label Hint(string text)
        {
            return new Label { Text = text, AutoSize = true, MaximumSize = new Size(ContentWidth, 0), ForeColor = SystemColors.GrayText, Margin = new Padding(18, 2, 0, 0) };
        }

        private static void AddDetails(TableLayoutPanel layout, SetupOption option)
        {
            string title = option.DetailsTitle ?? "Подробнее";
            var toggle = new LinkLabel { Text = title + " ▸", AutoSize = true, Margin = new Padding(18, 2, 0, 0) };
            Label details = Hint(option.Details);
            details.Visible = false;
            toggle.LinkClicked += (sender, e) =>
            {
                details.Visible = !details.Visible;
                toggle.Text = title + (details.Visible ? " ▾" : " ▸");
            };
            layout.Controls.Add(toggle);
            layout.Controls.Add(details);
        }

        private void BrowseFolder()
        {
            using (var dialog = new FolderBrowserDialog { Description = "Папка установки " + AppIdentity.Name, ShowNewFolderButton = true })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                string chosen = dialog.SelectedPath;
                bool isEmptyOrOurs = !Directory.Exists(chosen) || InstallTarget.Validate(chosen) == null;
                folderBox.Text = isEmptyOrOurs ? chosen : Path.Combine(chosen, AppIdentity.Name);
            }
        }

        private void StartInstall()
        {
            if (RunningApp.IsUninstalling())
            {
                MessageBox.Show(this, "Сейчас идёт удаление " + AppIdentity.Name + ". Заверши его — ответь в окне удаления и дождись сообщения о результате, — затем нажми «Установить» ещё раз.",
                    Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string problem = InstallTarget.Validate(folderBox.Text);
            if (problem != null)
            {
                MessageBox.Show(this, problem, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            var request = new InstallRequest { TargetDirectory = Path.GetFullPath(folderBox.Text), EnablesAutostart = autostartBox.Checked };
            foreach (KeyValuePair<SetupOption, CheckBox> pair in optionBoxes)
            {
                if (pair.Value.Enabled && pair.Value.Checked) request.CheckedOptions.Add(pair.Key.Key);
            }
            foreach (KeyValuePair<SetupField, ComboBox> pair in fieldBoxes) request.Values[pair.Key.Key] = pair.Value.Text.Trim();
            SetEditable(false);
            var installation = new Installation(request, Report);
            var worker = new Thread(() =>
            {
                Exception failure = null;
                try
                {
                    installation.Run();
                }
                catch (Exception error)
                {
                    failure = error;
                }
                BeginInvoke(new Action(() => Finish(installation, failure)));
            });
            worker.IsBackground = true;
            worker.Start();
        }

        private void SetEditable(bool editable)
        {
            folderBox.Enabled = editable;
            browseButton.Enabled = editable;
            autostartBox.Enabled = editable;
            installButton.Enabled = editable;
            foreach (KeyValuePair<SetupOption, CheckBox> pair in optionBoxes) pair.Value.Enabled = editable && pair.Key.Enabled;
            foreach (KeyValuePair<SetupField, ComboBox> pair in fieldBoxes) pair.Value.Enabled = editable;
        }

        private void Report(int percent, string message)
        {
            BeginInvoke(new Action(() =>
            {
                progress.Value = Math.Max(0, Math.Min(100, percent));
                status.Text = message;
            }));
        }

        private void Finish(Installation installation, Exception failure)
        {
            if (failure != null)
            {
                status.Text = "Ошибка: " + failure.Message;
                SetEditable(true);
                MessageBox.Show(this, failure.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            string message = AppIdentity.Name + " установлен и запущен — значок в трее.";
            if (installation.Notes.Count > 0) message += Environment.NewLine + Environment.NewLine + string.Join(Environment.NewLine + Environment.NewLine, installation.Notes);
            MessageBox.Show(this, message, Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
            Close();
        }
    }
}
