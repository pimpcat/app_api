@echo off
REM GroSIG Cartography Engine — verificación local / Docker
REM Ejecutar desde una terminal con Docker y Python disponibles.

setlocal
cd /d "%~dp0\..\.."

echo === 1) Tests unitarios (sin PostGIS) ===
python -m pytest cartography_engine/tests/test_engine.py -v --tb=short
if errorlevel 1 goto :fail

echo === 2) Demo PDF ===
python -m cartography_engine.scripts.verify_cartography
if errorlevel 1 goto :fail

echo === 3) Croquis municipal via contenedor (requiere stack + flag) ===
docker exec fastapi_backend pip install -q -r /app/requirements-cartography.txt pytest
docker exec -e CARTOGRAPHY_ENGINE_ENABLED=true -e CARTOGRAPHY_INTEGRATION=true fastapi_backend python -m cartography_engine.scripts.verify_cartography --cve-mun 004 --out-dir /tmp
if errorlevel 1 (
  echo NOTA: si falla, recrea api_backend con CARTOGRAPHY_ENGINE_ENABLED=true en .env
  echo   docker compose up -d --build api_backend
)

echo VERIFY_SCRIPT_DONE
exit /b 0

:fail
echo VERIFY_SCRIPT_FAIL
exit /b 1
