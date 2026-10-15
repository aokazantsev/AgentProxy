using System;
using System.IO;

namespace AgentProxy
{
    internal static class InstallFolder
    {
        public static bool IsOurs(string directory, string registered)
        {
            if (File.Exists(Path.Combine(directory, AppIdentity.InstallMarkerName))) return true;
            return registered != null && SamePath(registered, directory) && File.Exists(Path.Combine(directory, AppIdentity.ExecutableName));
        }

        public static void Mark(string directory)
        {
            File.WriteAllText(Path.Combine(directory, AppIdentity.InstallMarkerName), AppIdentity.Name + " " + AppIdentity.Version);
        }

        public static bool OwnsRegistration(string registered, string installDirectory)
        {
            if (registered == null || !File.Exists(Path.Combine(registered, AppIdentity.ExecutableName))) return true;
            return SamePath(registered, installDirectory);
        }

        public static bool HoldsOnlyLeftovers(string directory)
        {
            if (!Directory.Exists(directory)) return false;
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                string name = Path.GetFileName(entry);
                if (!string.Equals(name, AppIdentity.UninstallerName, StringComparison.OrdinalIgnoreCase)
                    && !string.Equals(name, AppIdentity.InstallMarkerName, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }

        private static bool SamePath(string first, string second)
        {
            return string.Equals(Path.GetFullPath(first).TrimEnd('\\'), Path.GetFullPath(second).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
        }
    }
}
