@echo off
rem Post one clip. Schedule with Task Scheduler (see README).
cd /d "%~dp0\.."
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if not exist logs mkdir logs
echo === %DATE% %TIME% >> logs\run.log
python -m pip install --quiet --upgrade yt-dlp >> logs\run.log 2>&1
python -m motivate run %* >> logs\run.log 2>&1
