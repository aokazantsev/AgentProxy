using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace AgentProxy
{
    internal static class UninstallLog
    {
        public static readonly string FilePath = Path.Combine(Path.GetTempPath(), AppIdentity.Name + "-uninstall.log");

        public static void Write(List<string> problems)
        {
            try
            {
                string text = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + AppIdentity.Name + " " + AppIdentity.Version
                    + " uninstall: " + (problems.Count == 0 ? "ok" : string.Join("; ", problems.ToArray())) + Environment.NewLine;
                File.WriteAllText(FilePath, text, new UTF8Encoding(false));
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
