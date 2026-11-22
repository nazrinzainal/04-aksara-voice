@echo off
rem Aksara Voice - neural voice studio
setlocal
cd /d "%~dp0"
title Aksara Voice
set "PY=.venv\Scripts\python.exe"
set "PORT=7788"

if not exist "%PY%" (
  echo [Aksara Voice] The project environment .venv was not found.
  echo Create it first - see "Setup" in README.md. The global Python is not used,
  echo because it does not have the pinned torch/numpy/chatterbox versions.
  pause
  exit /b 1
)

rem Already running? Just open the browser instead of starting a second copy.
netstat -ano -p tcp | findstr /r /c:"127\.0\.0\.1:%PORT% .*LISTENING" >nul
if %errorlevel%==0 (
  echo [Aksara Voice] Port %PORT% is already in use - opening http://127.0.0.1:%PORT%/
  echo If the page does not load, another program owns the port: close it, or run
  echo   "%PY%" app.py --port 7790
  start "" "http://127.0.0.1:%PORT%/"
  timeout /t 4 >nul
  exit /b 0
)

set PYTHONUNBUFFERED=1
"%PY%" app.py %*
if errorlevel 1 (
  echo.
  echo [Aksara Voice] Server stopped with an error - see the messages above.
  pause
)
endlocal
