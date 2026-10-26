@echo off
rem Panel de highlights - abre el servidor local y el navegador.
cd /d "%~dp0"
set "PORT=8790"
set "VPY=%~dp0.venv\Scripts\python.exe"

if exist "%VPY%" goto check_deps

echo Primera vez: preparando el entorno de Python...
set "BASEPY="
where py >nul 2>nul && set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>nul && set "BASEPY=python"
)
if not defined BASEPY (
  if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "BASEPY="%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe""
)
if not defined BASEPY (
  echo No encontre Python. Instalalo desde https://www.python.org/downloads/ ^(marcar "Add to PATH"^) y volve a abrir este archivo.
  pause
  exit /b 1
)
%BASEPY% -m venv .venv
if not exist "%VPY%" (
  echo No se pudo crear el entorno .venv
  pause
  exit /b 1
)

:check_deps
"%VPY%" -c "import cv2, numpy, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
  echo Instalando dependencias...
  "%VPY%" -m pip install --upgrade pip >nul
  "%VPY%" -m pip install -r requirements.txt
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 goto open_panel

start "Highlights (no cerrar)" /min "%VPY%" -m highlight.server
timeout /t 3 /nobreak > nul

:open_panel
start "" http://127.0.0.1:%PORT%
exit /b
