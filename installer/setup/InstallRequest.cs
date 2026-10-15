using System.Collections.Generic;

namespace AgentProxy
{
    internal sealed class InstallRequest
    {
        public string TargetDirectory;
        public bool EnablesAutostart;
        public readonly HashSet<string> CheckedOptions = new HashSet<string>();
        public readonly Dictionary<string, string> Values = new Dictionary<string, string>();

        public bool Has(string key)
        {
            return CheckedOptions.Contains(key);
        }

        public string Value(string key)
        {
            string value;
            return Values.TryGetValue(key, out value) ? value : "";
        }
    }
}
