using System;
using System.Collections.Generic;
using System.IO;

namespace AgentProxy.Tests
{
    internal static class Tests
    {
        private static int failed;
        private static int passed;

        private static int Main()
        {
            Run("restart delays grow and cap", RestartDelaysGrowAndCap);
            Run("restart failures reset after stable run", RestartResetsAfterStableRun);
            Run("busy port waits longer and maps to state", BusyPortPolicy);
            Run("settings parse values and fallbacks", SettingsParse);
            Run("settings keep unknown keys on save", SettingsKeepUnknownKeys);
            Run("settings validation", SettingsValidation);
            Run("engine status parse", EngineStatusParse);
            Run("last crash block is extracted", LastCrashExtracted);
            Run("github release with digest", ReleaseWithDigest);
            Run("github release without digest", ReleaseWithoutDigest);
            Run("github release without asset fails", ReleaseWithoutAsset);
            Run("sha256 of file", Sha256OfFile);
            Run("silent setup defaults", SilentDefaults);
            Run("silent setup overrides", SilentOverrides);
            Run("silent setup rejects unknown", SilentRejectsUnknown);
            Run("dash arguments become slash arguments", DashArguments);
            Run("readme silent commands parse with real options", ReadmeSilentCommands);
            Run("download verification", DownloadVerification);
            Run("previous install location comparison", PreviousLocation);
            Run("nested install locations", NestedLocations);
            Run("install target overlapping data folder is refused", TargetOverlappingDataFolder);
            Run("folder contents removed except uninstaller", ContentsRemovedExceptUninstaller);
            Run("install folder recognition", InstallFolderRecognition);
            Run("release versions compare numerically", ReleaseVersionsCompare);
            Run("install target refusals", TargetRefusals);
            Run("uninstaller owns registration", OwnsRegistration);
            Run("json writer keeps doubles and refuses dates", JsonWriterNumbersAndDates);
            Run("json writer round trip", JsonWriterRoundTrip);
            Run("cleaner removes only our entries", CleanerRemovesOnlyOurs);
            Run("engine starts with tray start info and stops by token", EngineStartsWithTrayStartInfo);
            Console.WriteLine(passed + " passed, " + failed + " failed");
            return failed == 0 ? 0 : 1;
        }

        private static void Run(string name, Action test)
        {
            try
            {
                test();
                passed++;
            }
            catch (Exception error)
            {
                failed++;
                Console.WriteLine("FAILED " + name + ": " + error.Message);
            }
        }

        private static void Equal<T>(T expected, T actual, string what)
        {
            if (!EqualityComparer<T>.Default.Equals(expected, actual))
            {
                throw new InvalidOperationException(what + ": expected <" + expected + ">, got <" + actual + ">");
            }
        }

        private static void True(bool condition, string what)
        {
            if (!condition) throw new InvalidOperationException(what);
        }

        private static void Throws<T>(Action action, string what) where T : Exception
        {
            try
            {
                action();
            }
            catch (T)
            {
                return;
            }
            throw new InvalidOperationException(what + ": no " + typeof(T).Name);
        }

        private static void RestartDelaysGrowAndCap()
        {
            var policy = new RestartPolicy();
            var seconds = new List<int>();
            for (int i = 0; i < 7; i++) seconds.Add((int)policy.NextDelay(1, TimeSpan.FromSeconds(3)).TotalSeconds);
            Equal("2,5,10,30,60,60,60", string.Join(",", seconds), "delays");
        }

        private static void RestartResetsAfterStableRun()
        {
            var policy = new RestartPolicy();
            policy.NextDelay(1, TimeSpan.Zero);
            policy.NextDelay(1, TimeSpan.Zero);
            Equal(2, (int)policy.NextDelay(1, TimeSpan.FromMinutes(5)).TotalSeconds, "delay after stable run");
        }

        private static void BusyPortPolicy()
        {
            var policy = new RestartPolicy();
            Equal(30, (int)policy.NextDelay(RestartPolicy.ExitPortBusy, TimeSpan.Zero).TotalSeconds, "busy delay");
            Equal(0, policy.Failures, "busy port is not a failure");
            Equal(EngineState.PortBusy, RestartPolicy.StateAfterExit(3), "state 3");
            Equal(EngineState.Crashed, RestartPolicy.StateAfterExit(1), "state 1");
        }

        private static void SettingsParse()
        {
            EngineSettings parsed = EngineSettings.Parse("{\"proxy_port\": 9000, \"api_port\": 9001, \"verify_upstream\": false}");
            Equal(9000, parsed.ProxyPort, "proxy");
            Equal(9001, parsed.ApiPort, "api");
            Equal(false, parsed.VerifyUpstream, "verify");
            EngineSettings broken = EngineSettings.Parse("{broken");
            Equal(EngineSettings.DefaultProxyPort, broken.ProxyPort, "broken proxy");
            EngineSettings low = EngineSettings.Parse("{\"proxy_port\": 80, \"api_port\": \"x\"}");
            Equal(EngineSettings.DefaultProxyPort, low.ProxyPort, "low port");
            Equal(EngineSettings.DefaultApiPort, low.ApiPort, "string port");
            EngineSettings same = EngineSettings.Parse("{\"proxy_port\": 9000, \"api_port\": 9000}");
            True(same.ProxyPort != same.ApiPort, "same ports replaced");
            Equal(EngineSettings.DefaultApiPort, EngineSettings.Parse(null).ApiPort, "null json");
        }

        private static void SettingsKeepUnknownKeys()
        {
            EngineSettings parsed = EngineSettings.Parse("{\"max_flows\": 777, \"proxy_port\": 9000}");
            parsed.ProxyPort = 9100;
            EngineSettings round = EngineSettings.Parse(parsed.ToJson());
            Equal(9100, round.ProxyPort, "proxy");
            True(parsed.ToJson().Contains("\"max_flows\":777"), "unknown key kept: " + parsed.ToJson());
        }

        private static void SettingsValidation()
        {
            Equal<string>(null, EngineSettings.Validate(8890, 8891), "valid");
            True(EngineSettings.Validate(8890, 8890) != null, "same");
            True(EngineSettings.Validate(80, 8891) != null, "low");
        }

        private static void EngineStatusParse()
        {
            EngineStatus status = EngineStatus.Parse("{\"version\":\"0.1\",\"flows\":12,\"proxy\":{\"host\":\"127.0.0.1\",\"port\":8890,\"running\":true}}");
            Equal(12, status.Flows, "flows");
            Equal(true, status.ProxyRunning, "running");
            Equal<EngineStatus>(null, EngineStatus.Parse("nope"), "garbage");
        }

        private static void LastCrashExtracted()
        {
            string log = "2026-10-15 10:00:00  INFO  x: start\n2026-10-15 10:00:01  CRITICAL agentproxy: CRASH: Traceback\n  File x\nValueError: boom\n";
            string crash = EngineLogTail.LastCrash(log);
            True(crash.StartsWith("2026-10-15 10:00:01"), "starts at crash line: " + crash);
            True(crash.EndsWith("ValueError: boom"), "ends with error");
            Equal<string>(null, EngineLogTail.LastCrash("all good"), "no crash");
        }

        private const string Digest = "a3f1c0ffee00a3f1c0ffee00a3f1c0ffee00a3f1c0ffee00a3f1c0ffee00abcd";

        private static string ReleaseJson(bool withDigest, string assetName)
        {
            return "{\"tag_name\":\"v0.2\",\"assets\":[{\"name\":\"notes.txt\",\"size\":5,\"browser_download_url\":\"https://x/notes.txt\"},"
                + "{\"url\":\"u\",\"name\":\"" + assetName + "\",\"uploader\":{\"login\":\"a\"},\"size\":123456,"
                + (withDigest ? "\"digest\":\"sha256:" + Digest.ToUpperInvariant() + "\"," : "")
                + "\"download_count\":0,\"browser_download_url\":\"https://github.com/aokazantsev/AgentProxy/releases/download/v0.2/" + assetName + "\"}]}";
        }

        private static void ReleaseWithDigest()
        {
            UpdateRelease release = UpdateChecker.ParseLatest(ReleaseJson(true, "AgentProxySetup.exe"));
            Equal("0.2", release.VersionText, "version");
            Equal(123456L, release.Size, "size");
            Equal(Digest, release.Sha256, "digest lowercased");
            True(release.DownloadUrl.EndsWith("/AgentProxySetup.exe"), "url");
        }

        private static void ReleaseWithoutDigest()
        {
            UpdateRelease release = UpdateChecker.ParseLatest(ReleaseJson(false, "AgentProxySetup.exe"));
            Equal<string>(null, release.Sha256, "no digest");
            Equal(123456L, release.Size, "size");
        }

        private static void ReleaseWithoutAsset()
        {
            Throws<InvalidDataException>(() => UpdateChecker.ParseLatest(ReleaseJson(true, "OtherSetup.exe")), "missing asset");
            Throws<InvalidDataException>(() => UpdateChecker.ParseLatest("{}"), "missing tag");
        }

        private static void Sha256OfFile()
        {
            string path = Path.GetTempFileName();
            File.WriteAllText(path, "abc");
            try
            {
                Equal("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", UpdateChecker.Sha256Of(path), "sha256");
            }
            finally
            {
                File.Delete(path);
            }
        }

        private static List<SetupOption> Options()
        {
            return new List<SetupOption>
            {
                new SetupOption { Key = "claude", Checked = true },
                new SetupOption { Key = "crashReports", Checked = false },
                new SetupOption { Key = "locked", Checked = true, Enabled = false }
            };
        }

        private static void SilentDefaults()
        {
            InstallRequest request = SilentSetup.Parse(new[] { "/silent" }, Options(), @"C:\Target", true);
            Equal(@"C:\Target", request.TargetDirectory, "dir");
            Equal(true, request.EnablesAutostart, "autostart");
            Equal(true, request.Has("claude"), "claude");
            Equal(false, request.Has("crashReports"), "crash");
            Equal(false, request.Has("locked"), "disabled option");
        }

        private static void SilentOverrides()
        {
            InstallRequest request = SilentSetup.Parse(new[] { "/silent", "/dir=\"D:\\Apps\\AP\"", "/noautostart", "/option:claude=0", "/OPTION:crashReports=1" },
                Options(), @"C:\Target", true);
            Equal(@"D:\Apps\AP", request.TargetDirectory, "dir");
            Equal(false, request.EnablesAutostart, "autostart");
            Equal(false, request.Has("claude"), "claude off");
            Equal(true, request.Has("crashReports"), "crash on");
        }

        private static int FreePort()
        {
            var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
            listener.Start();
            int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
            listener.Stop();
            return port;
        }

        private static void EngineStartsWithTrayStartInfo()
        {
            string data = Path.Combine(Path.GetTempPath(), "AgentProxyTest-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(data);
            int apiPort = FreePort();
            File.WriteAllText(Path.Combine(data, "settings.json"), "{\"proxy_port\": " + FreePort() + ", \"api_port\": " + apiPort + "}");
            System.Diagnostics.ProcessStartInfo info = EngineSupervisor.EngineStartInfo(Environment.CurrentDirectory,
                System.Diagnostics.Process.GetCurrentProcess().Id);
            info.EnvironmentVariables["AGENTPROXY_DATA_DIR"] = data;
            using (System.Diagnostics.Process engine = System.Diagnostics.Process.Start(info))
            {
                engine.ErrorDataReceived += (sender, e) => { };
                engine.BeginErrorReadLine();
                try
                {
                    EngineStatus status = null;
                    DateTime deadline = DateTime.UtcNow.AddSeconds(60);
                    while (DateTime.UtcNow < deadline && (status == null || !status.ProxyRunning) && !engine.HasExited)
                    {
                        System.Threading.Thread.Sleep(500);
                        status = EngineStatus.Fetch(apiPort);
                    }
                    True(status != null && status.ProxyRunning, "engine reported running proxy");
                    string runtimeFile = Path.Combine(data, "engine-runtime.json");
                    True(EngineRuntimeInfo.RequestShutdown(apiPort, runtimeFile), "shutdown accepted");
                    True(engine.WaitForExit(20000), "engine exited after shutdown");
                    Equal(0, engine.ExitCode, "exit code");
                    True(!File.Exists(runtimeFile), "runtime file removed");
                }
                finally
                {
                    if (!engine.HasExited) engine.Kill();
                    engine.WaitForExit(10000);
                }
            }
            try
            {
                Directory.Delete(data, true);
            }
            catch (IOException)
            {
            }
        }

        private static void ReadmeSilentCommands()
        {
            List<SetupOption> options = SetupProfile.Options();
            InstallRequest plain = SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent" }), options, @"C:\T", true);
            True(plain.TargetDirectory == @"C:\T", "default dir");
            InstallRequest custom = SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent", "--noautostart", @"--dir=D:\AP",
                "--option:claude=0", "--option:crashReports=1", "/notify" }), options, @"C:\T", true);
            Equal(false, custom.Has(SetupProfile.ClaudeKey), "claude off");
            Equal(true, custom.Has(CrashReportConsent.OptionKey), "crash reports on");
            Equal(false, custom.EnablesAutostart, "autostart off");
            Equal(@"D:\AP", custom.TargetDirectory, "dir");
        }

        private static void DownloadVerification()
        {
            string path = Path.GetTempFileName();
            try
            {
                File.WriteAllText(path, "abc");
                const string abcHash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
                UpdateChecker.Verify(path, new UpdateRelease("0.2", "u", 3, abcHash));
                UpdateChecker.Verify(path, new UpdateRelease("0.2", "u", 3, null));
                True(File.Exists(path), "accepted file kept");
                Throws<IOException>(() => UpdateChecker.Verify(path, new UpdateRelease("0.2", "u", 3, new string('0', 64))), "hash mismatch");
                True(!File.Exists(path), "rejected file deleted");
                File.WriteAllText(path, "abc");
                Throws<IOException>(() => UpdateChecker.Verify(path, new UpdateRelease("0.2", "u", 4, abcHash)), "size mismatch");
                True(!File.Exists(path), "short file deleted");
            }
            finally
            {
                if (File.Exists(path)) File.Delete(path);
            }
        }

        private static void PreviousLocation()
        {
            Equal(false, SetupProfile.IsDifferentLocation(@"C:\Apps\AgentProxy", @"c:\apps\agentproxy\"), "case and slash");
            Equal(false, SetupProfile.IsDifferentLocation("C:/Apps/AgentProxy", @"C:\Apps\AgentProxy"), "forward slashes");
            Equal(true, SetupProfile.IsDifferentLocation(@"C:\Apps\AgentProxy", @"D:\AgentProxy"), "other drive");
            Equal(true, SetupProfile.IsDifferentLocation(@"C:\Apps\AgentProxy", @"C:\Apps\AgentProxy2"), "prefix only");
        }

        private static void NestedLocations()
        {
            Equal(true, SetupProfile.IsNested(@"D:\Tools", @"D:\Tools\AgentProxy"), "target inside previous");
            Equal(true, SetupProfile.IsNested(@"D:\Tools\AgentProxy\", @"d:\tools"), "previous inside target");
            Equal(true, SetupProfile.IsNested(@"D:\Tools", @"D:\Tools"), "same folder");
            Equal(false, SetupProfile.IsNested(@"D:\Tools", @"D:\Tools2\AgentProxy"), "prefix only");
            Equal(false, SetupProfile.IsNested(@"C:\Apps\AgentProxy", @"D:\AgentProxy"), "other drive");
        }

        private static void TargetOverlappingDataFolder()
        {
            True(InstallTarget.Validate(Path.Combine(AppIdentity.DataDirectory, "program")) != null, "inside data folder");
            True(InstallTarget.Validate(AppIdentity.DataDirectory) != null, "data folder itself");
            string fresh = Path.Combine(Path.GetTempPath(), "AgentProxyTarget-" + Guid.NewGuid().ToString("N"));
            Equal<string>(null, InstallTarget.Validate(fresh), "new folder elsewhere");
        }

        private static void ContentsRemovedExceptUninstaller()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyRemoval-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(root, "runtime", "python"));
            File.WriteAllText(Path.Combine(root, "runtime", "python", "python.exe"), "x");
            File.WriteAllText(Path.Combine(root, "AgentProxy.exe"), "x");
            File.WriteAllText(Path.Combine(root, "UNINSTALL.EXE"), "x");
            try
            {
                Equal<string>(null, FolderRemoval.DeleteContentsExcept(root, AppIdentity.UninstallerName), "problem");
                string[] left = Directory.GetFileSystemEntries(root);
                Equal(1, left.Length, "entries left");
                Equal("UNINSTALL.EXE", Path.GetFileName(left[0]), "kept file");
                Equal<string>(null, FolderRemoval.DeleteContentsExcept(Path.Combine(root, "missing"), AppIdentity.UninstallerName), "missing folder");
                Equal<string>(null, FolderRemoval.Delete(Path.Combine(root, "missing"), null), "missing folder delete");
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        private static void InstallFolderRecognition()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyFolder-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(root);
            try
            {
                Equal(false, InstallFolder.LooksInstalled(root), "empty folder");
                Equal(true, InstallFolder.HoldsOnlyUninstaller(root), "empty folder holds nothing foreign");
                File.WriteAllText(Path.Combine(root, AppIdentity.UninstallerName), "x");
                Equal(false, InstallFolder.LooksInstalled(root), "uninstaller without runtime");
                Equal(true, InstallFolder.HoldsOnlyUninstaller(root), "only uninstaller");
                Directory.CreateDirectory(Path.Combine(root, "runtime"));
                Equal(true, InstallFolder.LooksInstalled(root), "uninstaller with runtime");
                Equal(false, InstallFolder.HoldsOnlyUninstaller(root), "runtime left");
                Directory.Delete(Path.Combine(root, "runtime"));
                File.WriteAllText(Path.Combine(root, "notes.txt"), "x");
                Equal(false, InstallFolder.HoldsOnlyUninstaller(root), "foreign file");
                File.WriteAllText(Path.Combine(root, AppIdentity.ExecutableName), "x");
                Equal(true, InstallFolder.LooksInstalled(root), "program present");
                Equal(false, InstallFolder.HoldsOnlyUninstaller(Path.Combine(root, "missing")), "missing folder");
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        private static void TargetRefusals()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyRefuse-" + Guid.NewGuid().ToString("N"));
            string foreign = Path.Combine(root, "documents");
            string installed = Path.Combine(root, "installed");
            Directory.CreateDirectory(foreign);
            Directory.CreateDirectory(installed);
            File.WriteAllText(Path.Combine(foreign, "notes.txt"), "x");
            File.WriteAllText(Path.Combine(installed, AppIdentity.ExecutableName), "x");
            try
            {
                True(InstallTarget.Validate(foreign, null) != null, "foreign non-empty folder");
                Equal<string>(null, InstallTarget.Validate(installed, installed), "same installation");
                True(InstallTarget.Validate(@"C:\", null) != null, "drive root");
                True(InstallTarget.Validate(Path.Combine(installed, "inner"), installed) != null, "inside previous installation");
                True(InstallTarget.Validate(root, installed) != null, "around previous installation");
                Equal<string>(null, InstallTarget.Validate(Path.Combine(root, "other"), installed), "next to previous installation");
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        private static void OwnsRegistration()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyOwner-" + Guid.NewGuid().ToString("N"));
            string current = Path.Combine(root, "current");
            Directory.CreateDirectory(current);
            File.WriteAllText(Path.Combine(current, AppIdentity.ExecutableName), "x");
            try
            {
                Equal(true, InstallFolder.OwnsRegistration(null, Path.Combine(root, "old")), "no registration");
                Equal(true, InstallFolder.OwnsRegistration(current, current.ToUpperInvariant() + "\\"), "same folder");
                Equal(false, InstallFolder.OwnsRegistration(current, Path.Combine(root, "old")), "registration of moved install");
                Equal(true, InstallFolder.OwnsRegistration(Path.Combine(root, "gone"), Path.Combine(root, "old")), "dead registration");
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        private static void ReleaseVersionsCompare()
        {
            Equal(UpdateChecker.ParseVersion("0.1"), UpdateChecker.ParseVersion("v0.1.0"), "same version with tag prefix");
            True(UpdateChecker.ParseVersion("0.10") > UpdateChecker.ParseVersion("0.9"), "0.10 after 0.9");
            True(UpdateChecker.ParseVersion("1.0") > UpdateChecker.ParseVersion("0.10"), "1.0 after 0.10");
        }

        private static void JsonWriterNumbersAndDates()
        {
            var serializer = new System.Web.Script.Serialization.JavaScriptSerializer();
            object parsed = serializer.DeserializeObject("{\"x\": 1.2345678901234567e-5, \"big\": 9007199254740993, \"n\": 3}");
            string written = JsonWriter.Write(parsed);
            object again = serializer.DeserializeObject(written);
            Equal(serializer.Serialize(parsed), serializer.Serialize(again), "numbers survive: " + written);
            object dated = serializer.DeserializeObject("{\"d\": \"\\/Date(0)\\/\"}");
            Throws<ArgumentException>(() => JsonWriter.Write(dated), "date refused");
        }

        private static void JsonWriterRoundTrip()
        {
            const string source = "{\"a\": [1, 2.5, true, null, \"т\\\"x\\\\\"], \"b\": {}, \"c\": []}";
            var serializer = new System.Web.Script.Serialization.JavaScriptSerializer();
            object parsed = serializer.DeserializeObject(source);
            string written = JsonWriter.Write(parsed);
            Equal(serializer.Serialize(parsed), serializer.Serialize(serializer.DeserializeObject(written)), "round trip");
            True(written.Contains("\"т\\\"x\\\\\""), "non-ascii kept, escapes kept: " + written);
        }

        private static void CleanerRemovesOnlyOurs()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyCleaner-" + Guid.NewGuid().ToString("N"));
            string claudeDir = Path.Combine(root, ".claude");
            Directory.CreateDirectory(Path.Combine(claudeDir, "skills", "agentproxy"));
            File.WriteAllText(Path.Combine(claudeDir, "skills", "agentproxy", "SKILL.md"), "x");
            File.WriteAllText(Path.Combine(claudeDir, "settings.json"),
                "{\"model\": \"opus\", \"hooks\": {\"SessionStart\": [{\"hooks\": [{\"type\": \"command\", \"command\": \"mine.py\"}]},"
                + " {\"hooks\": [{\"type\": \"command\", \"command\": \"C:/x/python.exe -I -m agentproxy.cli hook session-start\"}]}],"
                + " \"UserPromptSubmit\": [{\"hooks\": [{\"type\": \"http\", \"url\": \"http://127.0.0.1:8891/claude/hooks/user-prompt-submit\"}]}]}}");
            string claudeJson = Path.Combine(root, ".claude.json");
            File.WriteAllText(claudeJson, "{\"numStartups\": 3, \"mcpServers\": {\"agentproxy\": {\"type\": \"http\"}, \"other\": {\"type\": \"stdio\"}}}");
            try
            {
                Equal<string>(null, new ClaudeConfigCleaner(claudeDir, claudeJson).Clean(), "no problems");
                True(!Directory.Exists(Path.Combine(claudeDir, "skills", "agentproxy")), "skill removed");
                var serializer = new System.Web.Script.Serialization.JavaScriptSerializer();
                string settings = serializer.Serialize(serializer.DeserializeObject(File.ReadAllText(Path.Combine(claudeDir, "settings.json"))));
                Equal("{\"model\":\"opus\",\"hooks\":{\"SessionStart\":[{\"hooks\":[{\"type\":\"command\",\"command\":\"mine.py\"}]}]}}", settings, "settings");
                string json = serializer.Serialize(serializer.DeserializeObject(File.ReadAllText(claudeJson)));
                Equal("{\"numStartups\":3,\"mcpServers\":{\"other\":{\"type\":\"stdio\"}}}", json, "claude.json");
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        private static void DashArguments()
        {
            string[] normalized = CommandLine.Normalize(new[] { "--silent", "-noautostart", "--option:claude=0", @"/dir=C:\X", @"C:\path" });
            Equal(@"/silent|/noautostart|/option:claude=0|/dir=C:\X|C:\path", string.Join("|", normalized), "normalized");
        }

        private static void SilentRejectsUnknown()
        {
            Throws<ArgumentException>(() => SilentSetup.Parse(new[] { "/silent", "/fast" }, Options(), "C:\\T", true), "unknown flag");
            Throws<ArgumentException>(() => SilentSetup.Parse(new[] { "/option:nope=1" }, Options(), "C:\\T", true), "unknown option");
            Throws<ArgumentException>(() => SilentSetup.Parse(new[] { "/option:claude=yes" }, Options(), "C:\\T", true), "bad value");
        }
    }
}
