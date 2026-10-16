using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace AgentProxy
{
    internal static class JsonWriter
    {
        private const string Indent = "  ";

        public static string Write(object value)
        {
            var output = new StringBuilder();
            WriteValue(output, value, 0);
            return output.ToString();
        }

        private static void WriteValue(StringBuilder output, object value, int depth)
        {
            if (value == null) output.Append("null");
            else if (value is string) WriteString(output, (string)value);
            else if (value is bool) output.Append((bool)value ? "true" : "false");
            else if (value is double) output.Append(((double)value).ToString("R", CultureInfo.InvariantCulture));
            else if (value is DateTime) throw new ArgumentException("date values are not written back");
            else if (value is IDictionary<string, object>) WriteObject(output, (IDictionary<string, object>)value, depth);
            else if (value is IEnumerable) WriteArray(output, (IEnumerable)value, depth);
            else if (value is IFormattable) output.Append(((IFormattable)value).ToString(null, CultureInfo.InvariantCulture));
            else throw new ArgumentException("unsupported JSON value " + value.GetType().Name);
        }

        private static void WriteObject(StringBuilder output, IDictionary<string, object> values, int depth)
        {
            if (values.Count == 0)
            {
                output.Append("{}");
                return;
            }
            output.Append("{\n");
            int index = 0;
            foreach (KeyValuePair<string, object> pair in values)
            {
                Pad(output, depth + 1);
                WriteString(output, pair.Key);
                output.Append(": ");
                WriteValue(output, pair.Value, depth + 1);
                output.Append(++index < values.Count ? ",\n" : "\n");
            }
            Pad(output, depth);
            output.Append('}');
        }

        private static void WriteArray(StringBuilder output, IEnumerable values, int depth)
        {
            var items = new List<object>();
            foreach (object item in values) items.Add(item);
            if (items.Count == 0)
            {
                output.Append("[]");
                return;
            }
            output.Append("[\n");
            for (int i = 0; i < items.Count; i++)
            {
                Pad(output, depth + 1);
                WriteValue(output, items[i], depth + 1);
                output.Append(i + 1 < items.Count ? ",\n" : "\n");
            }
            Pad(output, depth);
            output.Append(']');
        }

        private static void Pad(StringBuilder output, int depth)
        {
            for (int i = 0; i < depth; i++) output.Append(Indent);
        }

        private static void WriteString(StringBuilder output, string value)
        {
            output.Append('"');
            foreach (char symbol in value)
            {
                if (symbol == '"') output.Append("\\\"");
                else if (symbol == '\\') output.Append("\\\\");
                else if (symbol == '\n') output.Append("\\n");
                else if (symbol == '\r') output.Append("\\r");
                else if (symbol == '\t') output.Append("\\t");
                else if (symbol < ' ') output.Append("\\u").Append(((int)symbol).ToString("x4", CultureInfo.InvariantCulture));
                else output.Append(symbol);
            }
            output.Append('"');
        }
    }
}
