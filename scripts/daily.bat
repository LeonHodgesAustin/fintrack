@echo off
:: ============================================================
:: fintrack daily runner -- Windows
:: Registered as a Scheduled Task by setup_schedule.bat
:: ============================================================

set FINTRACK_DIR=C:\projects\fintrack
cd /d %FINTRACK_DIR%

echo [%date% %time%] Starting fintrack daily run >> logs\daily.log 2>&1

:: 1. Pull new transactions from all linked institutions
fintrack sync >> logs\daily.log 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] ERROR: sync failed >> logs\daily.log 2>&1
)

:: 2. Run alert checks and send via ntfy.sh (if NTFY_TOPIC is set)
fintrack check >> logs\daily.log 2>&1

:: 3. Push updated data to Google Sheets (if GOOGLE_SPREADSHEET_ID is set)
fintrack push >> logs\daily.log 2>&1

echo [%date% %time%] Daily run complete >> logs\daily.log 2>&1
