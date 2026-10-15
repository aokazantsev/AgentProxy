using System;
using System.IO;
using System.Text;

namespace AgentProxy
{
    internal static class EngineLogTail
    {
        private const int TailBytes = 64 * 1024;
        private const string CrashMarker = "CRASH";

        public static readonly string FilePath = UserDataPaths.File("engine.log");

        public static string Read(string path, int maxBytes)
        {
            try
            {
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    long start = Math.Max(0, stream.Length - maxBytes);
                    stream.Seek(start, SeekOrigin.Begin);
                    var buffer = new byte[stream.Length - start];
                    int read = stream.Read(buffer, 0, buffer.Length);
                    return new UTF8Encoding(false).GetString(buffer, 0, read);
                }
            }
            catch (IOException)
            {
                return "";
            }
            catch (UnauthorizedAccessException)
            {
                return "";
            }
        }

        public static string LastCrash(string text)
        {
            int index = text.LastIndexOf(CrashMarker, StringComparison.Ordinal);
            if (index < 0) return null;
            int lineStart = text.LastIndexOf('\n', index) + 1;
            return text.Substring(lineStart).Trim();
        }

        public static string LastCrash()
        {
            return LastCrash(Read(FilePath, TailBytes));
        }
    }
}
