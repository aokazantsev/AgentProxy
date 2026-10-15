using System;
using System.IO;

namespace AgentProxy
{
    internal static class InstallTarget
    {
        public static string DefaultDirectory()
        {
            string installed = UninstallRegistration.InstalledLocation();
            if (installed != null && File.Exists(Path.Combine(installed, AppIdentity.ExecutableName))) return installed;
            return AppIdentity.DefaultInstallDirectory;
        }

        public static string Validate(string directory)
        {
            if (string.IsNullOrWhiteSpace(directory)) return "Укажи папку установки.";
            string fullPath;
            try
            {
                fullPath = Path.GetFullPath(directory);
            }
            catch (Exception)
            {
                return "Путь к папке некорректный.";
            }
            if (string.Equals(fullPath.TrimEnd('\\'), Path.GetPathRoot(fullPath).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            {
                return "Нельзя ставить в корень диска — выбери папку, например " + AppIdentity.DefaultInstallDirectory + ".";
            }
            string installed = UninstallRegistration.InstalledLocation();
            if (installed != null && SetupProfile.IsDifferentLocation(installed, fullPath) && SetupProfile.IsNested(installed, fullPath))
            {
                return "Папка пересекается с прежней установкой в " + installed + ". Выбери эту же папку или папку вне её.";
            }
            if (SetupProfile.IsNested(AppIdentity.DataDirectory, fullPath))
            {
                return "Папка пересекается с папкой данных " + AppIdentity.DataDirectory + ". Выбери другую папку.";
            }
            if (!Directory.Exists(fullPath)) return null;
            if (File.Exists(Path.Combine(fullPath, AppIdentity.ExecutableName))) return null;
            if (Directory.GetFileSystemEntries(fullPath).Length == 0) return null;
            return "Папка не пустая и в ней нет " + AppIdentity.Name + ". Удаление стирает папку установки целиком, поэтому выбери пустую или новую папку.";
        }
    }
}
