# Tabla `c_rnc_routing` — Guía de instalación y mantenimiento

Instrucciones para habilitar el ruteo rápido sobre la Red Nacional de Caminos en el Atlas Municipal de Guerrero.

**Resultado esperado:** cálculo de rutas en **menos de 2 segundos** (frente a ~10 s sin esta tabla).

**Documentación relacionada:**
- Análisis técnico de rendimiento: [`RUTEA_PERFORMANCE.md`](RUTEA_PERFORMANCE.md)
- **Ruteo inteligente** (prioridades, peajes, resumen): [`RUTEA_RUTEO_INTELIGENTE.md`](RUTEA_RUTEO_INTELIGENTE.md)
- Código de la API: [`../facade.py`](../facade.py)

---

## 1. ¿Por qué existe esta tabla?

| Tabla | Rol |
|-------|-----|
| `atlas.c_rnc` | Red completa con **geometría** (`the_geom`), atributos y topología pgRouting |
| `atlas.c_rnc_routing` | Copia **ligera** solo con columnas que necesita `pgr_dijkstra` |
| `atlas.c_rnc_loc` | Localidades (origen / destino) |
| `atlas.c_rnc_vertices_pgr` | Vértices de la red |

En cada petición de ruta, pgRouting debe leer todas las aristas y construir un grafo en memoria. Si lee `c_rnc` completa, también arrastra geometrías pesadas que **no usa** para el cálculo del camino.

`c_rnc_routing` elimina ese costo: el routing lee solo `id`, `source`, `target`, `cost` y `reverse_cost`. La geometría de la ruta se obtiene **después**, uniendo por `gid = id` contra `c_rnc` únicamente para los ~100–300 tramos del resultado.

---

## 2. Requisitos previos

- PostgreSQL con **PostGIS** y **pgRouting** (contenedor `db_atlas` en Docker).
- Esquema `atlas` con las tablas de red ya cargadas y topología válida.
- API FastAPI desplegada (`api_backend` / contenedor `fastapi_backend`).
- Acceso a la base con pgAdmin, DBeaver o `psql`.

---

## 3. Creación inicial (una sola vez)

Ejecutar en la base `atlas` (pgAdmin o `psql`).

### 3.1 Crear la tabla

```sql
CREATE TABLE IF NOT EXISTS atlas.c_rnc_routing AS
SELECT gid AS id,
       source,
       target,
       cost,
       reverse_cost
  FROM atlas.c_rnc
 WHERE source IS NOT NULL
   AND target IS NOT NULL
   AND cost > 0;
```

> **Nota:** `id` debe coincidir con `gid` de `c_rnc`. La API une la geometría con `c_rnc.gid = c_rnc_routing.id`.

### 3.2 Crear índices

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_c_rnc_routing_id
    ON atlas.c_rnc_routing (id);

CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_source
    ON atlas.c_rnc_routing (source);

CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_target
    ON atlas.c_rnc_routing (target);
```

### 3.3 Actualizar estadísticas del planificador

```sql
ANALYZE atlas.c_rnc_routing;
```

En Windows, si el comando largo se corta en la terminal, ejecutar **línea por línea**:

```powershell
docker exec -it db_atlas psql -U postgres -d atlas -c "ANALYZE atlas.c_rnc_routing;"
```

### 3.4 Índices recomendados en tablas relacionadas (si aún no existen)

Mejoran localidades, vértices y el armado de la geometría de la ruta:

```sql
-- Red original (geometría y topología)
CREATE INDEX IF NOT EXISTS idx_c_rnc_geom_gist
    ON atlas.c_rnc USING GIST (the_geom);
CREATE INDEX IF NOT EXISTS idx_c_rnc_source ON atlas.c_rnc (source);
CREATE INDEX IF NOT EXISTS idx_c_rnc_target ON atlas.c_rnc (target);

-- Localidades
CREATE UNIQUE INDEX IF NOT EXISTS idx_c_rnc_loc_cvegeo
    ON atlas.c_rnc_loc (cvegeo);
CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_geom_gist
    ON atlas.c_rnc_loc USING GIST (the_geom);

-- Vértices pgRouting
CREATE INDEX IF NOT EXISTS idx_c_rnc_vertices_pgr_geom_gist
    ON atlas.c_rnc_vertices_pgr USING GIST (the_geom);

ANALYZE atlas.c_rnc;
ANALYZE atlas.c_rnc_loc;
ANALYZE atlas.c_rnc_vertices_pgr;
```

---

## 4. Activar en la API

La API **detecta automáticamente** si existe `atlas.c_rnc_routing`. No hay variable de entorno adicional.

Tras crear o actualizar la tabla, **reiniciar el contenedor de la API** (la caché de esquema se carga al arrancar el worker):

```powershell
cd c:\Stack_Martin
docker compose restart api_backend
```

Martin y Apache **no** requieren reinicio para el ruteo.

---

## 5. Verificación

### 5.1 Comprobar que la tabla tiene datos

```sql
SELECT COUNT(*) AS filas FROM atlas.c_rnc_routing;
```

El conteo debe ser menor o igual que `c_rnc` (se excluyen aristas sin `source`/`target` o con `cost <= 0`).

### 5.2 Probar el endpoint

Navegador o curl (ajustar puerto según `.env`, por defecto **850**):

```text
http://localhost:850/api/ruteo?cvegeo_origen=120010001&cvegeo_destino=120010143
```

Debe devolver JSON con `ok: true`, `edge_count`, `length_km` y `geojson` con la línea de la ruta.

### 5.3 Probar en el portal de ruteo

1. Abrir `http://localhost/ruteo/app/`
2. Origen y destino (ej. Acapulco de Juárez → La Providencia).
3. **Calcular ruta** — debe pintarse la línea y mostrarse distancia / tramos en pocos segundos.

---

## 6. Mantenimiento: cuando se actualice `c_rnc`

Cada vez que se recargue o modifique `atlas.c_rnc` (nueva descarga de la RNC, corrección de topología, etc.), **sincronizar** `c_rnc_routing`:

```sql
TRUNCATE atlas.c_rnc_routing;

INSERT INTO atlas.c_rnc_routing (id, source, target, cost, reverse_cost)
SELECT gid,
       source,
       target,
       cost,
       reverse_cost
  FROM atlas.c_rnc
 WHERE source IS NOT NULL
   AND target IS NOT NULL
   AND cost > 0;

ANALYZE atlas.c_rnc_routing;
```

Luego reiniciar la API:

```powershell
docker compose restart api_backend
```

### Script completo de refresco (copiar y pegar)

```sql
BEGIN;

TRUNCATE atlas.c_rnc_routing;

INSERT INTO atlas.c_rnc_routing (id, source, target, cost, reverse_cost)
SELECT gid, source, target, cost, reverse_cost
  FROM atlas.c_rnc
 WHERE source IS NOT NULL
   AND target IS NOT NULL
   AND cost > 0;

ANALYZE atlas.c_rnc_routing;

COMMIT;
```

---

## 7. Cómo trabaja la API internamente

Flujo resumido en `ruteo.py`:

1. **Localidades** — una consulta para origen y destino (`c_rnc_loc`).
2. **Vértices** — usa `node_id` precalculado si existe; si no, KNN contra `c_rnc_vertices_pgr`.
3. **Fase routing** — `pgr_dijkstra` sobre:
   ```sql
   SELECT id, source, target, cost, reverse_cost FROM atlas.c_rnc_routing
   ```
4. **Fase geometría** — solo para los `id` devueltos, lee `the_geom` desde `c_rnc` y arma el GeoJSON de la ruta.

Si `c_rnc_routing` **no existe**, la API vuelve al modo anterior (lee aristas desde `c_rnc` con filtro SQL). El ruteo funciona, pero será más lento.

---

## 8. Resolución de problemas

| Síntoma | Posible causa | Acción |
|---------|---------------|--------|
| Sigue tardando ~10 s | API no reiniciada tras crear la tabla | `docker compose restart api_backend` |
| Error o ruta vacía tras actualizar `c_rnc` | `c_rnc_routing` desactualizada | Ejecutar §6 (TRUNCATE + INSERT + ANALYZE) |
| `relation "c_rnc_routing" does not exist` | Tabla no creada en esquema `atlas` | Ejecutar §3.1 |
| Ruta calculada pero sin línea en mapa | Geometría en `c_rnc` o tipo GeoJSON | Revisar que `gid` en `c_rnc` coincida con `id` en routing |
| Comando `ANALYZE` cortado en CMD | Salto de línea en Windows | Ejecutar un `ANALYZE` por comando (§3.3) |

### Consulta de diagnóstico

```sql
-- ¿Existe la tabla?
SELECT table_schema, table_name
  FROM information_schema.tables
 WHERE table_schema = 'atlas'
   AND table_name = 'c_rnc_routing';

-- ¿Cuántas aristas válidas hay en cada tabla?
SELECT
  (SELECT COUNT(*) FROM atlas.c_rnc) AS c_rnc_total,
  (SELECT COUNT(*) FROM atlas.c_rnc_routing) AS routing_total;
```

---

## 9. Resumen del despliegue

```
┌─────────────────────────────────────────────────────────────┐
│  1. CREATE TABLE c_rnc_routing AS SELECT ... FROM c_rnc     │
│  2. CREATE INDEX (id, source, target)                       │
│  3. ANALYZE c_rnc_routing                                   │
│  4. docker compose restart api_backend                      │
│  5. Probar ruta en visor / API                              │
└─────────────────────────────────────────────────────────────┘
```

**Mantenimiento periódico:** repetir pasos 1–3 de §6 cada vez que cambie `c_rnc`, más reinicio de API.

---

*Atlas Municipal de Guerrero — módulo de ruteo (pgRouting).*
