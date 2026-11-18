@echo off
rem Cierra el panel si esta abierto (aunque la ventana este minimizada u oculta) y lo vuelve a abrir.
rem Ojo: si hay grabaciones SRT en curso, se cortan y hay que tocar Grabar de nuevo.
cd /d "%~dp0"
echo Cerrando el panel anterior...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak > nul
call "%~dp0iniciar_panel.bat"
