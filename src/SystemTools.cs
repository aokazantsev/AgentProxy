using System;
using System.IO;

namespace AgentProxy
{
    internal static class SystemTools
    {
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
