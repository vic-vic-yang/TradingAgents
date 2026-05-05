@echo off
setlocal

REM Sync local main with upstream and push to origin
REM Usage:
REM   sync-main.bat

cd /d "%~dp0"

echo [1/6] Fetch upstream...
git fetch upstream
if errorlevel 1 goto :fail

echo [2/6] Switch to main...
git checkout main
if errorlevel 1 goto :fail

echo [3/6] Rebase local changes on origin/main...
git pull --rebase origin main
if errorlevel 1 goto :fail

echo [4/6] Rebase onto upstream/main...
git rebase upstream/main
if errorlevel 1 goto :fail_rebase

echo [5/6] Push synced main to origin...
git push origin main
if errorlevel 1 goto :fail

echo [6/6] Done. main is synced with upstream and pushed to origin.
exit /b 0

:fail_rebase
echo.
echo [ERROR] Rebase stopped due to conflicts.
echo Resolve conflicts, then run:
echo   git add ^<file^>
echo   git rebase --continue
echo Or abort:
echo   git rebase --abort
pause
exit /b 1

:fail
echo.
echo [ERROR] Sync failed. Check git output above.
pause
exit /b 1
