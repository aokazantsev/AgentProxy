using System;
using System.IO;

namespace AgentProxy
{
    internal static class AutoUpdateCheck
    {
        private static readonly string FilePath = Path.Combine(AppIdentity.DataDirectory, "update-check-off.txt");

        public static bool IsEnabled
        {
            get { return !File.Exists(FilePath); }
        }

        public static void Set(bool enabled)
        {
            if (!enabled)
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllText(FilePath, "Обновления автоматически не проверяются. Выключено " + DateTime.Now.ToString("yyyy-MM-dd HH:mm") + ".");
            }
            else if (File.Exists(FilePath))
            {
                File.Delete(FilePath);
            }
        }
    }
}
