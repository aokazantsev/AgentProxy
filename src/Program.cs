using System;
using System.Runtime.ExceptionServices;
using System.Security;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class Program
    {
        [STAThread]
        private static int Main()
        {
            AppDomain.CurrentDomain.UnhandledException += OnUnhandledException;
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += OnThreadException;
            bool isFirstInstance;
            using (new Mutex(true, AppIdentity.SingleInstanceMutex, out isFirstInstance))
            {
                if (!isFirstInstance)
                {
                    AppLog.Append("another instance is running, exit");
                    return 0;
                }
                UserDataPaths.Ensure();
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new TrayApplication());
            }
            return 0;
        }

        private static void OnThreadException(object sender, ThreadExceptionEventArgs e)
        {
            CrashReport.Write("ui thread", e.Exception, false);
        }

        [HandleProcessCorruptedStateExceptions]
        [SecurityCritical]
        private static void OnUnhandledException(object sender, UnhandledExceptionEventArgs e)
        {
            CrashReport.Write("unhandled", e.ExceptionObject as Exception, e.IsTerminating);
        }
    }
}
