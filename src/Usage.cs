using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using Microsoft.Win32;

namespace AgentProxy
{
    internal static class Usage
    {
        private const string Endpoint = AppIdentity.SiteUrl + "usage";
        private const int FirstSendMs = 60 * 1000;
        private const int SendIntervalMs = 60 * 60 * 1000;
        private const int TimeoutMs = 10 * 1000;
        private const int KeepDays = 6;
        private const string DayFormat = "yyyy-MM-dd";

        private static readonly string IdPath = Path.Combine(AppIdentity.DataDirectory, "usage-id.txt");
        private static readonly string DaysPath = Path.Combine(AppIdentity.DataDirectory, "usage-days.txt");
        private static readonly object Sync = new object();
        private static readonly Dictionary<string, UsageDay> Days = new Dictionary<string, UsageDay>();
        private static readonly Dictionary<string, string> State = new Dictionary<string, string>();
        private static Timer timer;
        private static bool loaded;
        private static string lastReadFailure;
        private static string installId;
        private static string lastOutcome;

        public static void Start()
        {
            Hit("app.start");
            timer = new Timer(state => Send(), null, FirstSendMs, SendIntervalMs);
        }

        public static void Hit(string name)
        {
            Add(name, 1);
        }

        public static void Add(string name, int times)
        {
            if (times <= 0) return;
            lock (Sync)
            {
                if (!UsageConsent.IsGiven)
                {
                    Forget();
                    return;
                }
                Load();
                UsageDay day = DayEntry(Today());
                day.Version = AppIdentity.Version;
                int count;
                day.Events.TryGetValue(name, out count);
                day.Events[name] = count + times;
                Touch(day);
                Save();
            }
        }

        public static void SetState(string key, object value)
        {
            string text = value is bool ? ((bool)value ? "true" : "false") : Convert.ToString(value, CultureInfo.InvariantCulture).ToLowerInvariant();
            lock (Sync)
            {
                string known;
                if (State.TryGetValue(key, out known) && known == text) return;
                State[key] = text;
                UsageDay today;
                if (Days.TryGetValue(Today(), out today)) Touch(today);
            }
        }

        public static void SetConsent(bool given)
        {
            lock (Sync)
            {
                UsageConsent.Set(given);
                if (!given) Forget();
            }
        }

        private static void Send()
        {
            try
            {
                SendPending();
            }
            catch (Exception error)
            {
                Report("failed: " + error.Message);
            }
        }

        private static void SendPending()
        {
            if (!UsageConsent.IsGiven)
            {
                Forget();
                return;
            }
            var packets = new List<KeyValuePair<string, string>>();
            var revisions = new Dictionary<string, int>();
            lock (Sync)
            {
                Load();
                if (!loaded) return;
                string today = Today();
                if (!Days.ContainsKey(today))
                {
                    DayEntry(today);
                    Save();
                }
                foreach (KeyValuePair<string, UsageDay> pair in Days)
                {
                    if (pair.Value.Sent) continue;
                    packets.Add(new KeyValuePair<string, string>(pair.Key, Packet(pair.Key, pair.Value)));
                    revisions[pair.Key] = pair.Value.Revision;
                }
            }
            foreach (KeyValuePair<string, string> packet in packets)
            {
                if (!UsageConsent.IsGiven) return;
                int status = Post(packet.Value);
                Report(status == 200 ? "sent" : "answer " + status);
                if (!IsFinalAnswer(status)) return;
                MarkSent(packet.Key, revisions[packet.Key]);
            }
        }

        internal static bool IsFinalAnswer(int status)
        {
            return status == 200 || status == 400 || status == 413;
        }

        internal static void MarkSent(string dayKey, int revision)
        {
            lock (Sync)
            {
                UsageDay day;
                if (Days.TryGetValue(dayKey, out day) && day.Revision == revision)
                {
                    day.Sent = true;
                    Save();
                }
            }
        }

        private static void Forget()
        {
            lock (Sync)
            {
                Days.Clear();
                loaded = false;
            }
        }

        private static int Post(string json)
        {
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            byte[] bytes = new UTF8Encoding(false).GetBytes(json);
            var request = (HttpWebRequest)WebRequest.Create(Endpoint);
            request.Method = "POST";
            request.ContentType = "application/json; charset=utf-8";
            request.UserAgent = AppIdentity.Name + "/" + AppIdentity.Version;
            request.Timeout = TimeoutMs;
            request.ReadWriteTimeout = TimeoutMs;
            request.ContentLength = bytes.Length;
            try
            {
                using (Stream stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
                using (var response = (HttpWebResponse)request.GetResponse()) return (int)response.StatusCode;
            }
            catch (WebException error)
            {
                var response = error.Response as HttpWebResponse;
                if (response == null) throw;
                using (response) return (int)response.StatusCode;
            }
        }

        private static void Report(string outcome)
        {
            if (outcome == lastOutcome) return;
            lastOutcome = outcome;
            AppLog.Append("usage statistics: " + outcome);
        }

        private static string Packet(string dayKey, UsageDay day)
        {
            var body = new StringBuilder("{");
            AppendPair(body, "id", InstallId());
            body.Append(',');
            AppendPair(body, "day", dayKey);
            body.Append(',');
            AppendPair(body, "version", day.Version);
            body.Append(',');
            AppendPair(body, "windows", WindowsVersion());
            body.Append(",\"events\":{");
            bool first = true;
            foreach (KeyValuePair<string, int> pair in day.Events)
            {
                if (!first) body.Append(',');
                first = false;
                AppendString(body, pair.Key);
                body.Append(':').Append(pair.Value.ToString(CultureInfo.InvariantCulture));
            }
            body.Append("},\"state\":{");
            first = true;
            foreach (KeyValuePair<string, string> pair in dayKey == Today() ? State : new Dictionary<string, string>())
            {
                if (!first) body.Append(',');
                first = false;
                AppendPair(body, pair.Key, pair.Value);
            }
            body.Append("}}");
            return body.ToString();
        }

        private static string WindowsVersion()
        {
            try
            {
                using (RegistryKey key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                {
                    if (key != null && key.GetValue("CurrentMajorVersionNumber") is int)
                    {
                        return key.GetValue("CurrentMajorVersionNumber") + "." + key.GetValue("CurrentMinorVersionNumber") + "."
                            + key.GetValue("CurrentBuildNumber") + "." + (key.GetValue("UBR") ?? 0);
                    }
                }
            }
            catch (System.Security.SecurityException)
            {
            }
            return Environment.OSVersion.Version.ToString();
        }

        private static void AppendPair(StringBuilder body, string name, string value)
        {
            AppendString(body, name);
            body.Append(':');
            AppendString(body, value);
        }

        private static void AppendString(StringBuilder body, string value)
        {
            body.Append('"');
            foreach (char symbol in value ?? "")
            {
                if (symbol == '"' || symbol == '\\') body.Append('\\').Append(symbol);
                else if (symbol < ' ') body.Append("\\u").Append(((int)symbol).ToString("x4", CultureInfo.InvariantCulture));
                else body.Append(symbol);
            }
            body.Append('"');
        }

        private static string Today()
        {
            return DateTime.Now.ToString(DayFormat, CultureInfo.InvariantCulture);
        }

        private static UsageDay DayEntry(string key)
        {
            UsageDay day;
            if (!Days.TryGetValue(key, out day))
            {
                day = new UsageDay { Version = AppIdentity.Version };
                Days[key] = day;
            }
            return day;
        }

        private static void Touch(UsageDay day)
        {
            day.Sent = false;
            day.Revision++;
        }

        private static string InstallId()
        {
            if (installId != null) return installId;
            try
            {
                if (File.Exists(IdPath))
                {
                    string stored = File.ReadAllText(IdPath).Trim();
                    Guid parsed;
                    if (Guid.TryParseExact(stored, "N", out parsed)) installId = parsed.ToString("N");
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
            if (installId != null) return installId;
            installId = Guid.NewGuid().ToString("N");
            try
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllText(IdPath, installId);
            }
            catch (IOException error)
            {
                AppLog.Append("usage statistics: id not saved: " + error.Message);
            }
            catch (UnauthorizedAccessException error)
            {
                AppLog.Append("usage statistics: id not saved: " + error.Message);
            }
            return installId;
        }

        private static void Load()
        {
            if (loaded) return;
            string[] lines;
            try
            {
                lines = File.Exists(DaysPath) ? File.ReadAllLines(DaysPath, Encoding.UTF8) : new string[0];
            }
            catch (IOException error)
            {
                ReadFailed(error.Message);
                return;
            }
            catch (UnauthorizedAccessException error)
            {
                ReadFailed(error.Message);
                return;
            }
            loaded = true;
            var current = new Dictionary<string, UsageDay>(Days);
            Days.Clear();
            foreach (string line in lines)
            {
                string[] parts = line.Split('\t');
                DateTime date;
                if (parts.Length != 4 || !DateTime.TryParseExact(parts[0], DayFormat, CultureInfo.InvariantCulture, DateTimeStyles.None, out date)) continue;
                UsageDay day = DayEntry(parts[0]);
                day.Sent = parts[1] == "1";
                day.Version = parts[3];
                foreach (string item in parts[2].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
                {
                    int equals = item.IndexOf('=');
                    int count;
                    if (equals <= 0 || !int.TryParse(item.Substring(equals + 1), NumberStyles.None, CultureInfo.InvariantCulture, out count)) continue;
                    day.Events[item.Substring(0, equals)] = count;
                }
            }
            foreach (KeyValuePair<string, UsageDay> pair in current)
            {
                UsageDay day = DayEntry(pair.Key);
                foreach (KeyValuePair<string, int> item in pair.Value.Events)
                {
                    int count;
                    day.Events.TryGetValue(item.Key, out count);
                    day.Events[item.Key] = count + item.Value;
                }
                Touch(day);
            }
        }

        private static void ReadFailed(string message)
        {
            if (message != lastReadFailure) AppLog.Append("usage statistics: counters not read: " + message);
            lastReadFailure = message;
        }

        private static void Save()
        {
            if (!loaded || !UsageConsent.IsGiven) return;
            DateTime oldest = DateTime.Today.AddDays(-KeepDays);
            var stale = new List<string>();
            var lines = new List<string>();
            foreach (KeyValuePair<string, UsageDay> pair in Days)
            {
                DateTime date = DateTime.ParseExact(pair.Key, DayFormat, CultureInfo.InvariantCulture);
                if (date < oldest)
                {
                    stale.Add(pair.Key);
                    continue;
                }
                var items = new List<string>();
                foreach (KeyValuePair<string, int> item in pair.Value.Events) items.Add(item.Key + "=" + item.Value.ToString(CultureInfo.InvariantCulture));
                lines.Add(pair.Key + "\t" + (pair.Value.Sent ? "1" : "0") + "\t" + string.Join(",", items.ToArray()) + "\t" + pair.Value.Version);
            }
            foreach (string key in stale) Days.Remove(key);
            try
            {
                Directory.CreateDirectory(AppIdentity.DataDirectory);
                File.WriteAllLines(DaysPath, lines.ToArray(), new UTF8Encoding(false));
            }
            catch (IOException error)
            {
                Report("counters not saved: " + error.Message);
            }
            catch (UnauthorizedAccessException error)
            {
                Report("counters not saved: " + error.Message);
            }
        }
    }
}
