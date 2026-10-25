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
            + "читает их, ждёт нужные и подменяет ответы правилами (map local, rewrite) сам через MCP-сервер agentproxy. Ставится в папку пользователя, права администратора не нужны.";

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
                        + "• ~/.claude/skills/agentproxy/ — SKILL.md и rules.md, инструкция для Claude;\n"
                        + "• ~/.claude/settings.json — хук SessionStart (запуск agentproxy из папки установки) и хук UserPromptSubmit "
                        + "(запрос к 127.0.0.1, без запуска процессов).\n"
                        + "Снять — пункт трея «Отключить от Claude Code» или удаление программы.",
                    Checked = ClaudeWanted()
                },
                new SetupOption
                {
                    Key = ResetSettingsKey,
                    Text = "Сбросить настройки",
                    Hint = "Порты и параметры прокси (проверка сертификатов, выключение на время Charles) вернутся к стандартным, закреплённые в мониторе хосты открепятся. Правила подмены, история запросов, корневой сертификат и журнал сохранятся.",
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
                        + "движка — engine.log. В них шаги запуска, порты, ошибки и имена хостов (без пути и параметров), с которыми не удалось "
                        + "соединиться или соединение с которыми оборвалось, которые не приняли сертификат прокси или к запросам которых "
                        + "применились правила подмены, а также номера записей истории, id правил, их итог и пути к файлам "
                        + "ответов правил;\n"
                        + "• IP-адрес, с которого пришёл отчёт.\n"
                        + "Тела и заголовки запросов, токены и история запросов в отчёт не попадают. Отчёты видит только автор, "
                        + "хранятся последние 50 МБ. Изменить выбор — переустановить программу.",
                    Checked = CrashReportConsent.IsGiven
                },
                new SetupOption
                {
                    Key = UsageConsent.OptionKey,
                    Text = "Отправлять автору анонимную статистику использования",
                    Hint = "Помогает понять, сколько людей пользуется программой и какие инструменты нужны Claude. Без имён, путей, "
                        + "адресов и содержимого запросов. Выключить можно и потом — в настройках программы.",
                    DetailsTitle = "Что уходит в статистике",
                    Details = "Раз в час, пока программа запущена, на aokazantsev.ru (сервер в России) уходят счётчики за текущие и неотправленные прошлые сутки (до недели):\n"
                        + "• случайный номер установки — он не связан с тобой и компьютером;\n"
                        + "• версия программы и Windows;\n"
                        + "• сколько раз за сутки программа запускалась и сама находила новую версию, сколько раз ты пользовался "
                        + "пунктами меню, значком в трее, настройками и обновлением;\n"
                        + "• сколько раз за сутки Claude вызывал каждый инструмент MCP — только имя инструмента и число;\n"
                        + "• текущие настройки и состояние: работает ли движок, проверяются ли сертификаты серверов, выключается ли "
                        + "прокси на время Charles, подключён ли Claude Code, включены ли автозапуск, проверка обновлений и отчёты о сбоях, сколько правил подмены и "
                        + "сколько из них включено.\n"
                        + "Запросы, ответы, адреса, хосты, заголовки, тела, содержимое правил, имена пользователя и компьютера, пути "
                        + "в статистику не попадают никогда, IP-адрес сервер не хранит. Статистика хранится 400 суток, дальше — "
                        + "только итоги по дням.",
                    Checked = UsageConsent.InstallerDefault
                }
            };
        }

        private static bool ClaudeWanted()
        {
            return !InstallTarget.IsUpdate() || File.Exists(Path.Combine(AppIdentity.DataDirectory, "claude-integration.json"));
        }

        public static void BeforeExtract(InstallRequest request, Action<int, string> report, List<string> notes)
        {
            InstallFolder.Mark(request.TargetDirectory);
            CrashReportConsent.Set(request.Has(CrashReportConsent.OptionKey));
            SetupLog.Append("crash reports: " + request.Has(CrashReportConsent.OptionKey));
            UsageConsent.Set(request.Has(UsageConsent.OptionKey));
            SetupLog.Append("usage statistics: " + request.Has(UsageConsent.OptionKey));
            report(5, "Останавливаю движок…");
            string target = request.TargetDirectory;
            foreach (string folder in new[] { "engine", "runtime" })
            {
                string problem = FolderRemoval.Delete(Path.Combine(target, folder), () => EngineProcesses.StopAll(target));
                if (problem != null) throw new InvalidOperationException("Не удалось заменить файлы движка (" + problem + "). Закрой программы, которые их держат, и повтори установку.");
            }
            SetupLog.Append("old engine and runtime removed");
            if (request.Has(ResetSettingsKey))
            {
                foreach (string name in new[] { "settings.json", "monitor.json" })
                {
                    string path = Path.Combine(AppIdentity.DataDirectory, name);
                    if (File.Exists(path)) File.Delete(path);
                }
                SetupLog.Append("settings reset");
            }
        }

        public static bool IsDifferentLocation(string previous, string target)
        {
            return !string.Equals(Normalize(previous), Normalize(target), StringComparison.OrdinalIgnoreCase);
        }

        private static string Normalize(string path)
        {
            return Path.GetFullPath(path.Replace('/', '\\')).TrimEnd('\\');
        }

        public static bool IsNested(string first, string second)
        {
            string a = Normalize(first) + "\\";
            string b = Normalize(second) + "\\";
            return a.StartsWith(b, StringComparison.OrdinalIgnoreCase) || b.StartsWith(a, StringComparison.OrdinalIgnoreCase);
        }

        private static void RemovePreviousLocation(string target, List<string> notes)
        {
            string previous = UninstallRegistration.InstalledLocation();
            if (!InstallTarget.IsUpdate(previous) || !IsDifferentLocation(previous, target)) return;
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
            Process.Start(new ProcessStartInfo(executable) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(executable) });
        }
    }
}
