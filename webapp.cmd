@echo off
rem Starts the web UI with the first Python found of:
rem   1. MICROWAVE_PYTHON  -- an explicit interpreter, e.g. a venv elsewhere
rem   2. VIRTUAL_ENV       -- whichever venv is activated in this shell
rem   3. .venv\            -- the venv beside this file
rem   4. python            -- whatever is on PATH
setlocal
cd /d "%~dp0"

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
