@echo off
cd /d C:\Projects\file-finder
uv run survey_index.py --batch

:: To register as a weekly Monday 7am scheduled task (run once as admin):
::   schtasks /create /tn "Survey Index" /tr "C:\Projects\file-finder\run_index.bat" /sc weekly /d MON /st 07:00 /f
::
:: To run it immediately from Task Scheduler:
::   schtasks /run /tn "Survey Index"
::
:: To remove it:
::   schtasks /delete /tn "Survey Index" /f
