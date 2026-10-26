using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class TrayApplication : ApplicationContext
    {
        private const int TooltipLimit = 63;
        private const int ProblemTextLimit = 90;
        private const int BalloonMs = 5000;

        private static readonly string StatisticsNoticePath = Path.Combine(AppIdentity.DataDirectory, "usage-notice.txt");
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
        private readonly SynchronizationContext ui;
        private EngineState notifiedState = EngineState.Starting;
        private EngineState charlesState = EngineState.Starting;
        private Action balloonClick;
        private bool statisticsNoticePending;

        public TrayApplication()
        {
            AppLog.Append("=== " + AppIdentity.Name + " " + AppIdentity.Version + " start: user " + Environment.UserName
                + ", " + Environment.OSVersion.VersionString + ", .NET " + Environment.Version
                + ", install " + installDirectory + ", data " + UserDataPaths.Directory
                + ", autostart " + Autostart.IsEnabled() + ", crash reports " + CrashReportConsent.IsGiven
                + ", usage statistics " + UsageConsent.IsGiven + ", auto update " + AutoUpdateCheck.IsEnabled);
            ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
            engine = new EngineSupervisor(installDirectory);
            claude = new ClaudeLink(installDirectory);
            BuildMenu();
            tray.ContextMenuStrip = menu;
            tray.DoubleClick += (sender, e) =>
            {
                Usage.Hit("tray.double_click");
                OpenFromIcon();
            };
            tray.BalloonTipClicked += OnBalloonClicked;
            tray.Visible = true;
            engine.Changed += (sender, e) => UpdateView();
            claude.Changed += (sender, e) => UpdateView();
            UpdateView();
            statisticsNoticePending = UsageConsent.IsGiven && !File.Exists(StatisticsNoticePath);
            engine.Start();
            claude.Refresh();
            UpdateUsageState();
            Usage.Start();
            UpdateWatcher.Start(version => ui.Post(state => ShowUpdateBalloon(version), null));
        }

        private void BuildMenu()
        {
            monitorItem.Click += (sender, e) =>
            {
                Usage.Hit("menu.monitor");
                OpenMonitor();
            };
            claudeItem.Click += (sender, e) => ToggleClaude();
            restartItem.Click += (sender, e) =>
            {
                Usage.Hit("menu.engine_restart");
                charlesState = EngineState.Starting;
                engine.Restart();
            };
            autostartItem.Click += (sender, e) => ToggleAutostart();
            menu.Items.Add(stateItem);
            menu.Items.Add(claudeStateItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(monitorItem);
            menu.Items.Add(claudeItem);
            menu.Items.Add(restartItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Настройки…", null, (sender, e) =>
            {
                Usage.Hit("menu.settings");
                OpenSettings();
            }));
            menu.Items.Add(new ToolStripMenuItem("Журнал", null, (sender, e) =>
            {
                Usage.Hit("menu.journal");
                JournalForm.ShowSingle();
            }));
            menu.Items.Add(new ToolStripMenuItem("Проверить обновление…", null, (sender, e) =>
            {
                Usage.Hit("menu.update_check");
                UpdateForm.ShowSingle();
            }));
            menu.Items.Add(autostartItem);
            menu.Items.Add(new ToolStripMenuItem("О приложении…", null, (sender, e) =>
            {
                Usage.Hit("menu.about");
                AboutForm.ShowSingle();
            }));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Выход", null, (sender, e) =>
            {
                Usage.Hit("menu.exit");
                Quit();
            }));
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
            monitorItem.Enabled = state == EngineState.Running || state == EngineState.ProxyNotListening
                || state == EngineState.PausedForCharles;
            tray.Icon = TrayIcons.For(state);
            string tooltip = AppIdentity.Name + ": " + line;
            tray.Text = tooltip.Length > TooltipLimit ? tooltip.Substring(0, TooltipLimit) : tooltip;
            NotifyProblem(state);
            NotifyCharles(state);
            NotifyStatistics(state);
            UpdateEngineUsageState(state, settings);
        }

        private void UpdateEngineUsageState(EngineState state, EngineSettings settings)
        {
            Usage.SetState("engine_running", state == EngineState.Running);
            Usage.SetState("verify_upstream", settings.VerifyUpstream);
            Usage.SetState("yield_to_charles", settings.YieldToCharles);
            if (claude.Installed != null) Usage.SetState("claude_code", claude.Installed == true && claude.Complete);
            EngineStatus status = engine.LastStatus;
            if (status == null) return;
            Usage.SetState("rules", status.Rules);
            Usage.SetState("rules_enabled", status.RulesEnabled);
        }

        private void UpdateUsageState()
        {
            Usage.SetState("autostart", Autostart.IsEnabled());
            Usage.SetState("auto_update", AutoUpdateCheck.IsEnabled);
            Usage.SetState("crash_reports", CrashReportConsent.IsGiven);
            UpdateEngineUsageState(engine.State, engine.Settings ?? EngineSettings.Load());
        }

        private void ShowBalloon(string text, ToolTipIcon icon, Action click)
        {
            balloonClick = click;
            tray.ShowBalloonTip(BalloonMs, AppIdentity.Name, text, icon);
        }

        private void ShowUpdateBalloon(string version)
        {
            ShowBalloon("Вышла версия " + version + ". Нажми, чтобы скачать и установить.", ToolTipIcon.Info, () =>
            {
                Usage.Hit("update.notify_open");
                UpdateForm.ShowSingle();
            });
        }

        private void NotifyStatistics(EngineState state)
        {
            if (!statisticsNoticePending || state == EngineState.Starting || state == EngineState.Stopped) return;
            statisticsNoticePending = false;
            if (UsageConsent.IsGiven) ShowStatisticsBalloon();
        }

        private void ShowStatisticsBalloon()
        {
            AppLog.Append("usage statistics notice shown");
            try
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllText(StatisticsNoticePath, "shown");
            }
            catch (IOException error)
            {
                AppLog.Append("usage statistics notice mark not saved: " + error.Message);
            }
            catch (UnauthorizedAccessException error)
            {
                AppLog.Append("usage statistics notice mark not saved: " + error.Message);
            }
            ShowBalloon(AppIdentity.Name + " отправляет автору анонимную статистику: счётчики меню и инструментов MCP, без запросов, "
                + "адресов и содержимого. Состав — в README, выключить — в «Настройках».", ToolTipIcon.Info, OpenSettings);
        }

        private void OnBalloonClicked(object sender, EventArgs e)
        {
            Action click = balloonClick;
            balloonClick = null;
            if (click != null) click();
        }

        private void OpenSettings()
        {
            SettingsForm.ShowSingle(OnSettingsSaved, UpdateUsageState, () => TrayIcons.IsProblem(engine.State) || engine.State == EngineState.Stopped);
        }

        private string StateText(EngineState state, EngineSettings settings)
        {
            string detail = engine.Detail;
            if (state == EngineState.Running)
            {
                int flows = engine.LastStatus != null ? engine.LastStatus.Flows : 0;
                return "прокси 127.0.0.1:" + settings.ProxyPort + ", запросов " + flows;
            }
            if (state == EngineState.PausedForCharles) return "выключен, пока работает Charles; включится сам";
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
                ShowBalloon(StateText(state, engine.Settings ?? EngineSettings.Load()) + ". Подробности — в журнале.", ToolTipIcon.Warning, null);
            }
            if (state != EngineState.Starting) notifiedState = state;
        }

        private void NotifyCharles(EngineState state)
        {
            if (state == EngineState.PausedForCharles && charlesState != EngineState.PausedForCharles)
            {
                ShowBalloon("Прокси выключен, пока работает Charles. Включится сам, когда Charles закроют.", ToolTipIcon.Info, null);
            }
            else if (state == EngineState.Running && charlesState == EngineState.PausedForCharles)
            {
                ShowBalloon("Charles закрыт — прокси снова включён.", ToolTipIcon.Info, null);
            }
            if (state != EngineState.Starting) charlesState = state;
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
            if (claude.Installed == true && claude.Complete)
            {
                Usage.Hit("menu.claude_disconnect");
                claude.Uninstall();
            }
            else
            {
                Usage.Hit("menu.claude_connect");
                claude.Install();
            }
        }

        private void OnSettingsSaved()
        {
            charlesState = EngineState.Starting;
            engine.Restart();
            claude.Refresh();
        }

        private void ToggleAutostart()
        {
            bool enable = !Autostart.IsEnabled();
            Usage.Hit(enable ? "menu.autostart_on" : "menu.autostart_off");
            string problem = enable ? Autostart.Enable(Application.ExecutablePath) : Autostart.Disable();
            AppLog.Append("autostart " + (enable ? "enable" : "disable") + ": " + (problem ?? "ok"));
            Usage.SetState("autostart", Autostart.IsEnabled());
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
