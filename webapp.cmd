@echo off
rem Starts the web UI with the first Python found of:
rem   1. MICROWAVE_PYTHON  -- an explicit interpreter, e.g. a venv elsewhere
rem   2. VIRTUAL_ENV       -- whichever venv is activated in this shell
rem   3. .venv\            -- the venv beside this file
rem   4. python            -- whatever is on PATH
setlocal
cd /d "%~dp0"

rem A program started before `setx` ran -- an IDE, the Claude app -- hands
rem its children the environment it started with, so a variable set since
rem (ANSYS_ANALYZE_QUEUE_PATH, AEDT's ANSYSEM*_ROOT) is missing here. Fill
rem in any that are missing from what Windows has saved, user's over the
rem machine's; variables already set are left alone.
for %%K in ("HKCU\Environment" "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment") do (
    for /f "tokens=1,2,*" %%A in ('reg query %%K 2^>nul') do (
        if not defined %%A if "%%B"=="REG_SZ" set "%%A=%%C"
        if not defined %%A if "%%B"=="REG_EXPAND_SZ" call set "%%A=%%C"
    )
)

if defined MICROWAVE_PYTHON (
    set "PY=%MICROWAVE_PYTHON%"
) else if defined VIRTUAL_ENV (
    set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Using %PY%
"%PY%" -m microwave_package.webapp %*
