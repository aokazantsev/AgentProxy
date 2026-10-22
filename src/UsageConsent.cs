using System;
using System.IO;

namespace AgentProxy
{
    internal static class UsageConsent
    {
        public const string OptionKey = "usageStatistics";
        private const string Given = "on";
        private const string Refused = "off";

        private static readonly string FilePath = Path.Combine(AppIdentity.DataDirectory, "usage-statistics.txt");
        private static readonly string CountersPath = Path.Combine(AppIdentity.DataDirectory, "usage-days.txt");

        public static bool IsGiven
        {
            get { return Read() == Given; }
        }

        public static bool InstallerDefault
        {
            get
            {
                string choice = Read();
                return choice == null || choice == Given;
            }
        }

        public static void Set(bool given)
        {
            Directory.CreateDirectory(AppIdentity.DataDirectory);
            File.WriteAllText(FilePath, given ? Given : Refused);
            if (!given && File.Exists(CountersPath)) File.Delete(CountersPath);
        }

        private static string Read()
        {
            try
            {
                return File.Exists(FilePath) ? File.ReadAllText(FilePath).Trim() : null;
            }
            catch (IOException)
            {
                return Refused;
            }
            catch (UnauthorizedAccessException)
            {
                return Refused;
            }
        }
    }
}
