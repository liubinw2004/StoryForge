@echo off
REM ===================================================================
REM  StoryForge - GPU LLM tunnel launcher (ASCII only, on purpose).
REM  Opens SSH local port-forwards so that, on THIS PC:/r/nREM    127.0.0.1:8080 -> llama.cpp server on the GPU host (StoryForge LLM)
REM    127.0.0.1:8090 -> local image service (SD1.5 + IP-Adapter, optional)
REM  Keep this window open while using StoryForge.
REM ===================================================================
setlocal enabledelayedexpansion
title StoryForge - GPU Tunnel (LLM + Image)

set "SSH_HOST=hpc.vmip.com.cn"
set "SSH_PORT=29902"
set "SSH_USER=ubuntu"
set "LOCAL_PORT=8080"
set "REMOTE=127.0.0.1:8080"
set "IMG_LOCAL_PORT=8090"
set "IMG_REMOTE=127.0.0.1:8090"

where ssh >nul 2>nul
if errorlevel 1 goto no_ssh

echo ==============================================================
echo  StoryForge GPU Tunnel
echo --------------------------------------------------------------
echo  Host   : %SSH_USER%@%SSH_HOST%:%SSH_PORT%
echo  Forward: 127.0.0.1:%LOCAL_PORT%  -^>  %REMOTE%
echo  Forward: 127.0.0.1:%IMG_LOCAL_PORT%  -^>  %IMG_REMOTE%   (image service)
echo.
echo  Keep this window OPEN while using StoryForge.
echo  Close it (or press Ctrl+C) to stop the tunnel.
echo ==============================================================
echo.

:loop
echo [%date% %time%] connecting ...
ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -L %LOCAL_PORT%:%REMOTE% -L %IMG_LOCAL_PORT%:%IMG_REMOTE% -p %SSH_PORT% %SSH_USER%@%SSH_HOST%

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
