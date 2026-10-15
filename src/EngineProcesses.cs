using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;

namespace AgentProxy
{
    internal static class EngineProcesses
    {
        private const int ExitWaitMs = 10000;

        public static List<Process> Under(string installDirectory)
        {
            string root = Path.GetFullPath(installDirectory).TrimEnd('\\') + "\\";
            var found = new List<Process>();
            foreach (Process process in Process.GetProcessesByName("python"))
            {
                string path = PathOf(process);
                if (path != null && path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) found.Add(process);
                else process.Dispose();
            }
            return found;
        }

        public static bool StopAll(string installDirectory)
        {
            bool stopped = true;
            foreach (Process process in Under(installDirectory))
            {
                using (process)
                {
                    try
                    {
                        if (!process.WaitForExit(ExitWaitMs))
                        {
                            process.Kill();
                            if (!process.WaitForExit(ExitWaitMs)) stopped = false;
                        }
                    }
                    catch (Win32Exception)
                    {
                        stopped = false;
                    }
                    catch (InvalidOperationException)
                    {
                    }
                }
            }
            return stopped;
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
