using System;
using System.Collections.Generic;
using System.Threading;

namespace AgentProxy
{
    internal sealed class ClaudeLink
    {
        private readonly string installDirectory;
        private readonly SynchronizationContext ui;
        private int busy;

        public bool? Installed { get; private set; }
        public bool Complete { get; private set; }
        public string LastProblem { get; private set; }

        public event EventHandler Changed;

        public ClaudeLink(string installDirectory)
        {
            this.installDirectory = installDirectory;
            ui = SynchronizationContext.Current;
        }

        public bool IsBusy
        {
            get { return busy != 0; }
        }

        public void Refresh()
        {
            Run("claude refresh", true);
        }

        public void Install()
        {
            Run("claude install", true);
        }

        public void Uninstall()
        {
            Run("claude uninstall", true);
        }

        private void Run(string arguments, bool thenReadStatus)
        {
            if (Interlocked.Exchange(ref busy, 1) != 0) return;
            Notify();
            ThreadPool.QueueUserWorkItem(state =>
            {
                EngineCliResult result = EngineCli.Run(installDirectory, arguments);
                AppLog.Append("agentproxy " + arguments + ": " + result.Describe());
                EngineCliResult status = thenReadStatus ? EngineCli.Run(installDirectory, "claude status") : null;
                ui.Post(ignored => Apply(result, status), null);
            });
        }

        private void Apply(EngineCliResult action, EngineCliResult status)
        {
            Interlocked.Exchange(ref busy, 0);
            LastProblem = action.Succeeded ? null : action.Describe();
            Dictionary<string, object> data = status != null && status.Succeeded ? status.Json() : null;
            if (data != null)
            {
                Installed = Flag(data, "installed");
                Complete = Flag(data, "mcp") && Flag(data, "skill") && Flag(data, "hooks");
            }
            Notify();
        }

        private static bool Flag(Dictionary<string, object> data, string key)
        {
            object value;
            return data.TryGetValue(key, out value) && value is bool && (bool)value;
        }

        private void Notify()
        {
            EventHandler handler = Changed;
            if (handler != null) handler(this, EventArgs.Empty);
        }
    }
}
