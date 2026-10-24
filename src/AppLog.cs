using System;
using System.IO;
using System.Text;
using System.Threading;

namespace AgentProxy
{
    internal static class AppLog
    {
        private const long MaxBytes = 512 * 1024;
        private const int WriteAttempts = 5;
        private const int WriteRetryMs = 20;

        public static readonly string FilePath = UserDataPaths.File("log.txt");
        private static readonly string PreviousFilePath = UserDataPaths.File("log.old.txt");

        private static readonly object Gate = new object();

        public static void Append(string line)
        {
            string text = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine;
            lock (Gate)
            {
                for (int attempt = 1; attempt <= WriteAttempts; attempt++)
                {
                    try
                    {
                        Write(text);
                        return;
                    }
                    catch (IOException)
                    {
                    }
                    catch (UnauthorizedAccessException)
                    {
                        return;
                    }
                    Thread.Sleep(WriteRetryMs);
                }
            }
        }

        private static void Write(string text)
        {
            var info = new FileInfo(FilePath);
            if (info.Exists && info.Length > MaxBytes)
            {
                File.Copy(FilePath, PreviousFilePath, true);
                File.Delete(FilePath);
            }
            File.AppendAllText(FilePath, text, new UTF8Encoding(false));
        }
    }
}
