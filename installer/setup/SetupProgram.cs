using System;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class SetupProgram
    {
        [STAThread]
        private static int Main(string[] args)
        {
            SystemTools.LoadDllsOnlyFromSystem();
            args = CommandLine.Normalize(args);
            bool silent = SilentSetup.IsRequested(args);
            bool isFirstInstance;
            using (new Mutex(true, AppIdentity.SetupMutex, out isFirstInstance))
            {
                if (!isFirstInstance)
                {
                    SetupLog.AppendWithoutHeader("another " + AppIdentity.Name + " " + AppIdentity.Version + " setup is already running, this one exits");
                    if (!silent) MessageBox.Show("Установка " + AppIdentity.Name + " уже идёт.", AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return SilentSetup.ExitFailed;
                }
                if (silent) return SilentSetup.Run(args);
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new SetupForm());
                return 0;
            }
        }
    }
}
