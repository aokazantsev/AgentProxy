using System;
using System.Security;
using Microsoft.Win32;

namespace AgentProxy
{
    internal static class Autostart
    {
        private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

        public static bool IsEnabled()
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKeyPath))
            {
                return key != null && key.GetValue(AppIdentity.Name) != null;
            }
        }

        public static string Enable(string executablePath)
        {
            return Write("включить автозапуск", key => key.SetValue(AppIdentity.Name, "\"" + executablePath + "\""));
        }

        public static string Disable()
        {
            return Write("выключить автозапуск", key => key.DeleteValue(AppIdentity.Name, false));
        }

        private static string Write(string action, Action<RegistryKey> change)
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKeyPath))
                {
                    change(key);
                }
                return null;
            }
            catch (UnauthorizedAccessException error)
            {
                return "Не удалось " + action + ": " + error.Message;
            }
            catch (SecurityException error)
            {
                return "Не удалось " + action + ": " + error.Message;
            }
        }
    }
}
