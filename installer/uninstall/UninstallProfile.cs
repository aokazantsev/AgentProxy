using System.Collections.Generic;

namespace AgentProxy
{
    internal static class UninstallProfile
    {
        public const string ConfirmDetails =
            "MCP-сервер, скилл и хуки Claude Code, которые поставил AgentProxy, будут сняты; чужие хуки и настройки останутся. "
            + "Удалятся история запросов и корневой сертификат прокси: после повторной установки сертификат на устройства придётся поставить заново.";

        public static void Remove(string installDirectory, List<string> problems)
        {
            EngineCliResult result = EngineCli.Run(installDirectory, "claude uninstall");
            UninstallLog.Append("claude uninstall: " + result.Describe());
            if (!result.Succeeded)
            {
                string fallback = ClaudeConfigCleaner.ForCurrentUser().Clean();
                UninstallLog.Append("claude fallback cleanup: " + (fallback ?? "done"));
                if (fallback != null) problems.Add("интеграция с Claude Code снята не полностью: " + fallback);
            }
            bool stopped = EngineProcesses.StopAll(installDirectory);
            UninstallLog.Append("engine stopped: " + stopped);
            if (!stopped) problems.Add("движок не закрылся");
        }
    }
}
