@echo off
setlocal

REM One-click launcher: start API + Web automatically
REM Usage:
REM   start-all.bat
REM   start-all.bat --check

set "PROJECT_DIR=D:\code\ai\TradingAgents"
set "WEB_DIR=D:\code\ai\trading-web"
set "WEB_URL=http://localhost:3000"

if not exist "%PROJECT_DIR%\web_api\__init__.py" (
  echo [ERROR] TradingAgents project directory is invalid: "%PROJECT_DIR%"
  echo Please edit start-all.bat and set PROJECT_DIR to your local path.
  pause
  exit /b 1
)

if not exist "%WEB_DIR%\package.json" (
  echo [ERROR] trading-web directory is invalid: "%WEB_DIR%"
  echo Please edit start-all.bat and set WEB_DIR to your local path.
  pause
  exit /b 1
)

if /I "%~1"=="--check" (
  echo [OK] Project dir: "%PROJECT_DIR%"
  echo [OK] Web dir: "%WEB_DIR%"
  exit /b 0
)

echo Starting TradingAgents API...
start "TradingAgents API" cmd /k "cd /d \"%PROJECT_DIR%\" && python -m web_api"

echo Starting TradingAgents Web...
where pnpm >nul 2>nul
if not errorlevel 1 (
  start "TradingAgents Web" cmd /k "cd /d \"%WEB_DIR%\" && pnpm --dir \"%WEB_DIR%\" dev"
) else (
  start "TradingAgents Web" cmd /k "cd /d \"%WEB_DIR%\" && npm run dev"
)

start "" "%WEB_URL%"

echo Done. API and Web are launching in separate windows.
exit /b 0
