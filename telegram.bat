@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0MemeBot.exe" (
    echo MemeBot.exe is missing. Put this BAT next to MemeBot.exe.
    pause
    exit /b 1
)
"%~dp0MemeBot.exe" --check-config
if errorlevel 1 (
    echo Please check config.ini in this folder.
    pause
    exit /b 1
)
if exist "D:\Telegram\Telegram.exe" start "" "D:\Telegram\Telegram.exe"
echo Starting MemeBot. Logs: %~dp0state
"%~dp0MemeBot.exe" %*
set "BOT_EXIT=%ERRORLEVEL%"
if not "%BOT_EXIT%"=="0" (
    echo MemeBot stopped with code %BOT_EXIT%. Check state\forwarder.log.
    echo Complete X login in the dedicated browser, then restart MemeBot.exe. Use --login-x for login only.
    pause
)
exit /b %BOT_EXIT%
