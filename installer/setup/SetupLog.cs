using System;
using System.IO;
using System.Security.Principal;
using System.Text;

namespace AgentProxy
{
    internal static class SetupLog
    {
        public static readonly string FilePath = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "-setup.log");

        public static void Start()
        {
            Write("=== " + AppIdentity.Name + " " + AppIdentity.Version + ", " + Environment.UserName + ", elevated=" + IsElevated(), false);
        }

        public static void Append(string line)
        {
            Write(line, true);
        }

        private static void Write(string line, bool append)
        {
            try
            {
                string text = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine;
                if (append) File.AppendAllText(FilePath, text, new UTF8Encoding(false));
                else File.WriteAllText(FilePath, text, new UTF8Encoding(false));
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        private static bool IsElevated()
        {
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            {
                return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
            }
        }
    }
}
