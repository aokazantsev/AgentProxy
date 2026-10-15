@echo off
setlocal
cd /d "%~dp0"
set APP=AgentProxy
set CSC="%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
set REFS=/r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll
set PAYLOAD=installer\obj\payload.zip
set SRCLIST=%~dp0installer\obj\src.rsp
set OUTDIR=%~dp0dist

py "%~dp0tools\runtime.py" fetch
if errorlevel 1 exit /b 1
py "%~dp0tools\package.py" clean
if errorlevel 1 exit /b 1

call "%~dp0build.cmd" nostart
if errorlevel 1 exit /b 1

if not exist "%~dp0installer\obj" mkdir "%~dp0installer\obj"
if exist "%SRCLIST%" del /f /q "%SRCLIST%"
for /r "%~dp0src" %%F in (*.cs) do if /i not "%%~nxF"=="AssemblyInfo.cs" echo "%%F">>"%SRCLIST%"

%CSC% /nologo /codepage:65001 /target:winexe /platform:x64 /optimize+ /main:AgentProxy.UninstallProgram /out:Uninstall.exe /win32manifest:installer\uninstall.manifest /win32icon:src\app.ico %REFS% installer\common\*.cs installer\uninstall\*.cs @"%SRCLIST%"
if errorlevel 1 exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\make-payload.ps1 -Root "%~dp0." -Output "%~dp0%PAYLOAD%"
if errorlevel 1 exit /b 1

if not exist "%OUTDIR%" mkdir "%OUTDIR%"
%CSC% /nologo /codepage:65001 /target:winexe /platform:x64 /optimize+ /nowarn:0649 /main:AgentProxy.SetupProgram /out:"%OUTDIR%\%APP%Setup.exe" /win32manifest:installer\setup.manifest /win32icon:src\app.ico %REFS% /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll /resource:%PAYLOAD%,%APP%.Payload.zip installer\common\*.cs installer\setup\*.cs @"%SRCLIST%"
if errorlevel 1 exit /b 1

del /f /q %APP%.exe %APP%.exe.config Uninstall.exe >nul 2>&1
py "%~dp0tools\package.py" checksum "%OUTDIR%\%APP%Setup.exe"
echo Installer: %OUTDIR%\%APP%Setup.exe
