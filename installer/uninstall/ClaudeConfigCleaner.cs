using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace AgentProxy
{
    internal sealed class ClaudeConfigCleaner
    {
        private const string ServerName = "agentproxy";
        private const string CliModule = "agentproxy.cli";
        private const string HookPath = "/claude/hooks/user-prompt-submit";

        private readonly string claudeDirectory;
        private readonly string claudeJson;

        public ClaudeConfigCleaner(string claudeDirectory, string claudeJson)
        {
            this.claudeDirectory = claudeDirectory;
            this.claudeJson = claudeJson;
        }

        public static ClaudeConfigCleaner ForCurrentUser()
        {
            string configured = Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR");
            if (!string.IsNullOrEmpty(configured)) return new ClaudeConfigCleaner(configured, Path.Combine(configured, ".claude.json"));
            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            return new ClaudeConfigCleaner(Path.Combine(home, ".claude"), Path.Combine(home, ".claude.json"));
        }

        public string Clean()
        {
            var problems = new List<string>();
            Try(problems, "скилл", RemoveSkill);
            Try(problems, "хуки", RemoveHooks);
            Try(problems, "MCP-сервер", RemoveServer);
            return problems.Count == 0 ? null : string.Join("; ", problems.ToArray());
        }

        private static void Try(List<string> problems, string what, Action action)
        {
            try
            {
                action();
            }
            catch (Exception error)
            {
                problems.Add(what + ": " + error.Message);
            }
        }

        private void RemoveSkill()
        {
            string folder = Path.Combine(claudeDirectory, "skills", ServerName);
            if (Directory.Exists(folder)) Directory.Delete(folder, true);
        }

        private void RemoveHooks()
        {
            string path = Path.Combine(claudeDirectory, "settings.json");
            Dictionary<string, object> settings = Load(path);
            if (settings == null) return;
            object hooksValue;
            if (!settings.TryGetValue("hooks", out hooksValue)) return;
            var hooks = hooksValue as Dictionary<string, object>;
            if (hooks == null) return;
            bool changed = false;
            foreach (string eventName in new List<string>(hooks.Keys))
            {
                var groups = hooks[eventName] as object[];
                if (groups == null) continue;
                var keptGroups = new List<object>();
                foreach (object groupValue in groups)
                {
                    var group = groupValue as Dictionary<string, object>;
                    object entriesValue;
                    object[] entries = group != null && group.TryGetValue("hooks", out entriesValue) ? entriesValue as object[] : null;
                    if (entries == null)
                    {
                        keptGroups.Add(groupValue);
                        continue;
                    }
                    var kept = new List<object>();
                    foreach (object entry in entries)
                    {
                        if (IsOurs(entry as Dictionary<string, object>)) changed = true;
                        else kept.Add(entry);
                    }
                    if (kept.Count == 0) continue;
                    group["hooks"] = kept.ToArray();
                    keptGroups.Add(group);
                }
                if (keptGroups.Count == 0) hooks.Remove(eventName);
                else hooks[eventName] = keptGroups.ToArray();
            }
            if (!changed) return;
            if (hooks.Count == 0) settings.Remove("hooks");
            Save(path, settings);
        }

        private void RemoveServer()
        {
            Dictionary<string, object> data = Load(claudeJson);
            if (data == null) return;
            object serversValue;
            var servers = data.TryGetValue("mcpServers", out serversValue) ? serversValue as Dictionary<string, object> : null;
            if (servers == null || !servers.Remove(ServerName)) return;
            Save(claudeJson, data);
        }

        public static bool IsOurs(Dictionary<string, object> hook)
        {
            if (hook == null) return false;
            object value;
            if (hook.TryGetValue("command", out value) && value is string && ((string)value).Contains(CliModule)) return true;
            return hook.TryGetValue("url", out value) && value is string && ((string)value).EndsWith(HookPath, StringComparison.Ordinal);
        }

        private static Dictionary<string, object> Load(string path)
        {
            if (!File.Exists(path)) return null;
            string text = File.ReadAllText(path, Encoding.UTF8);
            if (string.IsNullOrWhiteSpace(text)) return null;
            var serializer = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            var data = serializer.DeserializeObject(text) as Dictionary<string, object>;
            if (data == null) throw new InvalidDataException(path + " не объект JSON");
            return data;
        }

        private static void Save(string path, Dictionary<string, object> data)
        {
            string text = JsonWriter.Write(data) + "\n";
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            {
                File.WriteAllText(path, text, new UTF8Encoding(false));
                return;
            }
            string temporary = path + ".agentproxy-tmp";
            File.WriteAllText(temporary, text, new UTF8Encoding(false));
            File.Replace(temporary, path, null);
        }
    }
}
