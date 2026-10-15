@echo off
setlocal
cd /d "%~dp0"
set APP=AgentProxy
set REFS=/r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll

if exist %APP%.exe del /f /q %APP%.exe >nul 2>&1
if exist %APP%.exe (
    if exist %APP%.old.exe del /f /q %APP%.old.exe >nul 2>&1
    ren %APP%.exe %APP%.old.exe
)
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /codepage:65001 /target:winexe /platform:x64 /optimize+ /out:%APP%.exe /win32manifest:src\app.manifest /win32icon:src\app.ico %REFS% /recurse:src\*.cs
if errorlevel 1 exit /b 1
copy /y src\app.config %APP%.exe.config >nul
if /i not "%1"=="nostart" start "" "%~dp0%APP%.exe"
