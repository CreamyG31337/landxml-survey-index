@echo off
:: Edit SEARCH_PATH and TITLE to match your project before registering as a scheduled task.
set SUBST_TARGET=C:\Users\lcolton1\OneDrive - FlatironDragados\Rutherford, Chad's files - 1645 - Strathcona Dam Upgrade
set SEARCH_PATH=S:\
set TITLE=1645 - Strathcona Dam Upgrade
:: Company logo embedded in the report header (lives locally, outside git).
set LOGO=C:\Projects\FD LOGOS\Logo Package_NO Service Mark\Horizontal Logo_Digital\Full Color Horizontally Stacked Logo\FlatironDragados_Horizontal Logo_Full Color_RGB_DIGITAL.png

:: S: is a subst drive tied to the logon session. A scheduled run may land in a
:: session where it does not exist, so recreate it if missing before indexing.
if not exist S:\ subst S: "%SUBST_TARGET%"

cd /d %~dp0
:: NOTE: SEARCH_PATH is passed unquoted. A drive root like S:\ must be unquoted -
:: "S:\" would parse the trailing \" as an escaped quote. If you point this at a
:: folder whose name has spaces, drop the trailing backslash and quote it instead.
uv run survey_index.py --path %SEARCH_PATH% --title "%TITLE%" --logo "%LOGO%" --batch

:: Register as a weekly Monday 7am task (runs as you, only when logged on - no admin needed):
::   schtasks /create /tn "Survey Index" /tr "C:\Projects\file-finder\run_index.bat" /sc weekly /d MON /st 07:00 /f
::
:: To run it immediately from Task Scheduler:
::   schtasks /run /tn "Survey Index"
::
:: To remove it:
::   schtasks /delete /tn "Survey Index" /f
