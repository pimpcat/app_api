@echo off
cd /d "%~dp0.."
echo Reorganizando modulo ruteo en %CD%
py -3 ruteo\reorganize_ruteo.py 2>nul || python ruteo\reorganize_ruteo.py
if errorlevel 1 (
  echo ERROR: revisa la salida anterior.
  pause
  exit /b 1
)
echo.
echo Listo. Reinicia el contenedor fastapi_backend si esta en ejecucion.
pause
