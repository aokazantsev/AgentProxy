using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace AgentProxy
{
    internal static class UninstallLog
    {
        private const string StepsOk = "steps: ok";
        private const string StepsPrefix = "steps: ";

        public static readonly string FilePath = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "-uninstall.log");

        public static void Start(string installDirectory)
        {
            Write("=== " + AppIdentity.Name + " " + AppIdentity.Version + " uninstall, folder " + installDirectory, false);
        }

        public static void Append(string line)
        {
            Write(line, true);
        }

        public static void Steps(List<string> problems)
        {
            Append(problems.Count == 0 ? StepsOk : StepsPrefix + string.Join("; ", problems.ToArray()));
        }

        public static bool StepsSucceeded()
        {
            try
            {
                foreach (string line in File.ReadAllLines(FilePath, Encoding.UTF8))
                {
                    if (line.EndsWith(StepsOk, StringComparison.Ordinal)) return true;
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
            return false;
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
    }
}
