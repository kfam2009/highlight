@echo off
rem Baja la ultima version del panel desde GitHub.
cd /d "%~dp0"
git pull
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo Listo. Reiniciando el panel con la version nueva...
call "%~dp0reiniciar_panel.bat"
