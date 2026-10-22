using System.Collections.Generic;
using System.Text.RegularExpressions;

namespace AgentProxy
{
    internal sealed class ToolCallUsage
    {
        public const string EventPrefix = "mcp.";

        private static readonly Regex ToolName = new Regex(@"\A[a-z0-9_]{1,44}\z");
        private Dictionary<string, int> previous = new Dictionary<string, int>();

        public int Generation { get; private set; }

        public void Reset()
        {
            previous = new Dictionary<string, int>();
            Generation++;
        }

        public bool IsCurrent(int generation)
        {
            return generation == Generation;
        }

        public Dictionary<string, int> Delta(Dictionary<string, int> current)
        {
            var added = new Dictionary<string, int>();
            var next = new Dictionary<string, int>();
            foreach (KeyValuePair<string, int> pair in current)
            {
                if (pair.Value < 0 || !ToolName.IsMatch(pair.Key)) continue;
                next[pair.Key] = pair.Value;
                int before;
                previous.TryGetValue(pair.Key, out before);
                int count = pair.Value >= before ? pair.Value - before : pair.Value;
                if (count > 0) added[EventPrefix + pair.Key] = count;
            }
            previous = next;
            return added;
        }

        public void Apply(Dictionary<string, int> current)
        {
            foreach (KeyValuePair<string, int> pair in Delta(current)) Usage.Add(pair.Key, pair.Value);
        }
    }
}
