using System;
using System.IO;
using System.Threading;

namespace AgentProxy
{
    internal static class FolderRemoval
    {
        private const int Attempts = 20;
        private const int RetryMs = 500;

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
