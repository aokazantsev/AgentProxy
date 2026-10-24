using System.Runtime.InteropServices;

namespace AgentProxy
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct ScrollInfo
    {
        public int Size;
        public int Mask;
        public int Min;
        public int Max;
        public int Page;
        public int Position;
        public int TrackPosition;
    }
}
