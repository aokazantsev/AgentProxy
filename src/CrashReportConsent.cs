using System;
using System.IO;

namespace AgentProxy
{
    internal static class CrashReportConsent
    {
        public const string OptionKey = "crashReports";

        private static readonly string FilePath = Path.Combine(AppIdentity.DataDirectory, "crash-reports-consent.txt");

        public static bool IsGiven
        {
            get { return File.Exists(FilePath); }
        }

        public static void Set(bool given)
        {
            if (given)
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllText(FilePath, "Отчёты о сбоях отправляются автору. Согласие дано " + DateTime.Now.ToString("yyyy-MM-dd HH:mm") + ".");
            }
            else if (File.Exists(FilePath))
            {
                File.Delete(FilePath);
            }
        }
    }
}
