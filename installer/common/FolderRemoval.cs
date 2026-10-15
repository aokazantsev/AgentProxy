using System;
using System.IO;
using System.Threading;

namespace AgentProxy
{
    internal static class FolderRemoval
    {
        private const int Attempts = 20;
        private const int RetryMs = 500;

        public static string DeleteContentsExcept(string directory, params string[] keptFileNames)
        {
            if (!Directory.Exists(directory)) return null;
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                string problem;
                if (Directory.Exists(entry)) problem = Delete(entry, null);
                else if (IsKept(Path.GetFileName(entry), keptFileNames)) continue;
                else problem = DeleteFile(entry);
                if (problem != null) return problem;
            }
            return null;
        }

        private static bool IsKept(string name, string[] keptFileNames)
        {
            foreach (string kept in keptFileNames)
            {
                if (string.Equals(name, kept, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        private static string DeleteFile(string path)
        {
            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    File.Delete(path);
                    return null;
                }
                catch (IOException error)
                {
                    if (attempt >= Attempts) return error.Message;
                }
                catch (UnauthorizedAccessException error)
                {
                    if (attempt >= Attempts) return error.Message;
                }
                Thread.Sleep(RetryMs);
            }
        }

        public static string Delete(string directory, Action beforeAttempt)
        {
            for (int attempt = 1; ; attempt++)
            {
                if (!Directory.Exists(directory)) return null;
                if (beforeAttempt != null) beforeAttempt();
                try
                {
                    Directory.Delete(directory, true);
                    return null;
                }
                catch (IOException error)
                {
                    if (attempt >= Attempts) return error.Message;
                }
                catch (UnauthorizedAccessException error)
                {
                    if (attempt >= Attempts) return error.Message;
                }
                Thread.Sleep(RetryMs);
            }
        }
    }
}
