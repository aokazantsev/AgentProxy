using System;
using System.IO;
using System.Security.Principal;
using System.Text;

namespace AgentProxy
{
    internal static class SetupLog
    {
        private const long MaxBytes = 512 * 1024;

        private static bool started;

        public static readonly string FilePath = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "-setup.log");
        public static readonly string PreviousFilePath = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "-setup.old.log");

        public static void Start()
        {
            if (started) return;
            started = true;
            if (IsTooLarge()) MoveToPrevious();
            Write("=== " + AppIdentity.Name + " " + AppIdentity.Version + ", " + Environment.UserName + ", elevated=" + IsElevated());
        }

        public static void Append(string line)
        {
            Start();
            Write(line);
        }

        public static void AppendWithoutHeader(string line)
        {
            Write(line);
        }

        private static bool IsTooLarge()
        {
            try
            {
                var file = new FileInfo(FilePath);
                return file.Exists && file.Length > MaxBytes;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private static void MoveToPrevious()
        {
            try
            {
                File.Delete(PreviousFilePath);
                File.Move(FilePath, PreviousFilePath);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        private static void Write(string line)
        {
            try
            {
                File.AppendAllText(FilePath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine, new UTF8Encoding(false));
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
