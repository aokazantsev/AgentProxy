using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AgentProxy
{
    internal static class TrayIcons
    {
        private static readonly Color ErrorColor = Color.FromArgb(218, 54, 51);
        private static readonly Color WarningColor = Color.FromArgb(232, 140, 0);
        private static readonly Color IdleColor = Color.FromArgb(120, 128, 140);
        private static readonly Dictionary<EngineState, Icon> Cache = new Dictionary<EngineState, Icon>();

        public static Icon For(EngineState state)
        {
            Icon icon;
            if (Cache.TryGetValue(state, out icon)) return icon;
            icon = Create(state);
            Cache[state] = icon;
            return icon;
        }

        public static bool IsProblem(EngineState state)
        {
            return state == EngineState.Crashed || state == EngineState.PortBusy || state == EngineState.NotResponding
                || state == EngineState.ProxyNotListening;
        }

        private static Icon Create(EngineState state)
        {
            int side = SystemInformation.SmallIconSize.Width;
            using (var source = new Icon(Icon.ExtractAssociatedIcon(Application.ExecutablePath), side, side))
            using (var bitmap = new Bitmap(side, side))
            {
                using (Graphics graphics = Graphics.FromImage(bitmap))
                {
                    graphics.SmoothingMode = SmoothingMode.AntiAlias;
                    graphics.Clear(Color.Transparent);
                    graphics.DrawIcon(source, new Rectangle(0, 0, side, side));
                    Color? badge = BadgeColor(state);
                    if (badge.HasValue) DrawBadge(graphics, side, badge.Value, IsProblem(state));
                }
                IntPtr handle = bitmap.GetHicon();
                try
                {
                    return (Icon)Icon.FromHandle(handle).Clone();
                }
                finally
                {
                    DestroyIcon(handle);
                }
            }
        }

        private static Color? BadgeColor(EngineState state)
        {
            if (IsProblem(state)) return ErrorColor;
            if (state == EngineState.Starting) return WarningColor;
            if (state == EngineState.Stopped) return IdleColor;
            return null;
        }

        private static void DrawBadge(Graphics graphics, int side, Color color, bool exclamation)
        {
            float diameter = side * 0.6f;
            var bounds = new RectangleF(side - diameter, side - diameter, diameter - 0.5f, diameter - 0.5f);
            using (var brush = new SolidBrush(color))
            using (var outline = new Pen(Color.White, Math.Max(1f, side / 16f)))
            {
                graphics.FillEllipse(brush, bounds);
                graphics.DrawEllipse(outline, bounds);
            }
            if (!exclamation) return;
            float centerX = bounds.X + bounds.Width / 2f;
            using (var pen = new Pen(Color.White, Math.Max(1.5f, side / 9f)))
            {
                pen.StartCap = LineCap.Round;
                pen.EndCap = LineCap.Round;
                graphics.DrawLine(pen, centerX, bounds.Y + bounds.Height * 0.24f, centerX, bounds.Y + bounds.Height * 0.56f);
                graphics.DrawLine(pen, centerX, bounds.Y + bounds.Height * 0.76f, centerX, bounds.Y + bounds.Height * 0.77f);
            }
        }

        [DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr handle);
    }
}
