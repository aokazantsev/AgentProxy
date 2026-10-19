import os
import re

from agentproxy import VERSION, changelog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8-sig") as stream:
        return stream.read()


def test_app_identity_matches_engine():
    match = re.search(r'Version = "([0-9.]+)"', read("src", "AppIdentity.cs"))
    assert match.group(1) == VERSION


def test_assembly_infos_match_engine():
    for path in (("src",), ("installer", "setup"), ("installer", "uninstall")):
        text = read(*path, "AssemblyInfo.cs")
        assert 'AssemblyInformationalVersion("{0}")'.format(VERSION) in text, path
        numeric = ".".join((VERSION.split(".") + ["0", "0", "0"])[:4])
        assert 'AssemblyVersion("{0}")'.format(numeric) in text, path
        assert 'AssemblyFileVersion("{0}")'.format(numeric) in text, path


def test_installers_restrict_dll_search_first():
    for path in (("installer", "setup", "SetupProgram.cs"), ("installer", "uninstall", "UninstallProgram.cs")):
        body = read(*path).split("Main(string[] args)", 1)[1]
        first = body.split("{", 1)[1].strip().splitlines()[0].strip()
        assert first == "SystemTools.LoadDllsOnlyFromSystem();", path


def test_changelog_top_entry_is_current_version():
    entries = changelog.parse(read("CHANGELOG.md"))
    assert entries and entries[0]["version"] == VERSION
    assert entries[0]["lines"]
