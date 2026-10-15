using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;

namespace AgentProxy
{
    internal static class Payload
    {
        public const string ResourceName = AppIdentity.Name + ".Payload.zip";

        public static long Extract(string target, Action<int, string> report, int fromPercent, int toPercent)
        {
            Directory.CreateDirectory(target);
            string root = target.TrimEnd('\\') + "\\";
            long totalBytes = 0;
            using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream(ResourceName))
            {
                if (payload == null) throw new InvalidOperationException("В установщике нет архива программы — он собран неправильно.");
                using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                {
                    int index = 0;
                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        index++;
                        string relative = entry.FullName.Replace('/', '\\');
                        string destination = Path.GetFullPath(Path.Combine(target, relative));
                        if (!destination.StartsWith(root, StringComparison.OrdinalIgnoreCase)) continue;
                        if (relative.EndsWith("\\"))
                        {
                            Directory.CreateDirectory(destination);
                            continue;
                        }
                        Directory.CreateDirectory(Path.GetDirectoryName(destination));
                        entry.ExtractToFile(destination, true);
                        totalBytes += entry.Length;
                        report(fromPercent + index * (toPercent - fromPercent) / archive.Entries.Count, "Распаковка: " + relative);
                    }
                }
            }
            return totalBytes;
        }
    }
}
