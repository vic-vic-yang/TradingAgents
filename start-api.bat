@echo off
setlocal

REM TradingAgents Web API launcher
REM Works even if this .bat is copied to Desktop:
REM 1) Prefer script directory when it looks like project root
REM 2) Fallback to fixed local repo path
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR="

if exist "%SCRIPT_DIR%web_api\__init__.py" set "PROJECT_DIR=%SCRIPT_DIR%"
if not defined PROJECT_DIR if exist "D:\code\ai\TradingAgents\web_api\__init__.py" set "PROJECT_DIR=D:\code\ai\TradingAgents"

if not defined PROJECT_DIR (
  echo [ERROR] Could not find TradingAgents project directory.
  echo Please edit start-api.bat and set PROJECT_DIR to your local path.
  pause
  exit /b 1
)

cd /d "%PROJECT_DIR%"
python -m web_api %*
if errorlevel 1 pause
