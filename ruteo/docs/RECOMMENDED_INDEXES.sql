-- Índices recomendados para el motor de ruteo (NO ejecutar automáticamente).
-- Revisar en pgAdmin / psql según carga y planes de consulta (EXPLAIN ANALYZE).
--
-- Contexto: app_api/ruteo/routing_engine — consultas frecuentes sobre c_rnc,
-- c_rnc_routing, c_rnc_loc y c_rnc_vertices_pgr.

-- ---------------------------------------------------------------------------
-- 1. c_rnc_routing — aristas ligeras para pgr_dijkstra
-- ---------------------------------------------------------------------------
-- Ya documentados en RUTEA_TABLA_ROUTING.md; críticos para lectura del grafo.

CREATE UNIQUE INDEX IF NOT EXISTS idx_c_rnc_routing_id
    ON atlas.c_rnc_routing (id);

CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_source
    ON atlas.c_rnc_routing (source);

CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_target
    ON atlas.c_rnc_routing (target);

-- Filtros dinámicos (sin peajes, construcción, peaje):
CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_peaje
    ON atlas.c_rnc_routing (peaje)
    WHERE peaje IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_condicion
    ON atlas.c_rnc_routing (condicion)
    WHERE condicion IS NOT NULL;

-- Búsqueda de corredor por nombre (estrategia OD / stitch):
CREATE INDEX IF NOT EXISTS idx_c_rnc_routing_nombre_lower
    ON atlas.c_rnc_routing (LOWER(TRIM(nombre)))
    WHERE nombre IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. c_rnc — geometría y atributos al construir GeoJSON / validar peaje
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_c_rnc_gid
    ON atlas.c_rnc (gid);

-- JOIN post-Dijkstra: WHERE gid = ANY(array) — acelera resumen y geometría.
-- (Si gid ya es PK, este índice es redundante.)

CREATE INDEX IF NOT EXISTS idx_c_rnc_nombre_lower
    ON atlas.c_rnc (LOWER(TRIM(nombre)))
    WHERE nombre IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_c_rnc_peaje
    ON atlas.c_rnc (peaje)
    WHERE peaje IS NOT NULL;

-- Geometría: KNN y ST_DWithin en snap a corredor (GIST obligatorio en red).
CREATE INDEX IF NOT EXISTS idx_c_rnc_geom_gist
    ON atlas.c_rnc USING GIST (the_geom);

-- ---------------------------------------------------------------------------
-- 3. c_rnc_loc — localidades origen/destino
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_cvegeo
    ON atlas.c_rnc_loc (TRIM(cvegeo::text));

CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_geom_gist
    ON atlas.c_rnc_loc USING GIST (the_geom);

CREATE INDEX IF NOT EXISTS idx_c_rnc_loc_cve_mun
    ON atlas.c_rnc_loc (cve_mun)
    WHERE cve_mun IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. c_rnc_vertices_pgr — snap KNN (<->)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_c_rnc_vertices_geom_gist
    ON atlas.c_rnc_vertices_pgr USING GIST (the_geom);

-- ---------------------------------------------------------------------------
-- 5. Estadísticas
-- ---------------------------------------------------------------------------

ANALYZE atlas.c_rnc;
ANALYZE atlas.c_rnc_routing;
ANALYZE atlas.c_rnc_loc;
ANALYZE atlas.c_rnc_vertices_pgr;

-- ---------------------------------------------------------------------------
-- Mejoras NO implementadas (evaluar con DBA)
-- ---------------------------------------------------------------------------
--
-- A) Vista materializada ``atlas.c_rnc_routing_costs`` con columnas
--    cost_distancia, cost_tiempo, cost_sin_peaje precalculadas por restricción.
--    Beneficio: evita evaluar expresiones CASE largas en cada pgr_dijkstra.
--    Costo: mantenimiento tras cada actualización de c_rnc.
--
-- B) Tabla auxiliar de componentes conectados (pgr_connectedComponents) por
--    subgrafo (pavimentado / sin peaje). Beneficio: fallar rápido con NO_ROUTE
--    sin ejecutar Dijkstra cuando origen/destino están en componentes distintas.
--
-- C) Particionado de c_rnc_routing por estado o jerarquía admin para consultas
--    regionales. Beneficio: grafos más pequeños en despliegues multi-estado.
