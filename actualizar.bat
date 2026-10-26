@echo off
rem Baja la ultima version del panel desde GitHub.
cd /d "%~dp0"
git pull
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo Listo. Si el panel estaba abierto, cerra la ventana "Highlights (no cerrar)" y abrilo de nuevo con iniciar_panel.bat
pause
