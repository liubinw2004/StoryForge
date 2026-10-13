@echo off
REM ===================================================================
REM  StoryForge - GPU LLM tunnel launcher (ASCII only, on purpose).
REM  Opens an SSH local port-forward so that, on THIS PC:
REM    127.0.0.1:8080  ->  llama.cpp on the GPU host (StoryForge LLM)
REM  Keep this window open while using StoryForge.
REM ===================================================================
setlocal
title StoryForge - GPU Tunnel (LLM)

set "SSH_HOST=hpc.vmip.com.cn"
set "SSH_PORT=29902"
set "SSH_USER=ubuntu"
set "KEY=%~dp0ssh_key\id_storyforge"
set "LOCAL_PORT=8080"
set "REMOTE=127.0.0.1:8080"

where ssh >nul 2>nul
if errorlevel 1 goto no_ssh

REM Windows OpenSSH requires the private key to be readable only by the current user.
icacls "%KEY%" /inheritance:r /grant:r "%USERNAME%:R" >nul 2>nul

echo ==============================================================
echo  StoryForge GPU Tunnel
echo --------------------------------------------------------------
echo  Host   : %SSH_USER%@%SSH_HOST%:%SSH_PORT%
echo  Forward: 127.0.0.1:%LOCAL_PORT%  -^>  %REMOTE%
echo.
echo  Keep this window OPEN while using StoryForge.
echo  Close it (or press Ctrl+C) to stop the tunnel.
echo ==============================================================
echo.

:loop
echo [%date% %time%] connecting ...
ssh -N -i "%KEY%" -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -L %LOCAL_PORT%:%REMOTE% -p %SSH_PORT% %SSH_USER%@%SSH_HOST%

if errorlevel 1 (
  echo.
  echo [warn] Tunnel closed. Is the local port %LOCAL_PORT% already in use?
  echo        Check with:  netstat -ano ^| findstr %LOCAL_PORT%
  echo        Retrying in 5 seconds ... ^(Ctrl+C to quit^)
  echo.
  timeout /t 5 /nobreak >nul
  goto loop
)

echo.
echo [info] Tunnel ended normally.
pause
endlocal
exit /b 0

:no_ssh
echo.
echo [ERROR] The "ssh" command was not found.
echo         Install OpenSSH Client:
echo           Settings - Apps - Optional features - Add a feature - OpenSSH Client
echo         Then re-run this file.
echo.
pause
endlocal
exit /b 1
