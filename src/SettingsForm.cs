using System;
using System.Drawing;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class SettingsForm : Form
    {
        private const int ContentWidth = 380;

        private static SettingsForm openForm;

        private readonly NumericUpDown proxyPort = PortBox();
        private readonly NumericUpDown apiPort = PortBox();
        private readonly CheckBox verifyUpstream = new CheckBox { Text = "Проверять сертификаты серверов", AutoSize = true, Margin = new Padding(3, 10, 3, 0) };
        private readonly CheckBox yieldToCharles = new CheckBox { Text = "Выключать прокси, пока запущен Charles", AutoSize = true, Margin = new Padding(3, 6, 3, 0) };
        private readonly CheckBox autoUpdate = new CheckBox { Text = "Раз в сутки проверять обновление и сообщать о новой версии", AutoSize = true, Margin = new Padding(3, 10, 3, 0) };
        private readonly CheckBox usageStatistics = new CheckBox { Text = "Отправлять автору анонимную статистику использования", AutoSize = true, Margin = new Padding(3, 6, 3, 0) };
        private readonly Action saved;
        private readonly Action optionsChanged;
        private readonly Func<bool> engineDown;
        private readonly EngineSettings shown;

        public static void ShowSingle(Action saved, Action optionsChanged, Func<bool> engineDown)
        {
            if (openForm != null)
            {
                openForm.Activate();
                return;
            }
            openForm = new SettingsForm(saved, optionsChanged, engineDown);
            openForm.FormClosed += (sender, e) => openForm = null;
            openForm.Show();
        }

        private SettingsForm(Action saved, Action optionsChanged, Func<bool> engineDown)
        {
            this.saved = saved;
            this.optionsChanged = optionsChanged;
            this.engineDown = engineDown;
            shown = EngineSettings.Load();
            Text = "Настройки " + AppIdentity.Name;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = SystemFonts.MessageBoxFont;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

            var layout = new TableLayoutPanel { AutoSize = true, ColumnCount = 2, Padding = new Padding(16) };
            Controls.Add(layout);
            proxyPort.Value = shown.ProxyPort;
            apiPort.Value = shown.ApiPort;
            verifyUpstream.Checked = shown.VerifyUpstream;
            yieldToCharles.Checked = shown.YieldToCharles;
            AddRow(layout, "Порт прокси", proxyPort);
            AddRow(layout, "Порт API и MCP", apiPort);
            layout.Controls.Add(verifyUpstream);
            layout.SetColumnSpan(verifyUpstream, 2);
            layout.Controls.Add(yieldToCharles);
            layout.SetColumnSpan(yieldToCharles, 2);
            var hint = new Label
            {
                Text = "Прокси слушает только 127.0.0.1: устройство подключается через adb reverse. "
                    + "С галкой про Charles AgentProxy на время его работы не слушает порт, а потом включается сам. "
                    + "После смены этих настроек движок перезапустится, адрес MCP в Claude Code обновится сам; "
                    + "уже открытые сессии Claude Code увидят новый порт после перезапуска.",
                AutoSize = true,
                MaximumSize = new Size(ContentWidth, 0),
                ForeColor = SystemColors.GrayText,
                Margin = new Padding(3, 10, 3, 0)
            };
            layout.Controls.Add(hint);
            layout.SetColumnSpan(hint, 2);
            autoUpdate.Checked = AutoUpdateCheck.IsEnabled;
            layout.Controls.Add(autoUpdate);
            layout.SetColumnSpan(autoUpdate, 2);
            usageStatistics.Checked = UsageConsent.IsGiven;
            layout.Controls.Add(usageStatistics);
            layout.SetColumnSpan(usageStatistics, 2);

            var buttons = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Margin = new Padding(0, 14, 0, 0), Anchor = AnchorStyles.Right };
            var cancel = new Button { Text = "Отмена", AutoSize = true };
            cancel.Click += (sender, e) => Close();
            var save = new Button { Text = "Сохранить", AutoSize = true };
            save.Click += (sender, e) => Save();
            buttons.Controls.Add(cancel);
            buttons.Controls.Add(save);
            layout.Controls.Add(buttons);
            layout.SetColumnSpan(buttons, 2);
            AcceptButton = save;
            CancelButton = cancel;
        }

        private static NumericUpDown PortBox()
        {
            return new NumericUpDown { Minimum = 0, Maximum = 99999, Width = 90 };
        }

        private static void AddRow(TableLayoutPanel layout, string text, Control control)
        {
            layout.Controls.Add(new Label { Text = text, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 6, 12, 3) });
            layout.Controls.Add(control);
        }

        private void Save()
        {
            int proxy = (int)proxyPort.Value;
            int api = (int)apiPort.Value;
            string problem = EngineSettings.Validate(proxy, api);
            if (problem != null)
            {
                MessageBox.Show(this, problem, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            EngineSettings current = EngineSettings.LoadOver(shown);
            bool writeSettings = current.TakeChanges(shown, proxy, api, verifyUpstream.Checked, yieldToCharles.Checked);
            if (writeSettings)
            {
                problem = EngineSettings.Validate(current.ProxyPort, current.ApiPort);
                if (problem != null)
                {
                    MessageBox.Show(this, problem + " Порты в " + EngineSettings.FilePath + " поменялись, пока было открыто окно.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                try
                {
                    current.Save();
                }
                catch (Exception error)
                {
                    AppLog.Append("settings save failed: " + error);
                    MessageBox.Show(this, "Не удалось сохранить настройки: " + error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                AppLog.Append("settings saved: proxy " + current.ProxyPort + ", api " + current.ApiPort + ", verify upstream " + current.VerifyUpstream
                    + ", yield to charles " + current.YieldToCharles);
            }
            string optionsProblem = SaveOptions();
            if (optionsProblem != null)
            {
                AppLog.Append("options not saved: " + optionsProblem);
                MessageBox.Show(this, "Не удалось сохранить выбор проверки обновлений и статистики: " + optionsProblem, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            AppLog.Append("options: auto update " + AutoUpdateCheck.IsEnabled + ", usage statistics " + UsageConsent.IsGiven);
            Usage.Hit("settings.save");
            Close();
            bool changedInWindow = proxy != shown.ProxyPort || api != shown.ApiPort || verifyUpstream.Checked != shown.VerifyUpstream
                || yieldToCharles.Checked != shown.YieldToCharles;
            if (writeSettings || changedInWindow || engineDown()) saved();
            optionsChanged();
        }

        private string SaveOptions()
        {
            try
            {
                if (autoUpdate.Checked != AutoUpdateCheck.IsEnabled) AutoUpdateCheck.Set(autoUpdate.Checked);
                if (usageStatistics.Checked != UsageConsent.IsGiven) Usage.SetConsent(usageStatistics.Checked);
                return null;
            }
            catch (System.IO.IOException error)
            {
                return error.Message;
            }
            catch (UnauthorizedAccessException error)
            {
                return error.Message;
            }
        }
    }
}
