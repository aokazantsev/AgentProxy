using System;
using System.IO;

namespace AgentProxy
{
    internal static class AppIdentity
    {
        public const string Name = "AgentProxy";
        public const string Version = "0.12";
        public const string UpdateSetupArguments = "/silent /notify";
        public const string UpdateKeeps = "настройки, правила подмены, история запросов и сертификат сохранятся";
        public const string ExecutableName = "AgentProxy.exe";
        public const string ProcessName = "AgentProxy";
        public const string UninstallerName = "Uninstall.exe";
        public const string InstallMarkerName = "AgentProxy.install";
        public const string SingleInstanceMutex = "AgentProxy.SingleInstance";
        public const string SetupMutex = "AgentProxy.Setup";
        public const string GitHubUrl = "https://github.com/aokazantsev/AgentProxy";
        public const string SiteUrl = "https://aokazantsev.ru/pets/agentproxy/";

        public static string DefaultInstallDirectory
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", Name); }
        }

        public static string DataDirectory
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), Name); }
        }
    }
}
