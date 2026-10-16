using System;
using System.IO;
using System.Runtime.InteropServices;

namespace AgentProxy
{
    internal static class SystemTools
    {
        private const uint LoadLibrarySearchSystem32 = 0x800;

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetDefaultDllDirectories(uint directoryFlags);

        public static void LoadDllsOnlyFromSystem()
        {
            SetDefaultDllDirectories(LoadLibrarySearchSystem32);
        }

        public static string Cmd
        {
            get { return Path.Combine(Environment.SystemDirectory, "cmd.exe"); }
        }

        public static string Ping
        {
            get { return Path.Combine(Environment.SystemDirectory, "PING.EXE"); }
        }

        public static string Taskkill
        {
            get { return Path.Combine(Environment.SystemDirectory, "taskkill.exe"); }
        }

        public static string Explorer
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe"); }
        }
    }
}
