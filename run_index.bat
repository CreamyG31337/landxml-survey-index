@echo off
:: Edit SEARCH_PATH and TITLE to match your project before registering as a scheduled task.
set SEARCH_PATH=C:\Your\Project\Folder
set TITLE=Survey File Index

cd /d %~dp0
uv run survey_index.py --path "%SEARCH_PATH%" --title "%TITLE%" --batch

:: To register as a weekly Monday 7am scheduled task (run once as admin):
::   schtasks /create /tn "Survey Index" /tr "C:\Projects\landxml-survey-index\run_index.bat" /sc weekly /d MON /st 07:00 /f
::
:: To run it immediately from Task Scheduler:
::   schtasks /run /tn "Survey Index"
::
:: To remove it:
::   schtasks /delete /tn "Survey Index" /f
