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
                    ClearReadOnlyAttribute(path);
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
                    ClearReadOnly(directory);
                }
                catch (UnauthorizedAccessException error)
                {
                    if (attempt >= Attempts) return error.Message;
                    ClearReadOnly(directory);
                }
                Thread.Sleep(RetryMs);
            }
        }

        private static void ClearReadOnly(string directory)
        {
            try
            {
                if ((File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0) return;
                ClearReadOnlyAttribute(directory);
                foreach (string file in Directory.GetFiles(directory)) ClearReadOnlyAttribute(file);
                foreach (string child in Directory.GetDirectories(directory))
                {
                    if ((File.GetAttributes(child) & FileAttributes.ReparsePoint) == 0) ClearReadOnly(child);
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        private static void ClearReadOnlyAttribute(string path)
        {
            try
            {
                FileAttributes attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.ReadOnly) == 0 || (attributes & FileAttributes.ReparsePoint) != 0) return;
                File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
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
