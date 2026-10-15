using System;

namespace AgentProxy
{
    internal sealed class UpdateRelease
    {
        public readonly Version Version;
        public readonly string VersionText;
        public readonly string DownloadUrl;
        public readonly long Size;
        public readonly string Sha256;

        public UpdateRelease(string versionText, string downloadUrl, long size, string sha256)
        {
            VersionText = versionText;
            Version = UpdateChecker.ParseVersion(versionText);
            DownloadUrl = downloadUrl;
            Size = size;
            Sha256 = sha256;
        }
    }
}
