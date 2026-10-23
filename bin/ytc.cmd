@echo off
rem yt-coach script launcher for Windows cmd/PowerShell: ytc <script> [args...]
rem   e.g. bin\ytc.cmd ui --no-open
rem - prefers the plugin .venv Python, falls back to python on PATH
rem - passes the current folder (or YT_COACH_HOME) as the work folder
setlocal
set "ROOT=%~dp0.."
if not defined YT_COACH_HOME set "YT_COACH_HOME=%CD%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

if "%~1"=="" (
  echo {"error": "usage", "message": "usage: ytc ^<script^> [args...]"}
  exit /b 1
)

set "SCRIPT=%~1"
set "ARGS="
shift
:collect
if "%~1"=="" goto run
set ARGS=%ARGS% %1
shift
goto collect

:run
cd /d "%ROOT%"
"%PY%" -m "scripts.%SCRIPT%"%ARGS%
exit /b %ERRORLEVEL%
