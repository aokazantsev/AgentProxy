using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;

namespace AgentProxy
{
    internal static class RunningApp
    {
        private const int ExitWaitMs = 5000;

        public static bool IsUninstalling()
        {
            string workerPrefix = AppIdentity.Name + "Uninstall-";
            string installed = UninstallRegistration.InstalledLocation();
            foreach (Process process in Process.GetProcesses())
            {
                using (process)
                {
                    if (process.ProcessName.StartsWith(workerPrefix, StringComparison.OrdinalIgnoreCase)) return true;
                    if (installed == null || !string.Equals(process.ProcessName, "Uninstall", StringComparison.OrdinalIgnoreCase)) continue;
                    string path = PathOf(process);
                    if (path != null && string.Equals(Path.GetDirectoryName(path).TrimEnd('\\'), installed.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) return true;
                }
            }
            return false;
        }

        public static bool Stop()
        {
            bool stoppedAll = true;
            foreach (Process process in Process.GetProcessesByName(AppIdentity.ProcessName))
            {
                using (process)
                {
                    try
                    {
                        process.Kill();
                        if (!process.WaitForExit(ExitWaitMs)) stoppedAll = false;
                    }
                    catch (Win32Exception)
                    {
                        stoppedAll = false;
                    }
                    catch (InvalidOperationException)
                    {
                    }
                }
            }
            return stoppedAll;
        }

        private static string PathOf(Process process)
        {
            try
            {
                return process.MainModule.FileName;
            }
            catch (Win32Exception)
            {
                return null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
            catch (NullReferenceException)
            {
                return null;
            }
        }
    }
}
