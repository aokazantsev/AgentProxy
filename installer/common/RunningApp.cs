using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;

namespace AgentProxy
{
    internal static class RunningApp
    {
        private const int ExitWaitMs = 5000;
        private const int StopPasses = 3;

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
            return Stop(AppIdentity.ProcessName);
        }

        internal static bool Stop(string processName)
        {
            for (int pass = 1; pass <= StopPasses; pass++)
            {
                bool found = false;
                bool stoppedAll = true;
                foreach (Process process in Process.GetProcessesByName(processName))
                {
                    using (process)
                    {
                        found = true;
                        if (!Kill(process)) stoppedAll = false;
                    }
                }
                if (!stoppedAll) return false;
                if (!found) return true;
            }
            return !IsRunning(processName);
        }

        private static bool IsRunning(string processName)
        {
            Process[] running = Process.GetProcessesByName(processName);
            foreach (Process process in running) process.Dispose();
            return running.Length > 0;
        }

        internal static bool Kill(Process process)
        {
            try
            {
                process.Kill();
            }
            catch (Win32Exception)
            {
            }
            catch (InvalidOperationException)
            {
                return true;
            }
            try
            {
                return process.WaitForExit(ExitWaitMs);
            }
            catch (Win32Exception)
            {
                return false;
            }
            catch (InvalidOperationException)
            {
                return true;
            }
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
