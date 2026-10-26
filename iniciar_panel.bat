@echo off
rem Panel de highlights - abre el servidor local y el navegador.
cd /d "%~dp0"
set "PORT=8790"
set "VPY=%~dp0.venv\Scripts\python.exe"

if exist "%VPY%" goto check_deps

echo Primera vez: preparando el entorno de Python...
rem Se prueba cada Python de verdad (el "python" de la Microsoft Store es un acceso falso).
set "CODEXPY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEXPY%" call :try_python "%CODEXPY%"
if exist "%VPY%" goto check_deps
call :try_python py -3
if exist "%VPY%" goto check_deps
call :try_python python
if exist "%VPY%" goto check_deps
call :try_python "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%VPY%" goto check_deps
call :try_python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%VPY%" goto check_deps

echo.
echo No encontre un Python que funcione. Instalalo con este comando en PowerShell:
echo     winget install -e --id Python.Python.3.12
echo y despues volve a abrir este archivo.
pause
exit /b 1

:check_deps
"%VPY%" -c "import cv2, numpy, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
  echo Instalando dependencias ^(solo la primera vez^)...
  "%VPY%" -m pip install --upgrade pip >nul 2>nul
  "%VPY%" -m pip install -r requirements.txt
  "%VPY%" -c "import cv2, numpy, imageio_ffmpeg" >nul 2>nul
  if errorlevel 1 (
    echo No se pudieron instalar las dependencias. Mandame una captura de esta ventana.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 goto open_panel

start "Highlights (no cerrar)" /min "%VPY%" -m highlight.server
timeout /t 3 /nobreak > nul

:open_panel
start "" http://127.0.0.1:%PORT%
exit /b 0

:try_python
%* -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
echo Usando Python: %*
%* -m venv "%~dp0.venv" >nul 2>nul
if not exist "%VPY%" (
  if exist "%~dp0.venv" rmdir /s /q "%~dp0.venv"
)
exit /b 0
