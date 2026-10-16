using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class TrayApplication : ApplicationContext
    {
        private const int TooltipLimit = 63;
        private const int ProblemTextLimit = 90;

        private readonly string installDirectory = Application.StartupPath;
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly ContextMenuStrip menu = new ContextMenuStrip();
        private readonly ToolStripMenuItem stateItem = new ToolStripMenuItem { Enabled = false };
        private readonly ToolStripMenuItem claudeStateItem = new ToolStripMenuItem { Enabled = false };
        private readonly ToolStripMenuItem monitorItem = new ToolStripMenuItem("Открыть монитор");
        private readonly ToolStripMenuItem claudeItem = new ToolStripMenuItem();
        private readonly ToolStripMenuItem restartItem = new ToolStripMenuItem("Перезапустить движок");
        private readonly ToolStripMenuItem autostartItem = new ToolStripMenuItem("Запускать при входе в Windows");
        private readonly EngineSupervisor engine;
        private readonly ClaudeLink claude;
        private EngineState notifiedState = EngineState.Starting;

        public TrayApplication()
        {
            AppLog.Append("=== " + AppIdentity.Name + " " + AppIdentity.Version + " start: user " + Environment.UserName
                + ", " + Environment.OSVersion.VersionString + ", .NET " + Environment.Version
                + ", install " + installDirectory + ", data " + UserDataPaths.Directory
                + ", autostart " + Autostart.IsEnabled() + ", crash reports " + CrashReportConsent.IsGiven);
            engine = new EngineSupervisor(installDirectory);
            claude = new ClaudeLink(installDirectory);
            BuildMenu();
            tray.ContextMenuStrip = menu;
            tray.DoubleClick += (sender, e) => OpenFromIcon();
            tray.Visible = true;
            engine.Changed += (sender, e) => UpdateView();
            claude.Changed += (sender, e) => UpdateView();
            UpdateView();
            engine.Start();
            claude.Refresh();
        }

        private void BuildMenu()
        {
            monitorItem.Click += (sender, e) => OpenMonitor();
            claudeItem.Click += (sender, e) => ToggleClaude();
            restartItem.Click += (sender, e) => engine.Restart();
            autostartItem.Click += (sender, e) => ToggleAutostart();
            menu.Items.Add(stateItem);
            menu.Items.Add(claudeStateItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(monitorItem);
            menu.Items.Add(claudeItem);
            menu.Items.Add(restartItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Настройки…", null, (sender, e) => SettingsForm.ShowSingle(OnSettingsSaved)));
            menu.Items.Add(new ToolStripMenuItem("Журнал", null, (sender, e) => JournalForm.ShowSingle()));
            menu.Items.Add(new ToolStripMenuItem("Проверить обновление…", null, (sender, e) => UpdateForm.ShowSingle()));
            menu.Items.Add(autostartItem);
            menu.Items.Add(new ToolStripMenuItem("О приложении…", null, (sender, e) => AboutForm.ShowSingle()));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Выход", null, (sender, e) => Quit()));
            menu.Opening += (sender, e) => autostartItem.Checked = Autostart.IsEnabled();
        }

        private void UpdateView()
        {
            EngineState state = engine.State;
            EngineSettings settings = engine.Settings ?? EngineSettings.Load();
            string line = StateText(state, settings);
            stateItem.Text = AppIdentity.Name + " " + AppIdentity.Version + " — " + line;
            claudeStateItem.Text = ClaudeText();
            claudeItem.Text = ClaudeActionText();
            claudeItem.Enabled = !claude.IsBusy;
            monitorItem.Enabled = state == EngineState.Running || state == EngineState.ProxyNotListening;
            tray.Icon = TrayIcons.For(state);
            string tooltip = AppIdentity.Name + ": " + line;
            tray.Text = tooltip.Length > TooltipLimit ? tooltip.Substring(0, TooltipLimit) : tooltip;
            NotifyProblem(state);
        }

        private string StateText(EngineState state, EngineSettings settings)
        {
            string detail = engine.Detail;
            if (state == EngineState.Running)
            {
                int flows = engine.LastStatus != null ? engine.LastStatus.Flows : 0;
                return "прокси 127.0.0.1:" + settings.ProxyPort + ", запросов " + flows;
            }
            if (state == EngineState.Starting) return "запускается…";
            if (state == EngineState.Stopped) return "остановлен";
            if (state == EngineState.PortBusy) return detail ?? "порт занят";
            if (state == EngineState.Crashed) return detail ?? "движок упал";
            return detail ?? state.ToString();
        }

        private string ClaudeActionText()
        {
            if (claude.Installed != true) return "Подключить к Claude Code";
            return claude.Complete ? "Отключить от Claude Code" : "Переподключить к Claude Code";
        }

        private string ClaudeText()
        {
            if (claude.IsBusy) return "Claude Code: обновляю интеграцию…";
            if (claude.LastProblem != null) return "Claude Code: ошибка — " + Shorten(claude.LastProblem, ProblemTextLimit);
            if (claude.Installed == null) return "Claude Code: проверяю…";
            if (claude.Installed == false) return "Claude Code: не подключён";
            if (!claude.Complete) return "Claude Code: подключён не полностью — переподключи";
            return "Claude Code: подключён";
        }

        private static string Shorten(string text, int limit)
        {
            string line = text.Replace("\r", " ").Replace("\n", " ").Trim();
            return line.Length > limit ? line.Substring(0, limit) + "…" : line;
        }

        private void NotifyProblem(EngineState state)
        {
            bool problem = TrayIcons.IsProblem(state);
            if (problem && !TrayIcons.IsProblem(notifiedState))
            {
                tray.ShowBalloonTip(5000, AppIdentity.Name, StateText(state, engine.Settings ?? EngineSettings.Load())
                    + ". Подробности — в журнале.", ToolTipIcon.Warning);
            }
            if (state != EngineState.Starting) notifiedState = state;
        }

        private void OpenMonitor()
        {
            EngineSettings settings = engine.Settings ?? EngineSettings.Load();
            try
            {
                Process.Start(new ProcessStartInfo("http://127.0.0.1:" + settings.ApiPort + "/") { UseShellExecute = true });
            }
            catch (Win32Exception error)
            {
                AppLog.Append("monitor open failed: " + error.Message);
            }
        }

        private void ToggleClaude()
        {
            if (claude.Installed == true && claude.Complete) claude.Uninstall();
            else claude.Install();
        }

        private void OnSettingsSaved()
        {
            engine.Restart();
            claude.Refresh();
        }

        private void ToggleAutostart()
        {
            bool enable = !Autostart.IsEnabled();
            string problem = enable ? Autostart.Enable(Application.ExecutablePath) : Autostart.Disable();
            AppLog.Append("autostart " + (enable ? "enable" : "disable") + ": " + (problem ?? "ok"));
            if (problem != null) MessageBox.Show(problem, AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }

        private void OpenFromIcon()
        {
            if (monitorItem.Enabled) OpenMonitor();
            else JournalForm.ShowSingle();
        }

        private void Quit()
        {
            AppLog.Append("exit requested");
            tray.Visible = false;
            engine.Dispose();
            ExitThread();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                engine.Dispose();
                tray.Dispose();
                menu.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
