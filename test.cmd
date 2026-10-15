@echo off
setlocal
cd /d "%~dp0"
py "%~dp0tools\runtime.py" fetch
if errorlevel 1 exit /b 1
py "%~dp0tools\test.py" %*
if errorlevel 1 exit /b 1
call "%~dp0tests\csharp\run.cmd"
