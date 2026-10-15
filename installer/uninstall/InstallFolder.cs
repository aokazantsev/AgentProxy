using System;
using System.IO;

namespace AgentProxy
{
    internal static class InstallFolder
    {
        public static bool LooksInstalled(string directory)
        {
            return File.Exists(Path.Combine(directory, AppIdentity.ExecutableName))
                || (File.Exists(Path.Combine(directory, AppIdentity.UninstallerName)) && Directory.Exists(Path.Combine(directory, "runtime")));
        }

        public static bool OwnsRegistration(string registered, string installDirectory)
        {
            if (registered == null || !File.Exists(Path.Combine(registered, AppIdentity.ExecutableName))) return true;
            return string.Equals(Path.GetFullPath(registered).TrimEnd('\\'), Path.GetFullPath(installDirectory).TrimEnd('\\'),
                StringComparison.OrdinalIgnoreCase);
        }

        public static bool HoldsOnlyUninstaller(string directory)
        {
            if (!Directory.Exists(directory)) return false;
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                if (!string.Equals(Path.GetFileName(entry), AppIdentity.UninstallerName, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }
    }
}
