using System.Collections.Generic;

namespace AgentProxy
{
    internal sealed class SetupField
    {
        public string Key;
        public string Label;
        public string Value;
        public string Hint;
        public readonly List<string> Suggestions = new List<string>();
    }
}
