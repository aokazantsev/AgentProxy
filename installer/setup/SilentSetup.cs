using System;
using System.Collections.Generic;
using System.IO;

namespace AgentProxy
{
    internal static class SilentSetup
    {
        public const string SilentFlag = "/silent";
        private const string DirectoryPrefix = "/dir=";
        private const string NoAutostartFlag = "/noautostart";
        private const string OptionPrefix = "/option:";
        public const int ExitOk = 0;
        public const int ExitFailed = 1;
        public const int ExitBadArguments = 2;

        public static bool IsRequested(string[] args)
        {
            foreach (string arg in args)
            {
                if (string.Equals(arg, SilentFlag, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        public static InstallRequest Parse(string[] args, List<SetupOption> options, string defaultDirectory, bool defaultAutostart)
        {
            var request = new InstallRequest { TargetDirectory = defaultDirectory, EnablesAutostart = defaultAutostart };
            var choices = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
            foreach (SetupOption option in options) choices[option.Key] = option.Checked && option.Enabled;
            foreach (string arg in args)
            {
                if (string.Equals(arg, SilentFlag, StringComparison.OrdinalIgnoreCase)) continue;
                if (string.Equals(arg, NoAutostartFlag, StringComparison.OrdinalIgnoreCase))
                {
                    request.EnablesAutostart = false;
                }
                else if (arg.StartsWith(DirectoryPrefix, StringComparison.OrdinalIgnoreCase))
                {
                    request.TargetDirectory = arg.Substring(DirectoryPrefix.Length).Trim('"');
                }
                else if (arg.StartsWith(OptionPrefix, StringComparison.OrdinalIgnoreCase))
                {
                    string[] pair = arg.Substring(OptionPrefix.Length).Split('=');
                    if (pair.Length != 2 || !choices.ContainsKey(pair[0]) || (pair[1] != "0" && pair[1] != "1"))
                    {
                        throw new ArgumentException("неизвестная опция " + arg);
                    }
                    choices[pair[0]] = pair[1] == "1";
                }
                else
                {
                    throw new ArgumentException("неизвестный аргумент " + arg);
                }
            }
            foreach (KeyValuePair<string, bool> choice in choices)
            {
                if (choice.Value) request.CheckedOptions.Add(choice.Key);
            }
            return request;
        }

        public static int Run(string[] args)
        {
            InstallRequest request;
            try
            {
                bool autostart = !SetupProfile.IsUpdate() || Autostart.IsEnabled();
                request = Parse(args, SetupProfile.Options(), InstallTarget.DefaultDirectory(), autostart);
            }
            catch (ArgumentException error)
            {
                SetupLog.Start();
                SetupLog.Append("silent install rejected: " + error.Message);
                return ExitBadArguments;
            }
            string problem = InstallTarget.Validate(request.TargetDirectory);
            if (problem != null)
            {
                SetupLog.Start();
                SetupLog.Append("silent install rejected: " + problem);
                return ExitBadArguments;
            }
            request.TargetDirectory = Path.GetFullPath(request.TargetDirectory);
            var installation = new Installation(request, (percent, message) => { });
            try
            {
                installation.Run();
            }
            catch (Exception error)
            {
                SetupLog.Append("silent install failed: " + error);
                return ExitFailed;
            }
            foreach (string note in installation.Notes) SetupLog.Append("note: " + note);
            SetupLog.Append("silent install done");
            return ExitOk;
        }
    }
}
