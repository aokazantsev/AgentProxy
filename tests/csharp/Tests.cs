using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace AgentProxy.Tests
{
    internal static class Tests
    {
        private static int failed;
        private static int passed;

        private static readonly string Sandbox = Path.Combine(Path.GetTempPath(), "AgentProxyTests-" + Guid.NewGuid().ToString("N"));

        private static int Main()
        {
            try
            {
                Isolate();
                Run("restart delays grow and cap", RestartDelaysGrowAndCap);
                Run("restart failures reset after stable run", RestartResetsAfterStableRun);
                Run("busy port waits longer and maps to state", BusyPortPolicy);
                Run("settings parse values and fallbacks", SettingsParse);
                Run("settings keep unknown keys on save", SettingsKeepUnknownKeys);
                Run("settings validation", SettingsValidation);
                Run("engine status parse", EngineStatusParse);
                Run("tool call usage counts differences", ToolCallUsageDelta);
                Run("status of previous engine launch is stale", ToolCallUsageGeneration);
                Run("last crash block is extracted", LastCrashExtracted);
                Run("github release with digest", ReleaseWithDigest);
                Run("github release without digest", ReleaseWithoutDigest);
                Run("github release without asset fails", ReleaseWithoutAsset);
                Run("sha256 of file", Sha256OfFile);
                Run("installer downloads into its own folder", InstallerPathInOwnFolder);
                Run("install status text follows setup mode", InstallStartedText);
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
                Run("dead registration is not an update", DeadRegistrationIsNotUpdate);
                Run("tray reads claude status from engine cli", EngineCliClaudeRoundTrip);
                Run("json writer keeps doubles and refuses dates", JsonWriterNumbersAndDates);
                Run("json writer round trip", JsonWriterRoundTrip);
                Run("cleaner removes only our entries", CleanerRemovesOnlyOurs);
                Run("engine starts with tray start info and stops by token", EngineStartsWithTrayStartInfo);
                Run("usage counters round trip", UsageCountersRoundTrip);
                Run("usage packet shape", UsagePacketShape);
                Run("usage without consent keeps nothing", UsageWithoutConsent);
                Run("usage consent file", UsageConsentFile);
                Run("usage counters merge after failed read", UsageMergeAfterFailedRead);
                Run("usage keeps six days and drops broken lines", UsageRetention);
                Run("usage final answers and sent revision", UsageSentRevision);
                Run("usage refusal from settings forgets at once", UsageSetConsentRefusal);
                Run("usage counters not saved after refusal", UsageSaveAfterRefusal);
                Run("usage re-enabled starts from scratch", UsageConsentReenabled);
                Run("usage state change unmarks today", UsageStateUnmarksToday);
                Run("usage send without consent keeps nothing", UsageSendWithoutConsent);
                Run("update check stamp and due time", UpdateCheckDue);
                Run("update watcher keeps switch and interval", UpdateWatcherSkips);
                Run("tool call usage reaches counters", ToolCallUsageApply);
                Run("auto update check switch", AutoUpdateCheckSwitch);
                Run("settings written only when changed or broken", SettingsNeedsWrite);
                Run("broken settings kept aside on save", SettingsBrokenKeptOnSave);
                Run("silent setup usage statistics option", SilentUsageStatistics);
                Console.WriteLine(passed + " passed, " + failed + " failed");
                return failed == 0 ? 0 : 1;
            }
            finally
            {
                RemoveSandbox();
            }
        }

        private static void Isolate()
        {
            Directory.CreateDirectory(Sandbox);
            Redirect(typeof(UserDataPaths), "Directory", null);
            Redirect(typeof(AppLog), "FilePath", "log.txt");
            Redirect(typeof(AppLog), "PreviousFilePath", "log.old.txt");
            Redirect(typeof(Usage), "IdPath", "usage-id.txt");
            Redirect(typeof(Usage), "DaysPath", "usage-days.txt");
            Redirect(typeof(UsageConsent), "FilePath", "usage-statistics.txt");
            Redirect(typeof(UsageConsent), "CountersPath", "usage-days.txt");
            Redirect(typeof(UpdateWatcher), "StampPath", "update-check.txt");
            Redirect(typeof(AutoUpdateCheck), "FilePath", "update-check-off.txt");
            Redirect(typeof(EngineSettings), "FilePath", "settings.json");
            Redirect(typeof(CrashReportConsent), "FilePath", "crash-reports-consent.txt");
            Redirect(typeof(EngineLogTail), "FilePath", "engine.log");
            Redirect(typeof(EngineRuntimeInfo), "FilePath", "engine-runtime.json");
        }

        private static void Redirect(Type type, string field, string name)
        {
            string path = name == null ? Sandbox : Path.Combine(Sandbox, name);
            FieldInfo info = type.GetField(field, BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            if (info == null) throw new InvalidOperationException("no field " + type.Name + "." + field);
            info.SetValue(null, path);
            if ((string)info.GetValue(null) != path) throw new InvalidOperationException("not redirected " + type.Name + "." + field);
        }

        private static void RemoveSandbox()
        {
            try
            {
                if (Directory.Exists(Sandbox)) Directory.Delete(Sandbox, true);
            }
            catch (IOException)
            {
            }
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
            Equal("port busy", RestartPolicy.ExitMeaning(3), "meaning 3");
            Equal("ended from outside", RestartPolicy.ExitMeaning(1073807364), "meaning killed");
            Equal(null, RestartPolicy.ExitMeaning(77), "meaning unknown");
        }

        private static void SettingsParse()
        {
            EngineSettings parsed = EngineSettings.Parse("{\"proxy_port\": 9000, \"api_port\": 9001, \"verify_upstream\": false}");
            Equal(9000, parsed.ProxyPort, "proxy");
            Equal(9001, parsed.ApiPort, "api");
            Equal(false, parsed.VerifyUpstream, "verify");
            Equal(false, parsed.YieldToCharles, "yield to charles off by default");
            Equal(true, EngineSettings.Parse("{\"yield_to_charles\": true}").YieldToCharles, "yield to charles on");
            Equal(false, EngineSettings.Parse("{\"yield_to_charles\": 1}").YieldToCharles, "yield to charles not bool");
            EngineSettings broken = EngineSettings.Parse("{broken");
            Equal(EngineSettings.DefaultProxyPort, broken.ProxyPort, "broken proxy");
            EngineSettings low = EngineSettings.Parse("{\"proxy_port\": 80, \"api_port\": \"x\"}");
            Equal(EngineSettings.DefaultProxyPort, low.ProxyPort, "low port");
            Equal(EngineSettings.DefaultApiPort, low.ApiPort, "string port");
            EngineSettings same = EngineSettings.Parse("{\"proxy_port\": 9000, \"api_port\": 9000}");
            True(same.ProxyPort != same.ApiPort, "same ports replaced");
            Equal(EngineSettings.DefaultApiPort, EngineSettings.Parse(null).ApiPort, "null json");
            True(parsed.Parsed && low.Parsed, "object read from file");
            True(!broken.Parsed && !EngineSettings.Parse(null).Parsed && !EngineSettings.Parse("null").Parsed && !EngineSettings.Parse("[1]").Parsed,
                "missing or broken file is not parsed");
        }

        private static void SettingsKeepUnknownKeys()
        {
            EngineSettings parsed = EngineSettings.Parse("{\"max_flows\": 777, \"proxy_port\": 9000}");
            parsed.ProxyPort = 9100;
            EngineSettings round = EngineSettings.Parse(parsed.ToJson());
            Equal(9100, round.ProxyPort, "proxy");
            parsed.YieldToCharles = true;
            Equal(true, EngineSettings.Parse(parsed.ToJson()).YieldToCharles, "yield to charles saved");
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
            EngineStatus counted = EngineStatus.Parse("{\"rules\":{\"total\":3,\"enabled\":2,\"active\":1},\"tool_calls\":{\"list_flows\":4,\"get_flow\":\"x\"}}");
            Equal(3, counted.Rules, "rules total");
            Equal(2, counted.RulesEnabled, "rules enabled");
            Equal(1, counted.ToolCalls.Count, "only numeric tool calls");
            Equal(4, counted.ToolCalls["list_flows"], "list_flows calls");
            Equal(0, status.ToolCalls.Count, "no tool calls field");
            Equal<string>(null, status.Paused, "not paused");
            Equal("charles", EngineStatus.Parse("{\"proxy\":{\"running\":false,\"paused\":\"charles\"}}").Paused, "paused for charles");
            Equal<string>(null, EngineStatus.Parse("{\"proxy\":{\"running\":true,\"paused\":null}}").Paused, "paused null");
        }

        private static void ToolCallUsageDelta()
        {
            var usage = new ToolCallUsage();
            Dictionary<string, int> first = usage.Delta(new Dictionary<string, int> { { "list_flows", 3 }, { "Bad-Name", 5 }, { "get_flow", 0 } });
            Equal(1, first.Count, "first snapshot keeps only valid names with calls");
            Equal(3, first["mcp.list_flows"], "first snapshot counts everything");
            Dictionary<string, int> second = usage.Delta(new Dictionary<string, int> { { "list_flows", 5 }, { "get_flow", 2 } });
            Equal(2, second["mcp.list_flows"], "growth is a difference");
            Equal(2, second["mcp.get_flow"], "new tool counted whole");
            Dictionary<string, int> restarted = usage.Delta(new Dictionary<string, int> { { "list_flows", 1 } });
            Equal(1, restarted["mcp.list_flows"], "engine restart counts current value");
            Equal(0, usage.Delta(new Dictionary<string, int> { { "list_flows", 1 } }).Count, "no change, no events");
            Equal(0, usage.Delta(new Dictionary<string, int> { { "list_flows", 1 }, { "get_flow", -1 }, { new string('a', 45), 1 } }).Count, "negative and long names skipped");
            usage.Reset();
            Equal(1, usage.Delta(new Dictionary<string, int> { { "list_flows", 1 } })["mcp.list_flows"], "reset starts over");
        }

        private static void ToolCallUsageGeneration()
        {
            var usage = new ToolCallUsage();
            int requested = usage.Generation;
            True(usage.IsCurrent(requested), "status of the running engine is current");
            usage.Reset();
            True(!usage.IsCurrent(requested), "status requested before launch is stale");
            True(usage.IsCurrent(usage.Generation), "status of the new engine is current");
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
            return "{\"tag_name\":\"v0.2\",\"assets\":[{\"name\":\"notes.txt\",\"size\":5,\"digest\":\"sha256:" + new string('b', 64) + "\",\"browser_download_url\":\"https://x/notes.txt\"},"
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
            Equal<string>(null, release.Sha256, "no digest, other asset digest not taken");
            Equal(123456L, release.Size, "size");
        }

        private static void ReleaseWithoutAsset()
        {
            Throws<InvalidDataException>(() => UpdateChecker.ParseLatest(ReleaseJson(true, "OtherSetup.exe")), "missing asset");
            Throws<InvalidDataException>(() => UpdateChecker.ParseLatest("{}"), "missing tag");
        }

        private static void InstallerPathInOwnFolder()
        {
            Equal(@"C:\D\AgentProxy-0.2\AgentProxySetup.exe", UpdateChecker.InstallerPath(@"C:\D", "0.2"), "path");
        }

        private static void InstallStartedText()
        {
            True(UpdateForm.InstallStartedText("/silent /notify").StartsWith("Устанавливаю"), "silent setup");
            True(UpdateForm.InstallStartedText("").Contains("«Установить»"), "setup window");
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
            string repository = Path.Combine(root, "repository");
            Directory.CreateDirectory(Path.Combine(repository, "runtime", "python"));
            File.WriteAllText(Path.Combine(repository, AppIdentity.ExecutableName), "x");
            File.WriteAllText(Path.Combine(repository, AppIdentity.UninstallerName), "x");
            File.WriteAllText(Path.Combine(repository, "runtime", "python", "python.exe"), "x");
            try
            {
                Equal(false, InstallFolder.IsOurs(repository, null), "built repository is not an installation");
                Equal(false, InstallFolder.IsOurs(repository, Path.Combine(root, "other")), "registered elsewhere");
                Equal(true, InstallFolder.IsOurs(repository, repository.ToUpperInvariant() + "\\"), "registered here");
                Equal(false, InstallFolder.HoldsOnlyLeftovers(root), "foreign entries are not leftovers");
                InstallFolder.Mark(repository);
                Equal(true, InstallFolder.IsOurs(repository, null), "marked by the installer");
                Equal<string>(null, FolderRemoval.DeleteContentsExcept(repository, InstallFolder.LeftoverNames), "cleanup");
                Equal(true, InstallFolder.HoldsOnlyLeftovers(repository), "uninstaller and marker left");
                Equal(true, InstallFolder.IsOurs(repository, null), "half-removed installation is still ours");
                File.WriteAllText(Path.Combine(repository, "notes.txt"), "x");
                Equal(false, InstallFolder.HoldsOnlyLeftovers(repository), "foreign file");
                Equal(false, InstallFolder.HoldsOnlyLeftovers(Path.Combine(root, "missing")), "missing folder");
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
                string built = Path.Combine(root, "built");
                Directory.CreateDirectory(built);
                File.WriteAllText(Path.Combine(built, AppIdentity.ExecutableName), "x");
                True(InstallTarget.Validate(built, null) != null, "folder with a stray AgentProxy.exe");
                InstallFolder.Mark(built);
                Equal<string>(null, InstallTarget.Validate(built, null), "folder marked by the installer");
                string leftovers = Path.Combine(root, "leftovers");
                Directory.CreateDirectory(leftovers);
                File.WriteAllText(Path.Combine(leftovers, AppIdentity.UninstallerName), "x");
                Equal<string>(null, InstallTarget.Validate(leftovers, null), "uninstaller left by an older build");
                string fresh = Path.Combine(root, "fresh", "AgentProxy");
                InstallFolder.Mark(fresh);
                Equal(true, InstallFolder.IsOurs(fresh, null), "mark creates the folder before extraction");
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

        private static void EngineCliClaudeRoundTrip()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyCli-" + Guid.NewGuid().ToString("N"));
            string claudeDir = Path.Combine(root, "claude");
            string dataDir = Path.Combine(root, "data");
            Directory.CreateDirectory(claudeDir);
            Directory.CreateDirectory(dataDir);
            string previousClaude = Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR");
            string previousData = Environment.GetEnvironmentVariable("AGENTPROXY_DATA_DIR");
            Environment.SetEnvironmentVariable("CLAUDE_CONFIG_DIR", claudeDir);
            Environment.SetEnvironmentVariable("AGENTPROXY_DATA_DIR", dataDir);
            try
            {
                string install = Environment.CurrentDirectory;
                True(EngineCli.Run(install, "claude install --no-cli").Succeeded, "install succeeded");
                EngineCliResult status = EngineCli.Run(install, "claude status");
                True(status.Succeeded, "status succeeded");
                Dictionary<string, object> json = status.Json();
                foreach (string key in new[] { "installed", "mcp", "skill", "hooks" })
                {
                    Equal(true, json != null && json.ContainsKey(key) && Equals(json[key], true), key);
                }
                True(EngineCli.Run(install, "claude uninstall --no-cli").Succeeded, "uninstall succeeded");
                Equal(false, Equals(EngineCli.Run(install, "claude status").Json()["installed"], true), "removed");
            }
            finally
            {
                Environment.SetEnvironmentVariable("CLAUDE_CONFIG_DIR", previousClaude);
                Environment.SetEnvironmentVariable("AGENTPROXY_DATA_DIR", previousData);
                Directory.Delete(root, true);
            }
        }

        private static void DeadRegistrationIsNotUpdate()
        {
            string root = Path.Combine(Path.GetTempPath(), "AgentProxyUpdate-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(root);
            try
            {
                Equal(false, SetupProfile.IsUpdate(null), "no registration");
                Equal(false, SetupProfile.IsUpdate(root), "registration without program");
                File.WriteAllText(Path.Combine(root, AppIdentity.ExecutableName), "x");
                Equal(true, SetupProfile.IsUpdate(root), "installed program");
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
            File.WriteAllText(Path.Combine(claudeDir, "skills", "agentproxy", "rules.md"), "y");
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

        private static string SandboxFile(string name)
        {
            return Path.Combine(Sandbox, name);
        }

        private static object StaticField(Type type, string name)
        {
            return type.GetField(name, BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public).GetValue(null);
        }

        private static void SetStaticField(Type type, string name, object value)
        {
            type.GetField(name, BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public).SetValue(null, value);
        }

        private static object CallStatic(Type type, string name, params object[] arguments)
        {
            try
            {
                return type.GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public).Invoke(null, arguments);
            }
            catch (TargetInvocationException error)
            {
                throw error.InnerException;
            }
        }

        private static string Today()
        {
            return DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        private static string DaysAgo(int days)
        {
            return DateTime.Today.AddDays(-days).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        private static Dictionary<string, UsageDay> UsageDays()
        {
            return (Dictionary<string, UsageDay>)StaticField(typeof(Usage), "Days");
        }

        private static void ResetUsage()
        {
            CallStatic(typeof(Usage), "Forget");
            ((Dictionary<string, string>)StaticField(typeof(Usage), "State")).Clear();
            SetStaticField(typeof(Usage), "installId", null);
            SetStaticField(typeof(Usage), "lastReadFailure", null);
            SetStaticField(typeof(Usage), "lastOutcome", null);
            foreach (string name in new[] { "usage-days.txt", "usage-id.txt", "usage-statistics.txt" })
            {
                if (File.Exists(SandboxFile(name))) File.Delete(SandboxFile(name));
            }
        }

        private static string[] DaysLines()
        {
            return File.ReadAllLines(SandboxFile("usage-days.txt"));
        }

        private static void UsageCountersRoundTrip()
        {
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            Usage.Add("mcp.list_flows", 3);
            Usage.Hit("menu.about");
            string[] lines = DaysLines();
            Equal(1, lines.Length, "one day");
            Equal(Today() + "\t0\tmenu.about=2,mcp.list_flows=3\t" + AppIdentity.Version, lines[0], "day line");
        }

        private static void UsagePacketShape()
        {
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            Usage.Hit("menu.about");
            Usage.Add("mcp.list_flows", 3);
            Usage.SetState("rules", 2);
            Usage.SetState("verify_upstream", true);
            UsageDay day = UsageDays()[Today()];
            string packet = (string)CallStatic(typeof(Usage), "Packet", Today(), day);
            Dictionary<string, object> json = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(packet);
            string id = (string)json["id"];
            True(Regex.IsMatch(id, "^[0-9a-f]{32}$"), "id: " + id);
            Equal(File.ReadAllText(SandboxFile("usage-id.txt")).Trim(), id, "id saved");
            Equal(Today(), (string)json["day"], "day");
            Equal(AppIdentity.Version, (string)json["version"], "version");
            True(Regex.IsMatch((string)json["windows"], @"^\d+(\.\d+){1,3}$"), "windows: " + json["windows"]);
            True(packet.Contains("\"events\":{\"menu.about\":2,\"mcp.list_flows\":3}"), "events: " + packet);
            True(packet.Contains("\"state\":{\"rules\":\"2\",\"verify_upstream\":\"true\"}"), "state: " + packet);
            string past = (string)CallStatic(typeof(Usage), "Packet", "2000-01-01", day);
            True(past.Contains("\"state\":{}"), "past day without state: " + past);
            Usage.SetState("idle_effect", DayOfWeek.Monday);
            Usage.SetState("adapter", "Fortinet");
            var state = (Dictionary<string, string>)StaticField(typeof(Usage), "State");
            Equal("monday", state["idle_effect"], "enum lower case");
            Equal("fortinet", state["adapter"], "text lower case");
            var odd = new UsageDay { Version = "1" };
            odd.Events["q\"b\\c\nd"] = 1;
            string escaped = (string)CallStatic(typeof(Usage), "Packet", "2000-01-01", odd);
            var parsed = (Dictionary<string, object>)new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(escaped)["events"];
            True(parsed.ContainsKey("q\"b\\c\nd"), "escaped name round trip: " + escaped);
        }

        private static void UsageWithoutConsent()
        {
            ResetUsage();
            Equal(false, UsageConsent.IsGiven, "no file, no consent");
            Equal(true, UsageConsent.InstallerDefault, "installer suggests on");
            Usage.Hit("menu.about");
            True(!File.Exists(SandboxFile("usage-days.txt")), "nothing counted without consent");
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            True(File.Exists(SandboxFile("usage-days.txt")), "counted with consent");
            UsageConsent.Set(false);
            Equal("off", File.ReadAllText(SandboxFile("usage-statistics.txt")), "refusal saved");
            True(!File.Exists(SandboxFile("usage-days.txt")), "counters removed on refusal");
            Equal(false, UsageConsent.InstallerDefault, "installer keeps refusal");
            Usage.Hit("menu.about");
            True(!File.Exists(SandboxFile("usage-days.txt")), "nothing counted after refusal");
            Equal(0, UsageDays().Count, "days forgotten");
        }

        private static void UsageConsentFile()
        {
            ResetUsage();
            File.WriteAllText(SandboxFile("usage-statistics.txt"), "on\r\n");
            Equal(true, UsageConsent.IsGiven, "on with line break");
            using (new FileStream(SandboxFile("usage-statistics.txt"), FileMode.Open, FileAccess.ReadWrite, FileShare.None))
            {
                Equal(false, UsageConsent.IsGiven, "unreadable file is no consent");
                Equal(false, UsageConsent.InstallerDefault, "unreadable file is not offered as on");
            }
            File.WriteAllText(SandboxFile("usage-statistics.txt"), "yes");
            Equal(false, UsageConsent.IsGiven, "unknown value is no consent");
            Equal(false, UsageConsent.InstallerDefault, "unknown value is not offered as on");
        }

        private static void UsageMergeAfterFailedRead()
        {
            ResetUsage();
            UsageConsent.Set(true);
            string stored = Today() + "\t1\tmenu.about=5\t0.10";
            File.WriteAllLines(SandboxFile("usage-days.txt"), new[] { stored });
            using (new FileStream(SandboxFile("usage-days.txt"), FileMode.Open, FileAccess.ReadWrite, FileShare.None))
            {
                Usage.Hit("menu.exit");
                Usage.Hit("menu.exit");
            }
            Equal(stored, string.Join("|", DaysLines()), "file untouched while unreadable");
            True(File.ReadAllText(AppLog.FilePath).Contains("usage statistics: counters not read"), "failure logged");
            Usage.Hit("menu.exit");
            Equal(Today() + "\t0\tmenu.about=5,menu.exit=3\t" + AppIdentity.Version, string.Join("|", DaysLines()), "merged");
        }

        private static void UsageRetention()
        {
            ResetUsage();
            UsageConsent.Set(true);
            File.WriteAllLines(SandboxFile("usage-days.txt"), new[]
            {
                DaysAgo(10) + "\t0\tmenu.about=1\t0.10",
                DaysAgo(6) + "\t1\tmenu.about=5,bad,x=-1\t0.10",
                "garbage line"
            });
            Usage.Hit("menu.about");
            string[] lines = DaysLines();
            Equal(2, lines.Length, "days kept: " + string.Join("|", lines));
            Equal(DaysAgo(6) + "\t1\tmenu.about=5\t0.10", lines[0], "six days ago kept as sent, broken items dropped");
            Equal(Today() + "\t0\tmenu.about=1\t" + AppIdentity.Version, lines[1], "today");
        }

        private static void UsageSentRevision()
        {
            foreach (int status in new[] { 200, 400, 413 }) True(Usage.IsFinalAnswer(status), "final " + status);
            foreach (int status in new[] { 429, 503, 500 }) True(!Usage.IsFinalAnswer(status), "retry " + status);
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            CallStatic(typeof(Usage), "Packet", Today(), UsageDays()[Today()]);
            int packed = UsageDays()[Today()].Revision;
            Usage.Hit("menu.about");
            Usage.MarkSent(Today(), packed);
            Equal(false, UsageDays()[Today()].Sent, "changed after packet is not sent");
            True(DaysLines()[0].Contains("\t0\t"), "file not marked");
            Usage.MarkSent(Today(), UsageDays()[Today()].Revision);
            Equal(true, UsageDays()[Today()].Sent, "current revision sent");
            True(DaysLines()[0].Contains("\t1\t"), "file marked");
            Usage.Hit("menu.about");
            Equal(false, UsageDays()[Today()].Sent, "new event unmarks");
        }

        private static void UsageSetConsentRefusal()
        {
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            Usage.SetConsent(false);
            Equal(0, UsageDays().Count, "days forgotten at once");
            Equal(false, (bool)StaticField(typeof(Usage), "loaded"), "counters to be read again");
            True(!File.Exists(SandboxFile("usage-days.txt")), "counters removed");
            Equal("off", File.ReadAllText(SandboxFile("usage-statistics.txt")), "refusal saved");
        }

        private static void UsageSaveAfterRefusal()
        {
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            int revision = UsageDays()[Today()].Revision;
            File.WriteAllText(SandboxFile("usage-statistics.txt"), "off");
            File.Delete(SandboxFile("usage-days.txt"));
            CallStatic(typeof(Usage), "Save");
            Usage.MarkSent(Today(), revision);
            True(!File.Exists(SandboxFile("usage-days.txt")), "nothing written back after refusal");
        }

        private static void UsageConsentReenabled()
        {
            ResetUsage();
            Usage.SetConsent(true);
            Usage.Hit("menu.about");
            Usage.Hit("menu.about");
            Usage.SetConsent(false);
            Usage.SetConsent(true);
            Usage.Hit("menu.exit");
            Equal(Today() + "\t0\tmenu.exit=1\t" + AppIdentity.Version, string.Join("|", DaysLines()), "old counters gone");
        }

        private static void UsageStateUnmarksToday()
        {
            ResetUsage();
            UsageConsent.Set(true);
            Usage.Hit("menu.about");
            Usage.MarkSent(Today(), UsageDays()[Today()].Revision);
            int revision = UsageDays()[Today()].Revision;
            Usage.SetState("rules", 2);
            Equal(false, UsageDays()[Today()].Sent, "state change unmarks");
            True(UsageDays()[Today()].Revision > revision, "revision grows");
            Usage.MarkSent(Today(), UsageDays()[Today()].Revision);
            Usage.SetState("rules", 2);
            Equal(true, UsageDays()[Today()].Sent, "same state keeps sent");
            ResetUsage();
            UsageConsent.Set(true);
            Usage.SetState("rules", 3);
            Equal(0, UsageDays().Count, "state alone creates no day");
        }

        private static void UsageSendWithoutConsent()
        {
            ResetUsage();
            UsageDays()[Today()] = new UsageDay { Version = AppIdentity.Version };
            CallStatic(typeof(Usage), "SendPending");
            Equal(0, UsageDays().Count, "days forgotten");
            True(!File.Exists(SandboxFile("usage-days.txt")), "no counters file");
            True(!File.Exists(SandboxFile("usage-id.txt")), "no install id");
        }

        private static void UpdateCheckDue()
        {
            string stamp = SandboxFile("update-check.txt");
            if (File.Exists(stamp)) File.Delete(stamp);
            Equal(DateTime.MinValue, (DateTime)CallStatic(typeof(UpdateWatcher), "LastCheck"), "no stamp");
            DateTime now = DateTime.UtcNow;
            CallStatic(typeof(UpdateWatcher), "SaveCheck", now);
            Equal(now, (DateTime)CallStatic(typeof(UpdateWatcher), "LastCheck"), "stamp round trip");
            File.WriteAllText(stamp, "yesterday");
            Equal(DateTime.MinValue, (DateTime)CallStatic(typeof(UpdateWatcher), "LastCheck"), "garbage stamp");
            Equal(false, UpdateWatcher.IsDue(now.AddHours(-23), now), "checked 23 hours ago");
            Equal(true, UpdateWatcher.IsDue(now.AddHours(-25), now), "checked 25 hours ago");
            Equal(true, UpdateWatcher.IsDue(DateTime.MinValue, now), "never checked");
            Equal(true, UpdateWatcher.IsDue(now.AddHours(2), now), "stamp in the future");
        }

        private static void UpdateWatcherSkips()
        {
            string stamp = SandboxFile("update-check.txt");
            if (File.Exists(stamp)) File.Delete(stamp);
            AutoUpdateCheck.Set(false);
            string log = File.Exists(AppLog.FilePath) ? File.ReadAllText(AppLog.FilePath) : "";
            CallStatic(typeof(UpdateWatcher), "Check");
            True(!File.Exists(stamp), "switched off, no check");
            AutoUpdateCheck.Set(true);
            CallStatic(typeof(UpdateWatcher), "SaveCheck", DateTime.UtcNow);
            string saved = File.ReadAllText(stamp);
            CallStatic(typeof(UpdateWatcher), "Check");
            Equal(saved, File.ReadAllText(stamp), "checked recently, no check");
            Equal(log, File.Exists(AppLog.FilePath) ? File.ReadAllText(AppLog.FilePath) : "", "nothing logged");
        }

        private static void ToolCallUsageApply()
        {
            ResetUsage();
            UsageConsent.Set(true);
            new ToolCallUsage().Apply(new Dictionary<string, int> { { "list_flows", 3 } });
            True(DaysLines()[0].Contains("mcp.list_flows=3"), "counted: " + DaysLines()[0]);
            ResetUsage();
            UsageConsent.Set(false);
            new ToolCallUsage().Apply(new Dictionary<string, int> { { "list_flows", 3 } });
            True(!File.Exists(SandboxFile("usage-days.txt")), "nothing counted without consent");
        }

        private static void AutoUpdateCheckSwitch()
        {
            string flag = SandboxFile("update-check-off.txt");
            if (File.Exists(flag)) File.Delete(flag);
            Equal(true, AutoUpdateCheck.IsEnabled, "enabled by default");
            AutoUpdateCheck.Set(false);
            Equal(false, AutoUpdateCheck.IsEnabled, "disabled");
            True(File.Exists(flag), "flag file written");
            AutoUpdateCheck.Set(true);
            Equal(true, AutoUpdateCheck.IsEnabled, "enabled again");
            True(!File.Exists(flag), "flag file removed");
        }

        private static void SettingsNeedsWrite()
        {
            File.WriteAllText(EngineSettings.FilePath, "{\"proxy_port\": 9000, \"api_port\": 9001, \"verify_upstream\": false}");
            EngineSettings stored = EngineSettings.Load();
            Equal(false, stored.NeedsWrite(9000, 9001, false, false), "same values");
            Equal(true, stored.NeedsWrite(9002, 9001, false, false), "proxy port changed");
            Equal(true, stored.NeedsWrite(9000, 9002, false, false), "api port changed");
            Equal(true, stored.NeedsWrite(9000, 9001, true, false), "verify changed");
            Equal(true, stored.NeedsWrite(9000, 9001, false, true), "yield to charles changed");
            File.WriteAllText(EngineSettings.FilePath, "{broken");
            Equal(true, EngineSettings.Load().NeedsWrite(EngineSettings.DefaultProxyPort, EngineSettings.DefaultApiPort, true, false), "broken file rewritten");
            File.Delete(EngineSettings.FilePath);
            Equal(false, EngineSettings.Load().NeedsWrite(EngineSettings.DefaultProxyPort, EngineSettings.DefaultApiPort, true, false), "no file, defaults");
        }

        private static void SettingsBrokenKeptOnSave()
        {
            File.WriteAllText(EngineSettings.FilePath, "{broken");
            try
            {
                EngineSettings broken = EngineSettings.Load();
                broken.ProxyPort = 9100;
                broken.Save();
                Equal("{broken", File.ReadAllText(EngineSettings.FilePath + ".broken"), "broken copy");
                EngineSettings saved = EngineSettings.Load();
                True(saved.Parsed, "new file parsed");
                Equal(9100, saved.ProxyPort, "new value");
            }
            finally
            {
                File.Delete(EngineSettings.FilePath);
                File.Delete(EngineSettings.FilePath + ".broken");
            }
        }

        private static void SilentUsageStatistics()
        {
            ResetUsage();
            List<SetupOption> options = SetupProfile.Options();
            InstallRequest plain = SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent" }), options, @"C:\T", true);
            Equal(true, plain.Has(UsageConsent.OptionKey), "on by default");
            InstallRequest refused = SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent", "--option:usageStatistics=0" }), options, @"C:\T", true);
            Equal(false, refused.Has(UsageConsent.OptionKey), "turned off");
            UsageConsent.Set(false);
            Equal(false, SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent" }), SetupProfile.Options(), @"C:\T", true).Has(UsageConsent.OptionKey), "previous refusal kept");
            UsageConsent.Set(true);
            Equal(true, SilentSetup.Parse(CommandLine.Normalize(new[] { "--silent" }), SetupProfile.Options(), @"C:\T", true).Has(UsageConsent.OptionKey), "previous consent kept");
        }
    }
}
