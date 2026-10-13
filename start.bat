@echo off
REM ===================================================================
REM  StoryForge launcher (ASCII only, on purpose).
REM  Chinese UX text lives in scripts\launcher.py (UTF-8) instead, so
REM  the console codepage (GBK/936) can never corrupt this batch file.
REM ===================================================================
setlocal
cd /d "%~dp0"
title StoryForge

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "DEPS_FLAG=%VENV_DIR%\.deps_ok"

if exist "%VENV_PY%" goto venv_ready

echo [StoryForge] Creating the Python virtual environment (.venv) ...
echo.

set "BOOT_PY="
where py >nul 2>nul
if not errorlevel 1 set "BOOT_PY=py -3"
if not defined BOOT_PY (
  where python >nul 2>nul
  if not errorlevel 1 set "BOOT_PY=python"
)
if not defined BOOT_PY goto no_python

echo [StoryForge] Using Python launcher: %BOOT_PY%
%BOOT_PY% -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" goto venv_failed

:venv_ready
if exist "%DEPS_FLAG%" goto run_app

echo [StoryForge] Installing dependencies (first run only, 1-3 minutes) ...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto deps_failed
echo ok> "%DEPS_FLAG%"

:run_app
echo.
"%VENV_PY%" "%~dp0scripts\launcher.py"
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
  echo.
  echo [StoryForge] Launcher exited with code %EXITCODE%.
  echo.
  pause
)
endlocal
exit /b 0

:no_python
echo.
echo [ERROR] Python 3.10 or newer was not found on this computer.
echo         Please install Python 3.10+ ^(3.13 recommended^) from:
echo             https://www.python.org/downloads/
echo         During setup, remember to check "Add Python to PATH".
echo.
pause
endlocal
exit /b 1

:venv_failed
echo.
echo [ERROR] Failed to create the virtual environment.
echo         Try manually: %BOOT_PY% -m venv "%VENV_DIR%"
echo.
pause
endlocal
exit /b 1

:deps_failed
echo.
echo [ERROR] Failed to install dependencies. Check your network, then retry:
echo         "%VENV_PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
endlocal
exit /b 1
