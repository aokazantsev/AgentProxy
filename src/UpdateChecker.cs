using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace AgentProxy
{
    internal static class UpdateChecker
    {
        private const string GitHubPrefix = "https://github.com/";
        private const string ApiPrefix = "https://api.github.com/repos/";
        private const int RequestTimeoutMs = 15 * 1000;
        private const int DownloadTimeoutMs = 120 * 1000;
        private const int BufferSize = 81920;
        private static readonly object DownloadLock = new object();
        private static readonly Guid DownloadsFolderId = new Guid("374DE290-123F-4565-9164-39C4925E467B");

        public static Version CurrentVersion
        {
            get { return ParseVersion(AppIdentity.Version); }
        }

        public static Version ParseVersion(string text)
        {
            var parsed = new Version(text.Trim().TrimStart('v', 'V'));
            return new Version(parsed.Major, parsed.Minor, Math.Max(0, parsed.Build), Math.Max(0, parsed.Revision));
        }

        public static string DescribeForLog(Exception error)
        {
            var network = error as WebException;
            if (network == null) return error.ToString();
            var response = network.Response as HttpWebResponse;
            return "network error " + network.Status + (response != null ? ", HTTP " + (int)response.StatusCode : "");
        }

        public static UpdateRelease FetchLatest()
        {
            string url = ApiPrefix + AppIdentity.GitHubUrl.Substring(GitHubPrefix.Length) + "/releases/latest";
            HttpWebRequest request = CreateRequest(url, RequestTimeoutMs);
            request.Accept = "application/vnd.github+json";
            string json;
            using (var response = (HttpWebResponse)request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
            {
                json = reader.ReadToEnd();
            }
            return ParseLatest(json);
        }

        public static UpdateRelease ParseLatest(string json)
        {
            Match tag = Regex.Match(json, "\"tag_name\"\\s*:\\s*\"(v?[0-9][0-9.]*)\"");
            if (!tag.Success) throw new InvalidDataException("в ответе GitHub нет номера версии");
            string assetName = AppIdentity.Name + "Setup.exe";
            Match asset = Regex.Match(json, "\"size\"\\s*:\\s*([0-9]+)[^{}]*?(?:\"digest\"\\s*:\\s*\"sha256:([0-9a-fA-F]{64})\"[^{}]*?)?\"browser_download_url\"\\s*:\\s*\"([^\"]*/" + Regex.Escape(assetName) + ")\"");
            if (!asset.Success) throw new InvalidDataException("в последнем релизе нет файла " + assetName);
            return new UpdateRelease(tag.Groups[1].Value.TrimStart('v', 'V'), asset.Groups[3].Value,
                long.Parse(asset.Groups[1].Value, CultureInfo.InvariantCulture),
                asset.Groups[2].Success ? asset.Groups[2].Value.ToLowerInvariant() : null);
        }

        public static void Verify(string path, UpdateRelease release)
        {
            string problem = null;
            if (release.Size > 0 && new FileInfo(path).Length != release.Size) problem = "файл скачался не полностью";
            else if (release.Sha256 != null && Sha256Of(path) != release.Sha256) problem = "контрольная сумма скачанного файла не совпала с опубликованной на GitHub";
            if (problem == null) return;
            File.Delete(path);
            throw new IOException(problem);
        }

        public static string Sha256Of(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 hash = SHA256.Create())
            {
                return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            }
        }

        public static string InstallerPath(string downloadsDirectory, string versionText)
        {
            return Path.Combine(Path.Combine(downloadsDirectory, AppIdentity.Name + "-" + versionText), AppIdentity.Name + "Setup.exe");
        }

        public static string Download(UpdateRelease release, out bool reused)
        {
            string target = InstallerPath(DownloadsDirectory(), release.VersionText);
            lock (DownloadLock)
            {
                reused = IsVerifiedCopy(target, release);
                if (reused) return target;
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                string partial = target + ".part";
                HttpWebRequest request = CreateRequest(release.DownloadUrl, DownloadTimeoutMs);
                using (var response = (HttpWebResponse)request.GetResponse())
                using (Stream source = response.GetResponseStream())
                using (var output = new FileStream(partial, FileMode.Create, FileAccess.Write))
                {
                    source.CopyTo(output, BufferSize);
                }
                Verify(partial, release);
                if (File.Exists(target)) File.Delete(target);
                File.Move(partial, target);
                return target;
            }
        }

        private static bool IsVerifiedCopy(string path, UpdateRelease release)
        {
            if (release.Sha256 == null || !File.Exists(path)) return false;
            try
            {
                return Sha256Of(path) == release.Sha256;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private static HttpWebRequest CreateRequest(string url, int timeoutMs)
        {
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = AppIdentity.Name + "/" + AppIdentity.Version;
            request.Timeout = timeoutMs;
            request.ReadWriteTimeout = timeoutMs;
            return request;
        }

        private static string DownloadsDirectory()
        {
            Guid folderId = DownloadsFolderId;
            IntPtr pathPointer;
            if (SHGetKnownFolderPath(ref folderId, 0, IntPtr.Zero, out pathPointer) == 0)
            {
                try
                {
                    return Marshal.PtrToStringUni(pathPointer);
                }
                finally
                {
                    Marshal.FreeCoTaskMem(pathPointer);
                }
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
        }

        [DllImport("shell32.dll")]
        private static extern int SHGetKnownFolderPath(ref Guid folderId, uint flags, IntPtr token, out IntPtr path);
    }
}
