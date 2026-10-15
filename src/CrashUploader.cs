using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Text;

namespace AgentProxy
{
    internal static class CrashUploader
    {
        private const string Endpoint = AppIdentity.SiteUrl + "crash-report";
        private const int TimeoutMs = 5000;
        private const int LogTailBytes = 100 * 1024;

        public static string Send(string source, string error, string logPath)
        {
            try
            {
                ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
                var body = new StringBuilder("{");
                AppendField(body, "app", AppIdentity.Name, false);
                AppendField(body, "version", AppIdentity.Version, true);
                AppendField(body, "os", Environment.OSVersion.VersionString, true);
                AppendField(body, "clr", Environment.Version.ToString(), true);
                AppendField(body, "time", DateTime.Now.ToString("o", CultureInfo.InvariantCulture), true);
                AppendField(body, "source", source, true);
                AppendField(body, "error", error, true);
                AppendField(body, "log", Tail(logPath), true);
                body.Append('}');
                byte[] bytes = new UTF8Encoding(false).GetBytes(body.ToString());

                var request = (HttpWebRequest)WebRequest.Create(Endpoint);
                request.Method = "POST";
                request.ContentType = "application/json; charset=utf-8";
                request.UserAgent = AppIdentity.Name + "/" + AppIdentity.Version;
                request.Timeout = TimeoutMs;
                request.ReadWriteTimeout = TimeoutMs;
                request.ContentLength = bytes.Length;
                using (Stream stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    return null;
                }
            }
            catch (Exception failure)
            {
                return failure.Message;
            }
        }

        private static string Tail(string path)
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) return "";
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                long start = Math.Max(0, stream.Length - LogTailBytes);
                stream.Seek(start, SeekOrigin.Begin);
                var buffer = new byte[stream.Length - start];
                int read = stream.Read(buffer, 0, buffer.Length);
                return new UTF8Encoding(false).GetString(buffer, 0, read);
            }
        }

        private static void AppendField(StringBuilder body, string name, string value, bool comma)
        {
            if (comma) body.Append(',');
            body.Append('"').Append(name).Append("\":\"");
            foreach (char symbol in value ?? "")
            {
                if (symbol == '"') body.Append("\\\"");
                else if (symbol == '\\') body.Append("\\\\");
                else if (symbol == '\n') body.Append("\\n");
                else if (symbol == '\r') body.Append("\\r");
                else if (symbol == '\t') body.Append("\\t");
                else if (symbol < ' ') body.Append("\\u").Append(((int)symbol).ToString("x4", CultureInfo.InvariantCulture));
                else body.Append(symbol);
            }
            body.Append('"');
        }
    }
}
