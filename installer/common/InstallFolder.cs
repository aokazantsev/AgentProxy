using System;
using System.IO;

namespace AgentProxy
{
    internal static class InstallFolder
    {
        public static readonly string[] LeftoverNames = { AppIdentity.UninstallerName, AppIdentity.InstallMarkerName };

        public static bool IsOurs(string directory, string registered)
        {
            if (File.Exists(Path.Combine(directory, AppIdentity.InstallMarkerName))) return true;
            return registered != null && SamePath(registered, directory) && File.Exists(Path.Combine(directory, AppIdentity.ExecutableName));
        }

        public static void Mark(string directory)
        {
            Directory.CreateDirectory(directory);
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
                if (!IsLeftoverName(Path.GetFileName(entry))) return false;
            }
            return true;
        }

        private static bool IsLeftoverName(string name)
        {
            foreach (string leftover in LeftoverNames)
            {
                if (string.Equals(name, leftover, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        private static bool SamePath(string first, string second)
        {
            return string.Equals(Path.GetFullPath(first).TrimEnd('\\'), Path.GetFullPath(second).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
        }
    }
}
