@echo off
setlocal

REM TradingAgents Web launcher
REM Fixed local repo path
set "WEB_DIR=D:\code\ai\trading-web"
set "WEB_URL=http://localhost:3000"

if not exist "%WEB_DIR%\package.json" (
  echo [ERROR] Could not find trading-web directory: "%WEB_DIR%"
  echo Please edit start-web.bat and set WEB_DIR to your local path.
  pause
  exit /b 1
)

pushd "%WEB_DIR%" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Failed to enter WEB_DIR: "%WEB_DIR%"
  pause
  exit /b 1
)

if not exist "package.json" (
  echo [ERROR] package.json not found in "%CD%"
  popd
  pause
  exit /b 1
)

start "" "%WEB_URL%"

where pnpm >nul 2>nul
if not errorlevel 1 (
  pnpm --dir "%WEB_DIR%" dev %*
  set "EXIT_CODE=%errorlevel%"
  popd
  if not "%EXIT_CODE%"=="0" pause
  exit /b %EXIT_CODE%
)

echo [WARN] pnpm not found, fallback to npm run dev
npm run dev -- %*
set "EXIT_CODE=%errorlevel%"
popd
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
