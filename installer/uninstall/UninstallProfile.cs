using System.Collections.Generic;

namespace AgentProxy
{
    internal static class UninstallProfile
    {
        public const string ConfirmDetails =
            "MCP-сервер, скилл и хуки Claude Code, которые поставил AgentProxy, будут сняты; чужие хуки и настройки останутся. "
            + "Удалятся история запросов и корневой сертификат прокси: после повторной установки сертификат на устройства придётся поставить заново.";

        public static void Remove(List<string> problems)
        {
            string installed = UninstallRegistration.InstalledLocation();
            if (installed == null) return;
            EngineCliResult result = EngineCli.Run(installed, "claude uninstall");
            if (!result.Succeeded) problems.Add("интеграция с Claude Code не снята: " + result.Describe());
            if (!EngineProcesses.StopAll(installed)) problems.Add("движок не закрылся");
        }
    }
}
