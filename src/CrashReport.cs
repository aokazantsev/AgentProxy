using System;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class CrashReport
    {
        private static int shown;

        public static void Write(string source, Exception error, bool terminating)
        {
            string text = error == null ? "unknown error" : error.ToString();
            AppLog.Append("CRASH (" + source + (terminating ? ", terminating" : "") + "): " + text);
            if (Interlocked.Exchange(ref shown, 1) != 0) return;
            if (CrashReportConsent.IsGiven)
            {
                string problem = CrashUploader.Send(source, text, AppLog.FilePath);
                AppLog.Append(problem == null ? "отчёт о сбое отправлен" : "отчёт о сбое не отправлен: " + problem);
            }
            try
            {
                MessageBox.Show(
                    AppIdentity.Name + " упал: " + (error == null ? "неизвестная ошибка" : error.Message)
                    + Environment.NewLine + Environment.NewLine + "Подробности в журнале: " + AppLog.FilePath,
                    AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            catch (InvalidOperationException)
            {
            }
        }
    }
}
