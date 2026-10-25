using System;
using System.Globalization;
using System.IO;
using System.Threading;

namespace AgentProxy
{
    internal static class UpdateWatcher
    {
        private const int FirstCheckMs = 2 * 60 * 1000;
        private const int TickIntervalMs = 60 * 60 * 1000;
        private static readonly TimeSpan CheckEvery = TimeSpan.FromDays(1);
        private static readonly string StampPath = Path.Combine(AppIdentity.DataDirectory, "update-check.txt");
        private static Timer timer;
        private static Action<string> notify;
        private static DateTime failureLogged;

        public static void Start(Action<string> onNewVersion)
        {
            notify = onNewVersion;
            timer = new Timer(state => Tick(), null, FirstCheckMs, TickIntervalMs);
        }

        private static void Tick()
        {
            try
            {
                Check();
            }
            catch (Exception error)
            {
                AppLog.Append("auto update check failed: " + UpdateChecker.DescribeForLog(error));
            }
        }

        private static void Check()
        {
            if (!AutoUpdateCheck.IsEnabled) return;
            DateTime last = LastCheck();
            DateTime now = DateTime.UtcNow;
            if (!IsDue(last, now)) return;
            UpdateRelease release;
            try
            {
                release = UpdateChecker.FetchLatest();
            }
            catch (Exception error)
            {
                if (failureLogged != DateTime.Today) AppLog.Append("auto update check failed: " + UpdateChecker.DescribeForLog(error));
                failureLogged = DateTime.Today;
                return;
            }
            failureLogged = DateTime.MinValue;
            SaveCheck(now);
            if (release.Version <= UpdateChecker.CurrentVersion)
            {
                AppLog.Append("auto update check: installed " + AppIdentity.Version + " is the latest");
                return;
            }
            AppLog.Append("auto update check: version " + release.VersionText + " is available");
            Usage.Hit("update.found_auto");
            notify(release.VersionText);
        }

        internal static bool IsDue(DateTime last, DateTime now)
        {
            return last > now || now - last >= CheckEvery;
        }

        private static DateTime LastCheck()
        {
            try
            {
                DateTime stamp;
                if (File.Exists(StampPath) && DateTime.TryParseExact(File.ReadAllText(StampPath).Trim(), "o", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out stamp)) return stamp.ToUniversalTime();
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
            return DateTime.MinValue;
        }

        private static void SaveCheck(DateTime now)
        {
            try
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllText(StampPath, now.ToString("o", CultureInfo.InvariantCulture));
            }
            catch (IOException error)
            {
                AppLog.Append("auto update check: stamp not saved: " + error.Message);
            }
            catch (UnauthorizedAccessException error)
            {
                AppLog.Append("auto update check: stamp not saved: " + error.Message);
            }
        }
    }
}
