using System;
using System.IO;
using System.Text;

namespace AgentProxy
{
    internal static class AppLog
    {
        private const long MaxBytes = 512 * 1024;

        public static readonly string FilePath = UserDataPaths.File("log.txt");
        private static readonly string PreviousFilePath = UserDataPaths.File("log.old.txt");

        public static void Append(string line)
        {
            try
            {
                var info = new FileInfo(FilePath);
                if (info.Exists && info.Length > MaxBytes)
                {
                    File.Copy(FilePath, PreviousFilePath, true);
                    File.Delete(FilePath);
                }
                File.AppendAllText(FilePath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }
}
