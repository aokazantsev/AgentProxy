using System;

namespace AgentProxy
{
    internal sealed class RestartPolicy
    {
        public const int ExitPortBusy = 3;
        public const int ExitDataDir = 4;
        private static readonly int[] DelaysSeconds = { 2, 5, 10, 30, 60 };
        private const int PortBusyDelaySeconds = 30;
        private static readonly TimeSpan StableRun = TimeSpan.FromMinutes(2);

        private int failures;

        public int Failures
        {
            get { return failures; }
        }

        public TimeSpan NextDelay(int exitCode, TimeSpan ranFor)
        {
            if (ranFor >= StableRun) failures = 0;
            if (exitCode == ExitPortBusy) return TimeSpan.FromSeconds(PortBusyDelaySeconds);
            int index = Math.Min(failures, DelaysSeconds.Length - 1);
            failures++;
            return TimeSpan.FromSeconds(DelaysSeconds[index]);
        }

        public void Reset()
        {
            failures = 0;
        }

        public static string ExitMeaning(int exitCode)
        {
            switch (exitCode)
            {
                case 0: return "normal stop";
                case 1: return "crash";
                case ExitPortBusy: return "port busy";
                case ExitDataDir: return "data folder unavailable";
                case 1073807364: return "ended from outside";
                case -1073741510: return "ended from outside";
                default: return null;
            }
        }

        public static EngineState StateAfterExit(int exitCode)
        {
            if (exitCode == ExitPortBusy) return EngineState.PortBusy;
            return EngineState.Crashed;
        }
    }
}
