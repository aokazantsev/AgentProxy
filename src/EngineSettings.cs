using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace AgentProxy
{
    internal sealed class EngineSettings
    {
        public const int DefaultProxyPort = 8890;
        public const int DefaultApiPort = 8891;
        public const int MinPort = 1024;
        public const int MaxPort = 65535;

        public static readonly string FilePath = UserDataPaths.File("settings.json");

        public int ProxyPort = DefaultProxyPort;
        public int ApiPort = DefaultApiPort;
        public bool VerifyUpstream = true;
        public bool YieldToCharles;
        public bool Parsed;
        private Dictionary<string, object> raw = new Dictionary<string, object>();

        public static EngineSettings Load()
        {
            return Parse(File.Exists(FilePath) ? ReadText(FilePath) : null);
        }

        public static EngineSettings Parse(string json)
        {
            var settings = new EngineSettings();
            if (string.IsNullOrWhiteSpace(json)) return settings;
            try
            {
                Dictionary<string, object> values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
                if (values == null) return settings;
                settings.raw = values;
                settings.Parsed = true;
            }
            catch (ArgumentException)
            {
                return settings;
            }
            catch (InvalidOperationException)
            {
                return settings;
            }
            settings.ProxyPort = Port(settings.raw, "proxy_port", DefaultProxyPort);
            settings.ApiPort = Port(settings.raw, "api_port", DefaultApiPort);
            object verify;
            if (settings.raw.TryGetValue("verify_upstream", out verify) && verify is bool) settings.VerifyUpstream = (bool)verify;
            object yieldToCharles;
            if (settings.raw.TryGetValue("yield_to_charles", out yieldToCharles) && yieldToCharles is bool) settings.YieldToCharles = (bool)yieldToCharles;
            if (settings.ProxyPort == settings.ApiPort)
            {
                settings.ProxyPort = DefaultProxyPort;
                settings.ApiPort = DefaultApiPort;
            }
            return settings;
        }

        public static string Validate(int proxyPort, int apiPort)
        {
            if (proxyPort < MinPort || proxyPort > MaxPort || apiPort < MinPort || apiPort > MaxPort)
            {
                return "Порт должен быть от " + MinPort + " до " + MaxPort + ".";
            }
            if (proxyPort == apiPort) return "Порт прокси и порт API должны различаться.";
            return null;
        }

        public bool NeedsWrite(int proxyPort, int apiPort, bool verifyUpstream, bool yieldToCharles)
        {
            return proxyPort != ProxyPort || apiPort != ApiPort || verifyUpstream != VerifyUpstream || yieldToCharles != YieldToCharles
                || !Parsed && File.Exists(FilePath);
        }

        public string ToJson()
        {
            var values = new Dictionary<string, object>(raw);
            values["proxy_port"] = ProxyPort;
            values["api_port"] = ApiPort;
            values["verify_upstream"] = VerifyUpstream;
            values["yield_to_charles"] = YieldToCharles;
            return new JavaScriptSerializer().Serialize(values);
        }

        public void Save()
        {
            UserDataPaths.Ensure();
            if (!Parsed && File.Exists(FilePath)) File.Copy(FilePath, FilePath + ".broken", true);
            string temporary = FilePath + ".tmp";
            File.WriteAllText(temporary, ToJson(), new UTF8Encoding(false));
            if (File.Exists(FilePath)) File.Replace(temporary, FilePath, null);
            else File.Move(temporary, FilePath);
            Parsed = true;
        }

        private static int Port(Dictionary<string, object> values, string key, int fallback)
        {
            object value;
            if (!values.TryGetValue(key, out value) || !(value is int)) return fallback;
            int port = (int)value;
            return port >= MinPort && port <= MaxPort ? port : fallback;
        }

        private static string ReadText(string path)
        {
            try
            {
                return File.ReadAllText(path, Encoding.UTF8);
            }
            catch (IOException)
            {
                return null;
            }
            catch (UnauthorizedAccessException)
            {
                return null;
            }
        }
    }
}
