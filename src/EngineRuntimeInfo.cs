using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Web.Script.Serialization;

namespace AgentProxy
{
    internal static class EngineRuntimeInfo
    {
        private const string TokenHeader = "X-AgentProxy-Token";
        private const int TimeoutMs = 3000;

        public static readonly string FilePath = UserDataPaths.File("engine-runtime.json");

        public static string Token(string runtimeFile)
        {
            try
            {
                var data = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(runtimeFile, Encoding.UTF8));
                object token;
                return data != null && data.TryGetValue("token", out token) ? token as string : null;
            }
            catch (IOException)
            {
                return null;
            }
            catch (UnauthorizedAccessException)
            {
                return null;
            }
            catch (ArgumentException)
            {
                return null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }

        public static bool RequestShutdown(int apiPort, string runtimeFile)
        {
            string token = Token(runtimeFile);
            if (token == null) return false;
            try
            {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + apiPort + "/api/shutdown");
                request.Method = "POST";
                request.Timeout = TimeoutMs;
                request.Proxy = null;
                request.Headers[TokenHeader] = token;
                request.ContentLength = 0;
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    return response.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (WebException)
            {
                return false;
            }
        }
    }
}
