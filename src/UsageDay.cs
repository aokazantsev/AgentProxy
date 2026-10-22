using System.Collections.Generic;

namespace AgentProxy
{
    internal sealed class UsageDay
    {
        public readonly Dictionary<string, int> Events = new Dictionary<string, int>();
        public bool Sent;
        public int Revision;
        public string Version;
    }
}
