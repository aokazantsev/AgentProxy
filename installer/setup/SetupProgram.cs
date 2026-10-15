using System;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class SetupProgram
    {
        [STAThread]
        private static int Main(string[] args)
        {
            args = CommandLine.Normalize(args);
            if (SilentSetup.IsRequested(args)) return SilentSetup.Run(args);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
            return 0;
        }
    }
}
