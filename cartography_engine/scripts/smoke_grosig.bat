@echo off
REM Smoke-test productos GroSIG Cartography (condensado / croquis / localidad / GeoPDF)
cd /d C:\Stack_Martin

if not exist smoke_out mkdir smoke_out

echo === HEALTH ===
curl.exe -s http://localhost:850/api/cartography/health
echo.
echo.

echo Cierra Acrobat/visores de PDF antes de continuar (si un archivo esta abierto, curl escribe 0 bytes).
echo.

echo === CONDENSADO ESTATAL ===
curl.exe -s -w "HTTP %%{http_code} size %%{size_download}\n" -X POST http://localhost:850/api/cartography/generate -H "Content-Type: application/json" -d "{\"template_id\":\"condensado_estatal\",\"format\":\"pdf\",\"params\":{}}" --output smoke_out\condensado.pdf

echo === CROQUIS GroSIG mun 003 ===
curl.exe -s -w "HTTP %%{http_code} size %%{size_download}\n" -X POST http://localhost:850/api/cartography/generate -H "Content-Type: application/json" -d "{\"template_id\":\"grosig_croquis_municipal\",\"format\":\"pdf\",\"params\":{\"cve_mun\":\"003\"}}" --output smoke_out\croquis_grosig.pdf

echo === PLR 001-0143 PDF ===
curl.exe -s -w "HTTP %%{http_code} size %%{size_download}\n" -X POST http://localhost:850/api/cartography/generate -H "Content-Type: application/json" -d "{\"template_id\":\"plano_localidad\",\"format\":\"pdf\",\"params\":{\"cve_mun\":\"001\",\"cve_loc\":\"0143\",\"cve_ent\":\"12\"}}" --output smoke_out\plano.pdf

echo === PLR GeoPDF ===
curl.exe -s -w "HTTP %%{http_code} size %%{size_download}\n" -X POST http://localhost:850/api/cartography/generate -H "Content-Type: application/json" -d "{\"template_id\":\"plano_localidad\",\"format\":\"geopdf\",\"params\":{\"cve_mun\":\"001\",\"cve_loc\":\"0143\",\"cve_ent\":\"12\"}}" --output smoke_out\plano_geo.pdf

echo.
echo === Tamanos (plano.pdf debe ser MAYOR que 0; tipico 50-200 KB) ===
dir smoke_out\*.pdf

echo.
echo Si plano.pdf = 0 bytes: cierra Acrobat y vuelve a correr solo la linea curl de PLR.
pause
