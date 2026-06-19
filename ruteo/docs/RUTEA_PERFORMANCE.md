# Análisis de rendimiento — API de ruteo (`ruteo.py`)

**Guía operativa (crear y mantener `c_rnc_routing`):** [`RUTEA_TABLA_ROUTING.md`](RUTEA_TABLA_ROUTING.md)

Documento técnico sobre cuellos de botella, optimizaciones aplicadas, mejoras propuestas (solo BD) y estimación de tiempos.

**Contexto medido:** rutas de ~100 km / ~170 tramos en **9–14 s** con la implementación original.

---

## 1. Cuellos de botella identificados (implementación original)

| # | Cuello de botella | Tipo | Impacto estimado |
|---|-------------------|------|------------------|
| 1 | **`pgr_dijkstra` sobre subconsulta que escanea TODA `c_rnc`** sin filtro espacial | SQL / pgRouting | **60–80 %** del tiempo total |
| 2 | **`COALESCE(NULLIF(cost,0), ST_Length(geom))`** en la subconsulta de aristas | SQL (CPU geométrica) | **10–25 %** si `cost` ya existe |
| 3 | **Reconstrucción del grafo en memoria en cada petición** (pgRouting parsea SQL cada vez) | pgRouting | Incluido en #1 |
| 4 | **Múltiples viajes a PostgreSQL** (6–8 round-trips: meta ×3, localidad ×2, vértice ×2, ruta ×1) | Python / red | **5–10 %** |
| 5 | **`ST_Transform` redundante** en localidades (3× por fila: AsGeoJSON, X, Y) | SQL | **1–3 %** |
| 6 | **KNN vértice con `ST_Transform(l.geom, ST_SRID(v.geom))`** en ORDER BY | SQL (sin índice KNN) | **5–15 %** si falta `node_id` |
| 7 | **`ST_Union` + `ST_LineMerge` + `ST_Transform` sobre geometrías de ruta** | SQL | **3–8 %** (174 tramos ≈ bajo; empeora con rutas largas) |
| 8 | **Resolución de columnas** (`information_schema`) en cada request | Python / SQL | **<1 %** tras caché de `column_resolver` |

### Por qué 9–14 s es coherente con el diseño original

Para la Red Nacional de Caminos (orden de **decenas de miles de aristas** a escala estatal/nacional):

1. pgRouting materializa el grafo completo desde la subconsulta SQL.
2. Si cada arista evalúa `ST_Length` (aunque sea como fallback), el costo de construcción del grafo crece linealmente con el número de segmentos.
3. Dijkstra sobre 50k–200k aristas debería tardar **<500 ms** una vez construido el grafo; el resto del tiempo suele ser **construcción del grafo + geometría pesada**.

---

## 2. Optimizaciones implementadas (solo código Python/SQL)

### 2.1 Subgrafo acotado por bounding box (antes de Dijkstra)

**Qué hace:** calcula la envolvente de los vértices origen/destino, la expande (`ST_Expand` con margen 35 % o mínimo 15 km) y filtra aristas con `geom && envelope`.

**Por qué ayuda:** Dijkstra corre sobre miles de aristas en lugar de decenas de miles.

**Impacto estimado:** **50–70 %** de reducción en rutas intra/inter municipales.

**Riesgo:** rutas muy sinuosas que salen del bbox → **reintento automático sin filtro** (mismo resultado funcional).

**Cambio:** solo SQL + Python.

---

### 2.2 Uso directo de `cost` / `reverse_cost` sin `ST_Length`

**Antes:**
```sql
COALESCE(NULLIF(cost, 0), ST_Length(the_geom)) AS cost
```

**Ahora:** si las columnas existen en metadatos, se proyectan directamente:
```sql
cost AS cost, reverse_cost AS reverse_cost
```

**Impacto:** **10–20 %** cuando la tabla ya tiene costos precalculados.

**Cambio:** solo SQL (generado desde Python).

**Nota:** si existen filas con `cost = 0`, conviene corregirlas en BD (ver script propuesto §4).

---

### 2.3 Un solo viaje para origen + destino

**Antes:** 2× `_fetch_localidad`.

**Ahora:** `WHERE cvegeo IN (origen, destino)` + un solo `ST_Transform` por localidad (solo para GeoJSON).

**Impacto:** **2–5 %** + menos latencia de red.

**Cambio:** solo Python/SQL.

---

### 2.4 KNN sin `ST_Transform` en el operador `<->`

**Antes:**
```sql
ORDER BY v.geom <-> ST_Transform(l.geom, ST_SRID(v.geom))
```

**Ahora:**
```sql
ORDER BY v.geom <-> l.geom  -- mismo SRID
```

**Por qué:** el índice GiST KNN solo se usa si ambas geometrías comparten SRID.

**Impacto:** **5–15 %** cuando `node_id` no está precalculado; **~0 %** si todas las localidades tienen `node_id`.

**Cambio:** solo SQL.

---

### 2.5 Resolución de vértices: una consulta si faltan ambos nodos

**Antes:** hasta 2 consultas KNN secuenciales.

**Ahora:** una sola consulta con dos subselects si faltan origen y destino.

**Impacto:** **2–5 %** en el peor caso.

**Cambio:** solo SQL.

---

### 2.6 Geometría de ruta: `ST_Collect` ordenado por `path_seq` (no `ST_Union`)

**Antes:** `ST_LineMerge(ST_Union(ST_Transform(...)))` — `ST_Union` no preserva orden y fusiona geometrías de forma más costosa.

**Ahora:**
```sql
ST_LineMerge(ST_Collect(r.geom ORDER BY d.path_seq))
```
con fallback a `ST_Collect` si `LineMerge` devuelve NULL.

**Impacto:** **3–10 %** en rutas largas; marginal en rutas cortas.

**Cambio:** solo SQL.

---

### 2.7 `agg_cost` de Dijkstra como longitud total

**Antes:** `MAX(agg_cost)` en CTE separado (correcto).

**Ahora:** igual, sin recalcular longitud desde geometrías.

**Impacto:** ya era eficiente; se mantiene.

---

### 2.8 Caché de esquema en proceso (`@lru_cache`)

**Qué hace:** metadatos de columnas (`_loc_meta`, `_routing_meta`, `_vertices_meta`) se resuelven una vez por worker de Uvicorn/Gunicorn.

**Impacto:** **<1 %** por request; útil bajo carga.

**Cambio:** solo Python.

---

### 2.9 `SET LOCAL statement_timeout`

Evita que el timeout de 30 s afecte otras transacciones en el mismo pool (si se usara pool en el futuro).

**Cambio:** solo SQL.

---

## 3. Optimizaciones analizadas pero NO implementadas (y por qué)

### 3.1 `pgr_astar` en lugar de `pgr_dijkstra`

| Criterio | Evaluación |
|----------|------------|
| Ganancia teórica | 20–40 % sobre Dijkstra en grafos grandes |
| Requisito | Columnas `x1,y1,x2,y2` en aristas **o** vértices con coordenadas preproyectadas |
| Sin cambio BD | Forzar `ST_X(ST_StartPoint(geom))` en la subconsulta **empeora** la construcción del grafo |
| **Decisión** | **No implementar** sin columnas precalculadas |

**Recomendación BD:** ver §4.3.

---

### 3.2 GeoJSON ensamblado en Python (sin `ST_AsGeoJSON` en ruta)

| Criterio | Evaluación |
|----------|------------|
| Ganancia | 5–15 % si la ruta tiene muchos tramos |
| Coste | Dependencia opcional (Shapely) o lógica WKB manual |
| **Decisión** | **No implementar** ahora; dividir en 2 consultas (Dijkstra → edge ids, luego WKB) es el siguiente paso si aún falta rendimiento |

---

### 3.3 Devolver solo `edge_id` y reconstruir geometría en segunda consulta

**Flujo propuesto:**
1. `pgr_dijkstra` → lista de `edge` + `agg_cost` (sin JOIN geométrico).
2. `SELECT geom FROM c_rnc WHERE gid = ANY(%s) ORDER BY ord`.

**Ganancia estimada:** **5–12 %** (evita JOIN + collect en la misma transacción pesada).

**Estado:** parcialmente logrado en una sola query; separar en dos consultas es mejora incremental futura.

---

### 3.4 Vista materializada / tabla `c_rnc_routing` dedicada

**Ganancia:** **70–90 %** (grafo precalculado, sin escaneo completo).

**Requiere:** cambio de esquema BD — **solo propuesto** en §4.

---

## 4. Scripts SQL propuestos (NO aplicados automáticamente)

### 4.1 Índices esenciales

```sql
-- Aristas: topología pgRouting
CREATE INDEX IF NOT EXISTS idx_c_rnc_source ON atlas.c_rnc (source);
CREATE INDEX IF NOT EXISTS idx_c_rnc_target ON atlas.c_rnc (target);
CREATE INDEX IF NOT EXISTS idx_c_rnc_source_target ON atlas.c_rnc (source, target);

-- Filtro espacial del subgrafo (bbox)
CREATE INDEX IF NOT EXISTS idx_c_rnc_geom_gist ON atlas.c_rnc USING GIST (the_geom);

-- Vértices: KNN y bbox
CREATE INDEX IF NOT EXISTS idx_c_rnc_vertices_pgr_geom_gist
  ON atlas.c_rnc_vertices_pgr USING GIST (the_geom);
CREATE INDEX IF NOT EXISTS idx_c_rnc_vertices_pgr_id
  ON atlas.c_rnc_vertices_pgr (id);

-- Localidades
CREATE UNIQUE INDEX IF NOT EXISTS idx_c_rnc_loc_cvegeo
  ON atlas.c_rnc_loc (cvegeo);
CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_geom_gist
  ON atlas.c_rnc_loc USING GIST (the_geom);
CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_cve_mun
  ON atlas.c_rnc_loc (cve_mun);
```

**Impacto estimado combinado:** **30–50 %** adicional sobre el código optimizado si hoy faltan índices.

---

### 4.2 Población / validación de `node_id` en localidades

```sql
-- Actualizar node_id una sola vez (ajustar nombres de columnas)
UPDATE atlas.c_rnc_loc l
   SET node = sub.vid
  FROM (
    SELECT l2.ctid,
           (
             SELECT v.id
               FROM atlas.c_rnc_vertices_pgr v
              WHERE v.the_geom IS NOT NULL
              ORDER BY v.the_geom <-> l2.the_geom
              LIMIT 1
           ) AS vid
      FROM atlas.c_rnc_loc l2
     WHERE l2.the_geom IS NOT NULL
       AND (l2.node IS NULL OR l2.node = 0)
  ) sub
 WHERE l.ctid = sub.ctid;
```

**Impacto:** elimina KNN en tiempo de consulta → **5–15 %** por request.

---

### 4.3 Columnas para A* (opcional)

```sql
ALTER TABLE atlas.c_rnc
  ADD COLUMN IF NOT EXISTS x1 double precision,
  ADD COLUMN IF NOT EXISTS y1 double precision,
  ADD COLUMN IF NOT EXISTS x2 double precision,
  ADD COLUMN IF NOT EXISTS y2 double precision;

UPDATE atlas.c_rnc
   SET x1 = ST_X(ST_StartPoint(the_geom)),
       y1 = ST_Y(ST_StartPoint(the_geom)),
       x2 = ST_X(ST_EndPoint(the_geom)),
       y2 = ST_Y(ST_EndPoint(the_geom))
 WHERE x1 IS NULL;
```

Luego usar `pgr_astar` con esas columnas.

**Impacto:** **15–30 %** adicional en grafos grandes.

---

### 4.4 Tabla `c_rnc_routing` (implementada en API)

La API detecta `atlas.c_rnc_routing` al arrancar el worker y usa:

```sql
SELECT id, source, target, cost, reverse_cost FROM atlas.c_rnc_routing
```

para `pgr_dijkstra`. La geometría sigue leyéndose de `c_rnc` por `gid = id`.

**Mantenimiento** cuando se actualice `c_rnc`:

```sql
TRUNCATE atlas.c_rnc_routing;
INSERT INTO atlas.c_rnc_routing (id, source, target, cost, reverse_cost)
SELECT gid, source, target, cost, reverse_cost
  FROM atlas.c_rnc
 WHERE source IS NOT NULL AND target IS NOT NULL AND cost > 0;
ANALYZE atlas.c_rnc_routing;
```

Tras recargar datos, reiniciar `api_backend` (caché de esquema en memoria).

---

### 4.5 Vista de aristas para pgRouting (alternativa legacy)

```sql
CREATE OR REPLACE VIEW atlas.c_rnc_routing AS
SELECT
  gid AS id,
  source,
  target,
  cost,
  reverse_cost
FROM atlas.c_rnc
WHERE source IS NOT NULL
  AND target IS NOT NULL
  AND cost > 0;
```

Y en Python:
```sql
SELECT * FROM pgr_dijkstra('SELECT * FROM atlas.c_rnc_routing', ...)
```

**Impacto:** planificador cachea mejor; menos SQL dinámico.

---

### 4.5 Geometría en WGS84 precalculada (evita `ST_Transform` en ruta)

```sql
ALTER TABLE atlas.c_rnc ADD COLUMN IF NOT EXISTS geom_4326 geometry(LineString, 4326);

UPDATE atlas.c_rnc
   SET geom_4326 = ST_Transform(the_geom, 4326)
 WHERE geom_4326 IS NULL;

CREATE INDEX IF NOT EXISTS idx_c_rnc_geom4326_gist
  ON atlas.c_rnc USING GIST (geom_4326);
```

**Impacto en ensamblado de ruta:** **5–10 %**.

---

## 5. Resumen: qué requiere cada tipo de cambio

| Optimización | Python | SQL (dinámico) | Esquema BD |
|--------------|--------|----------------|------------|
| Bbox subgrafo | ✓ | ✓ | Índice GIST recomendado |
| cost sin ST_Length | ✓ | ✓ | — |
| Fetch par localidades | ✓ | ✓ | — |
| KNN mismo SRID | — | ✓ | Índice GIST |
| Collect ordenado vs Union | — | ✓ | — |
| Caché esquema | ✓ | — | — |
| Índices | — | — | ✓ |
| node_id precalculado | — | — | ✓ |
| Vista routing | — | — | ✓ |
| pgr_astar | ✓ | ✓ | ✓ (x1..y2) |
| geom_4326 | — | ✓ | ✓ |

---

## 6. Estimación de tiempo de respuesta

| Escenario | Original | Tras código optimizado | + índices BD | + node_id + vista |
|-----------|----------|------------------------|--------------|-------------------|
| Ruta ~100 km / ~170 tramos | 9–14 s | **1.5–4 s** | **0.8–2.5 s** | **0.4–1.5 s** |
| Ruta corta intra-municipal | 3–6 s | **0.3–1 s** | **0.2–0.6 s** | **0.1–0.4 s** |
| Ruta interestatal larga | 15–25 s | **3–8 s** | **2–5 s** | **1–3 s** |

**Reducción total estimada (código + índices + node_id):** **80–92 %** respecto al baseline de 9–14 s → objetivo **~1 s** en condiciones normales.

Factores que mantienen latencia alta sin cambios BD:
- Grafo estatal completo sin bbox (reintento sin filtro).
- Cold start del worker (primera petición tras reinicio).
- Disco lento en Docker / `data_postgres` en HDD.

---

## 7. Verificación recomendada

```sql
-- Tiempo solo Dijkstra (sin geometría)
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM pgr_dijkstra(
  'SELECT gid AS id, source, target, cost, reverse_cost FROM atlas.c_rnc WHERE source IS NOT NULL',
  <start_vid>, <end_vid>, directed := false
);

-- Índices existentes
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = 'atlas'
   AND tablename IN ('c_rnc', 'c_rnc_loc', 'c_rnc_vertices_pgr');
```

En la API, comparar `X-Process-Time` o logs antes/después con el mismo par `cvegeo_origen` / `cvegeo_destino`.

---

## 8. Comportamiento funcional preservado

- Mismas validaciones (`MISSING_PARAMS`, `SAME_LOCALITY`, `LOC_NOT_FOUND`, `VERTEX_NOT_FOUND`, `NO_ROUTE`).
- Mismo JSON de respuesta (`ok`, `geojson` FeatureCollection, `length_m`, `length_km`, `edge_count`, vértices, nombres).
- Mismo algoritmo: **Dijkstra no dirigido** (`directed := false`).
- Reintento sin bbox garantiza misma conectividad que grafo completo cuando el bbox es insuficiente.

## 9. Regresión observada: ~19 s tras índices (jun 2026)

### Síntomas
- Ruta correcta (42 km, 212 tramos) y línea visible en mapa.
- Tiempo **peor** que el baseline (~10 s).

### Causas probables (código anterior a este parche)

| Factor | Efecto |
|--------|--------|
| **Doble `pgr_dijkstra`** | Bbox fallaba → 1.er Dijkstra (~8–10 s) + 2.º sin filtro (~8–10 s) ≈ **16–20 s** |
| **`ST_Union` de 212 geometrías** en la misma consulta que Dijkstra | **+2–5 s** de CPU geométrica |
| **Índices recién creados sin `ANALYZE`** | Planificador puede elegir planes subóptimos temporalmente |
| **Índices GIST no aceleran** la construcción del grafo pgRouting si el SQL sigue siendo dinámico con bbox |

Los índices **sí ayudan** a `JOIN` por `gid`, KNN de vértices y filtros `cvegeo`; **no eliminan** el costo de materializar el grafo completo en cada petición.

### Parche aplicado en `ruteo.py`

1. **Eliminado bbox + reintento** → un solo Dijkstra por petición.
2. **SQL de aristas estático** (`edges_sql` en caché de esquema).
3. **Dos fases**: (a) solo `pgr_dijkstra`, (b) geometría con `unnest` + `ST_Dump` + `ST_Collect` ordenado (sin `ST_Union`).

### Tras reiniciar API, ejecutar en PostgreSQL

```sql
ANALYZE atlas.c_rnc;
ANALYZE atlas.c_rnc_loc;
ANALYZE atlas.c_rnc_vertices_pgr;
```

Tiempo esperado tras parche + ANALYZE: **~6–10 s** sin vista materializada; **~1–3 s** con vista/tabla de aristas dedicada (§4.4).

---

