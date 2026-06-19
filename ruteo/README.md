# Módulo de ruteo RNC (futura implementación)

Paquete aislado del Atlas principal. Contiene el motor de ruteo sobre la Red Nacional de Caminos (pgRouting).

## Estructura

```
ruteo/
  __init__.py          API pública
  facade.py            Fachada y alias legacy
  router.py            Endpoints GET /api/ruteo*
  routing_engine/      Motor (mover aquí con reorganize_ruteo.py)
  scripts/             Tests y diagnóstico
  docs/                Documentación técnica
  output/              Salidas de diagnóstico (GeoJSON, CSV, JSON)
  legacy/              Código histórico
```

## API HTTP

Montada en `main.py` vía `ruteo.router`:

- `GET /api/ruteo/localidades` — catálogo de localidades
- `GET /api/ruteo` — cálculo de ruta (GeoJSON)

## Scripts (Docker)

```powershell
docker exec fastapi_backend python /app/ruteo/scripts/test_peaje_ruteo.py
docker exec fastapi_backend python /app/ruteo/scripts/diagnose_corridor_subgraph.py
```

Salidas por defecto en `/app/ruteo/output/` → `app_api/ruteo/output/` en el host.

## Reorganización física (opcional)

El motor sigue en `app_api/routing_engine/` hasta que ejecutes una vez:

```powershell
cd c:\Stack_Martin\app_api
ruteo\run_reorganize.bat
```

Eso mueve `routing_engine`, elimina duplicados en `app_api/docs/` y actualiza imports.
Mientras tanto, `ruteo/routing_engine/__init__.py` reexporta el motor legacy.

## Portal web

Frontend independiente: `htdocs/ruteo/` (fuera del Atlas `atlas_gro`).
