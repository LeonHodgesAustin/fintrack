@echo off
:: ============================================================
:: setup_schedule.bat
:: Run this ONCE as Administrator to register the daily task.
:: After setup, manage it via Task Scheduler or:
::   schtasks /query /tn "fintrack-daily"
::   schtasks /run   /tn "fintrack-daily"   (run now to test)
::   schtasks /delete /tn "fintrack-daily" /f  (remove)
:: ============================================================

set FINTRACK_DIR=C:\projects\fintrack
set TASK_NAME=fintrack-daily
set RUN_TIME=07:00

:: Create logs directory
if not exist "%FINTRACK_DIR%\logs" mkdir "%FINTRACK_DIR%\logs"

echo Registering scheduled task: %TASK_NAME%
echo Run time: daily at %RUN_TIME%
echo Script:   %FINTRACK_DIR%\scripts\daily.bat

schtasks /create /tn "%TASK_NAME%" ^
    /tr "cmd /c \"%FINTRACK_DIR%\scripts\daily.bat\"" ^
    /sc DAILY ^
    /st %RUN_TIME% ^
    /ru "%USERNAME%" ^
    /rl HIGHEST ^
    /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Task registered successfully.
    echo.
    echo To test immediately:   schtasks /run /tn "%TASK_NAME%"
    echo To check status:       schtasks /query /tn "%TASK_NAME%"
    echo To change time:        schtasks /change /tn "%TASK_NAME%" /st HH:MM
    echo To remove:             schtasks /delete /tn "%TASK_NAME%" /f
    echo.
    echo Logs will appear in: %FINTRACK_DIR%\logs\daily.log
) else (
    echo.
    echo ERROR: Task registration failed.
    echo Make sure you are running this script as Administrator.
)
