using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;

namespace AgentProxy
{
    internal static class SetupProfile
    {
        public const string ResetSettingsKey = "resetSettings";
        public const string ClaudeKey = "claude";

        public const string Intro =
            "Прокси-сниффер HTTP(S) вместо Charles. Запросы телефона, эмулятора или браузера записываются, а Claude "
            + "читает их и ждёт нужные сам через MCP-сервер agentproxy. Ставится в папку пользователя, права администратора не нужны.";

        public static string Notice()
        {
            return null;
        }

        public static List<SetupField> Fields()
        {
            return new List<SetupField>();
        }

        public static List<SetupOption> Options()
        {
            return new List<SetupOption>
            {
                new SetupOption
                {
                    Key = ClaudeKey,
                    Text = "Подключить к Claude Code",
                    Hint = "MCP-сервер agentproxy, скилл agentproxy и два хука: на старте сессии Claude узнаёт, работает ли прокси, "
                        + "а после обновления — что нового. Чужие хуки, серверы и настройки не трогаются.",
                    DetailsTitle = "Что и куда пишется",
                    Details = "• ~/.claude.json — запись mcpServers.agentproxy (адрес http://127.0.0.1:<порт API>/mcp);\n"
                        + "• ~/.claude/skills/agentproxy/SKILL.md — инструкция для Claude;\n"
                        + "• ~/.claude/settings.json — хук SessionStart (запуск agentproxy из папки установки) и хук UserPromptSubmit "
                        + "(запрос к 127.0.0.1, без запуска процессов).\n"
                        + "Снять — пункт трея «Отключить от Claude Code» или удаление программы.",
                    Checked = ClaudeWanted()
                },
                new SetupOption
                {
                    Key = ResetSettingsKey,
                    Text = "Сбросить настройки",
                    Hint = "Порты и параметры прокси вернутся к стандартным. История запросов, корневой сертификат и журнал сохранятся.",
                    Checked = false
                },
                new SetupOption
                {
                    Key = CrashReportConsent.OptionKey,
                    Text = "Отправлять автору отчёты о сбоях",
                    Hint = "В отчёт попадает журнал программы, а в нём — пути с именем пользователя Windows и адреса хостов, "
                        + "к которым шли запросы. Не включай, если это запрещают правила твоей компании.",
                    DetailsTitle = "Что уходит в отчёте",
                    Details = "Отчёт уходит один раз — при падении программы или движка — на aokazantsev.ru (сервер в России), "
                        + "без повторных попыток:\n"
                        + "• версия программы, Windows и .NET;\n"
                        + "• текст ошибки;\n"
                        + "• последние 100 КБ журнала: при падении программы — %LOCALAPPDATA%\\AgentProxy\\log.txt, при падении "
                        + "движка — engine.log. В них шаги запуска, порты, ошибки и имена хостов, с которыми не удалось "
                        + "соединиться или которые не приняли сертификат прокси;\n"
                        + "• IP-адрес, с которого пришёл отчёт.\n"
                        + "Тела и заголовки запросов, токены и история запросов в отчёт не попадают. Отчёты видит только автор, "
                        + "хранятся последние 50 МБ. Изменить выбор — переустановить программу.",
                    Checked = CrashReportConsent.IsGiven
                }
            };
        }

        public static bool IsUpdate()
        {
            return UninstallRegistration.InstalledLocation() != null;
        }

        private static bool ClaudeWanted()
        {
            return !IsUpdate() || File.Exists(Path.Combine(AppIdentity.DataDirectory, "claude-integration.json"));
        }

        public static void BeforeExtract(InstallRequest request, Action<int, string> report, List<string> notes)
        {
            if (request.Has(ResetSettingsKey))
            {
                string settingsPath = Path.Combine(AppIdentity.DataDirectory, "settings.json");
                if (File.Exists(settingsPath)) File.Delete(settingsPath);
                SetupLog.Append("settings reset");
            }
            CrashReportConsent.Set(request.Has(CrashReportConsent.OptionKey));
            SetupLog.Append("crash reports: " + request.Has(CrashReportConsent.OptionKey));
            report(5, "Останавливаю движок…");
            string target = request.TargetDirectory;
            foreach (string folder in new[] { "engine", "runtime" })
            {
                string problem = FolderRemoval.Delete(Path.Combine(target, folder), () => EngineProcesses.StopAll(target));
                if (problem != null) throw new InvalidOperationException("Не удалось заменить файлы движка (" + problem + "). Закрой программы, которые их держат, и повтори установку.");
            }
            SetupLog.Append("old engine and runtime removed");
        }

        public static bool IsDifferentLocation(string previous, string target)
        {
            return !string.Equals(Normalize(previous), Normalize(target), StringComparison.OrdinalIgnoreCase);
        }

        private static string Normalize(string path)
        {
            return Path.GetFullPath(path.Replace('/', '\\')).TrimEnd('\\');
        }

        private static void RemovePreviousLocation(string target, List<string> notes)
        {
            string previous = UninstallRegistration.InstalledLocation();
            if (previous == null || !IsDifferentLocation(previous, target)) return;
            if (!File.Exists(Path.Combine(previous, AppIdentity.ExecutableName))) return;
            string problem = FolderRemoval.Delete(previous, () => EngineProcesses.StopAll(previous));
            SetupLog.Append("previous location " + previous + ": " + (problem ?? "removed"));
            notes.Add(problem == null
                ? "Прежняя установка в " + previous + " удалена: программа переехала в новую папку."
                : "Прежнюю установку в " + previous + " удалить не удалось (" + problem + "). Удали папку вручную.");
        }

        public static void AfterExtract(InstallRequest request, Action<int, string> report, List<string> notes)
        {
            RemovePreviousLocation(request.TargetDirectory, notes);
            report(80, "Claude Code…");
            bool wanted = request.Has(ClaudeKey);
            EngineCliResult result = EngineCli.Run(request.TargetDirectory, "claude " + (wanted ? "install" : "uninstall"));
            SetupLog.Append("claude " + (wanted ? "install" : "uninstall") + ": " + result.Describe());
            if (!result.Succeeded)
            {
                notes.Add("Claude Code: " + (wanted ? "подключить" : "отключить") + " не удалось (" + result.Describe()
                    + "). Это можно сделать из меню трея.");
            }
            else if (wanted)
            {
                notes.Add("Claude Code подключён: MCP-сервер agentproxy, скилл и хуки. Уже открытые сессии Claude Code увидят MCP-сервер после перезапуска.");
            }
        }

        public static void Launch(string executable)
        {
            Process.Start(new ProcessStartInfo(executable) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(executable) });
        }
    }
}
