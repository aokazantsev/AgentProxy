using System;
using System.Threading;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class SetupProgram
    {
        private const string SingleInstanceMutex = "AgentProxy.Setup";

        [STAThread]
        private static int Main(string[] args)
        {
            args = CommandLine.Normalize(args);
            bool silent = SilentSetup.IsRequested(args);
            bool isFirstInstance;
            using (new Mutex(true, SingleInstanceMutex, out isFirstInstance))
            {
                if (!isFirstInstance)
                {
                    SetupLog.Append("another setup is already running");
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
