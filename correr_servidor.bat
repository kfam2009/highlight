@echo off
rem Mantiene el panel abierto: si se cierra solo, lo vuelve a abrir (y las
rem grabaciones siguen solas). Lo usa iniciar_panel.bat; no hace falta abrirlo a mano.
cd /d "%~dp0"
:loop
"%~dp0.venv\Scripts\python.exe" -m highlight.server
if %errorlevel%==3 exit /b
echo.
echo El panel se cerro. Se vuelve a abrir en 3 segundos...
timeout /t 3 /nobreak > nul
goto loop
