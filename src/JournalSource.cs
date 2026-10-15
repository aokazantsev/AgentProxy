using System;
using System.IO;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class JournalSource
    {
        public readonly string Path;
        public readonly TextBox Box;
        private long lastLength = -1;
        private DateTime lastWrite;

        public JournalSource(string path, TextBox box)
        {
            Path = path;
            Box = box;
        }

        public void Refresh(bool force, int tailBytes)
        {
            var info = new FileInfo(Path);
            long length = info.Exists ? info.Length : 0;
            DateTime written = info.Exists ? info.LastWriteTimeUtc : DateTime.MinValue;
            if (!force && length == lastLength && written == lastWrite) return;
            lastLength = length;
            lastWrite = written;
            string text = info.Exists ? EngineLogTail.Read(Path, tailBytes) : "Журнала пока нет: " + Path;
            text = text.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
            bool atEnd = force || Box.SelectionStart >= Box.TextLength - 1;
            Box.Text = text;
            if (atEnd)
            {
                Box.SelectionStart = Box.TextLength;
                Box.ScrollToCaret();
            }
        }
    }
}
