@echo off
setlocal
cd /d "%~dp0..\.."
set CSC="%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
set OUT=%~dp0obj
if not exist "%OUT%" mkdir "%OUT%"
%CSC% /nologo /codepage:65001 /target:exe /platform:x64 /nowarn:0649 /main:AgentProxy.Tests.Tests /out:"%OUT%\Tests.exe" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll /recurse:src\*.cs installer\common\*.cs installer\setup\SilentSetup.cs installer\setup\InstallRequest.cs installer\setup\SetupOption.cs installer\setup\SetupField.cs installer\setup\SetupProfile.cs installer\setup\SetupLog.cs installer\setup\InstallTarget.cs installer\setup\Installation.cs installer\setup\Payload.cs tests\csharp\*.cs
if errorlevel 1 exit /b 1
"%OUT%\Tests.exe"
