using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AgentProxy
{
    internal sealed class JournalSource
    {
        private const int HorizontalBar = 0;
        private const int VerticalBar = 1;
        private const int ScrollAll = 0x17;

        public readonly string Path;
        public readonly TextBox Box;
        private long lastLength = -1;
        private DateTime lastWrite;
        private bool followsEnd = true;
        private bool endPending;

        public JournalSource(string path, TextBox box)
        {
            Path = path;
            Box = box;
        }

        public void Refresh(bool force, int tailBytes)
        {
            if (Box.Visible) followsEnd = Box.SelectionLength == 0 && IsScrolledToEnd();
            var info = new FileInfo(Path);
            long length = info.Exists ? info.Length : 0;
            DateTime written = info.Exists ? info.LastWriteTimeUtc : DateTime.MinValue;
            if (!force && length == lastLength && written == lastWrite) return;
            if (!force && !followsEnd) return;
            lastLength = length;
            lastWrite = written;
            string text = info.Exists ? EngineLogTail.Read(Path, tailBytes) : "Журнала пока нет: " + Path;
            text = text.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
            Box.Text = text;
            followsEnd = true;
            if (Box.Visible) ShowEnd();
            else endPending = true;
        }

        public void Shown()
        {
            if (!endPending) return;
            endPending = false;
            ShowEnd();
        }

        private void ShowEnd()
        {
            Box.SelectionStart = Math.Max(0, Box.GetFirstCharIndexFromLine(Box.GetLineFromCharIndex(Box.TextLength)));
            Box.ScrollToCaret();
        }

        public void Leaving()
        {
            followsEnd = Box.SelectionLength == 0 && IsScrolledToEnd();
        }

        private bool IsScrolledToEnd()
        {
            if (Box.TextLength == 0 || Box.ClientSize.Height == 0) return Box.TextLength == 0;
            ScrollInfo vertical;
            if (Read(VerticalBar, out vertical) && vertical.Position + Math.Max(vertical.Page, 1) <= vertical.Max) return false;
            ScrollInfo horizontal;
            return !Read(HorizontalBar, out horizontal) || horizontal.Position <= horizontal.Min;
        }

        private bool Read(int bar, out ScrollInfo info)
        {
            info = new ScrollInfo { Mask = ScrollAll };
            info.Size = Marshal.SizeOf(info);
            return GetScrollInfo(Box.Handle, bar, ref info);
        }

        [DllImport("user32.dll")]
        private static extern bool GetScrollInfo(IntPtr window, int bar, ref ScrollInfo info);
    }
}
