namespace AgentProxy
{
    internal static class CommandLine
    {
        public static string[] Normalize(string[] args)
        {
            var normalized = new string[args.Length];
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (arg.StartsWith("--")) arg = "/" + arg.Substring(2);
                else if (arg.StartsWith("-")) arg = "/" + arg.Substring(1);
                normalized[i] = arg;
            }
            return normalized;
        }
    }
}
