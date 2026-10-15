using System;
using System.Collections.Generic;
using System.Threading;

namespace AgentProxy
{
    internal sealed class ClaudeLink
    {
        private readonly string installDirectory;
        private readonly SynchronizationContext ui;
        private bool busy;
        private string pending;

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
            get { return busy; }
        }

        public void Refresh()
        {
            Run("claude refresh");
        }

        public void Install()
        {
            Run("claude install");
        }

        public void Uninstall()
        {
            Run("claude uninstall");
        }

        private void Run(string arguments)
        {
            if (busy)
            {
                pending = arguments;
                return;
            }
            busy = true;
            Notify();
            ThreadPool.QueueUserWorkItem(state =>
            {
                EngineCliResult result = EngineCli.Run(installDirectory, arguments);
                AppLog.Append("agentproxy " + arguments + ": " + result.Describe());
                EngineCliResult status = EngineCli.Run(installDirectory, "claude status");
                ui.Post(ignored => Apply(result, status), null);
            });
        }

        private void Apply(EngineCliResult action, EngineCliResult status)
        {
            busy = false;
            LastProblem = action.Succeeded ? null : ProblemText(action);
            Dictionary<string, object> data = status.Succeeded ? status.Json() : null;
            if (data != null)
            {
                Installed = Flag(data, "installed");
                Complete = Flag(data, "mcp") && Flag(data, "skill") && Flag(data, "hooks");
            }
            Notify();
            if (pending == null) return;
            string next = pending;
            pending = null;
            Run(next);
        }

        private static string ProblemText(EngineCliResult action)
        {
            Dictionary<string, object> data = action.Json();
            object error;
            if (data != null && data.TryGetValue("error", out error) && error is string) return (string)error;
            return action.Describe();
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
