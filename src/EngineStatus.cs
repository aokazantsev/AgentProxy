using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Web.Script.Serialization;

namespace AgentProxy
{
    internal sealed class EngineStatus
    {
        private const int TimeoutMs = 2000;

        public bool ProxyRunning;
        public string Paused;
        public int Flows;
        public int Rules;
        public int RulesEnabled;
        public readonly Dictionary<string, int> ToolCalls = new Dictionary<string, int>();

        public static EngineStatus Parse(string json)
        {
            Dictionary<string, object> data;
            try
            {
                data = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
            }
            catch (ArgumentException)
            {
                return null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
            if (data == null) return null;
            var status = new EngineStatus();
            object value;
            if (data.TryGetValue("flows", out value) && value is int) status.Flows = (int)value;
            if (data.TryGetValue("proxy", out value))
            {
                var proxy = value as Dictionary<string, object>;
                object running;
                if (proxy != null && proxy.TryGetValue("running", out running) && running is bool) status.ProxyRunning = (bool)running;
                object paused;
                if (proxy != null && proxy.TryGetValue("paused", out paused)) status.Paused = paused as string;
            }
            if (data.TryGetValue("rules", out value))
            {
                var rules = value as Dictionary<string, object>;
                object count;
                if (rules != null && rules.TryGetValue("total", out count) && count is int) status.Rules = (int)count;
                if (rules != null && rules.TryGetValue("enabled", out count) && count is int) status.RulesEnabled = (int)count;
            }
            if (data.TryGetValue("tool_calls", out value))
            {
                var calls = value as Dictionary<string, object>;
                if (calls != null)
                {
                    foreach (KeyValuePair<string, object> pair in calls)
                    {
                        if (pair.Value is int) status.ToolCalls[pair.Key] = (int)pair.Value;
                    }
                }
            }
            return status;
        }

        public static EngineStatus Fetch(int apiPort)
        {
            try
            {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + apiPort + "/api/status");
                request.Timeout = TimeoutMs;
                request.ReadWriteTimeout = TimeoutMs;
                request.Proxy = null;
                using (var response = (HttpWebResponse)request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                {
                    return Parse(reader.ReadToEnd());
                }
            }
            catch (WebException)
            {
                return null;
            }
            catch (IOException)
            {
                return null;
            }
        }
    }
}
