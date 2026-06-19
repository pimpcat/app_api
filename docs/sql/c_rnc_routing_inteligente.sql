-- =============================================================================
-- Atlas Guerrero — c_rnc_routing inteligente (pgRouting + prioridades)
-- Ejecutar completo en pgAdmin sobre la base "atlas".
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1) Eliminar tabla anterior (solo aristas básicas)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS atlas.c_rnc_routing CASCADE;

-- -----------------------------------------------------------------------------
-- 2) Crear tabla enriquecida
-- -----------------------------------------------------------------------------
CREATE TABLE atlas.c_rnc_routing (
    id                      BIGINT PRIMARY KEY,          -- = c_rnc.gid
    source                  INTEGER NOT NULL,
    target                  INTEGER NOT NULL,

    -- Costos para pgRouting (metros penalizados + versión sin peajes)
    cost                    DOUBLE PRECISION NOT NULL,
    reverse_cost            DOUBLE PRECISION NOT NULL,
    cost_sin_peaje          DOUBLE PRECISION NOT NULL,
    reverse_cost_sin_peaje  DOUBLE PRECISION NOT NULL,

    -- Coordenadas para pgr_astar (mismo SRID que vértices)
    x1                      DOUBLE PRECISION,
    y1                      DOUBLE PRECISION,
    x2                      DOUBLE PRECISION,
    y2                      DOUBLE PRECISION,

    -- Atributos de catálogo (resumen de ruta)
    tipo_camino             VARCHAR(64),                 -- = tipo_vial
    tipo_vial               VARCHAR(64),
    velocidad_kmh           SMALLINT,
    jerarquia_vial          SMALLINT NOT NULL,           -- combinada (admin + pav + tipo)
    jerarquia_admin         SMALLINT NOT NULL,           -- 1 Federal … 7 N/A
    jerarquia_tipo_vial     SMALLINT NOT NULL,           -- 1 Carretera … 28 Otro
    sentido                 VARCHAR(64),                 -- circula
    es_pavimentado          BOOLEAN NOT NULL,
    cond_pav                VARCHAR(32),
    recubrimiento           VARCHAR(32),                 -- recubri
    administra              VARCHAR(32),
    peaje                   VARCHAR(8),
    nombre                  VARCHAR(512),
    longitud_m              DOUBLE PRECISION NOT NULL
);

-- -----------------------------------------------------------------------------
-- 3) Poblar desde c_rnc + vértices
-- -----------------------------------------------------------------------------
INSERT INTO atlas.c_rnc_routing (
    id, source, target,
    cost, reverse_cost, cost_sin_peaje, reverse_cost_sin_peaje,
    x1, y1, x2, y2,
    tipo_camino, tipo_vial, velocidad_kmh,
    jerarquia_vial, jerarquia_admin, jerarquia_tipo_vial,
    sentido, es_pavimentado, cond_pav, recubrimiento, administra, peaje, nombre,
    longitud_m
)
WITH base AS (
    SELECT
        r.gid,
        r.source,
        r.target,
        TRIM(r.tipo_vial)                                           AS tipo_vial,
        TRIM(r.administra)                                          AS administra,
        TRIM(r.cond_pav)                                            AS cond_pav,
        TRIM(r.recubri)                                             AS recubri,
        TRIM(r.peaje)                                               AS peaje,
        TRIM(r.nombre)                                              AS nombre,
        TRIM(r.circula)                                             AS circula,
        CASE
            WHEN TRIM(r.velocidad) ~ '^[0-9]+$'
            THEN TRIM(r.velocidad)::SMALLINT
            ELSE NULL
        END                                                         AS velocidad_kmh,
        COALESCE(
            NULLIF(r.cost, 0),
            NULLIF(r.longitud, 0) * 1000.0,
            ST_Length(r.the_geom::geography)
        )                                                           AS longitud_m,
        CASE TRIM(r.administra)
            WHEN 'Federal'    THEN 1
            WHEN 'Estatal'    THEN 2
            WHEN 'Municipal'  THEN 3
            WHEN 'Otro'       THEN 4
            WHEN 'Particular' THEN 5
            WHEN 'N/D'        THEN 6
            ELSE 7   -- N/A y otros
        END                                                         AS jerarquia_admin,
        CASE TRIM(r.tipo_vial)
            WHEN 'Carretera'         THEN 1
            WHEN 'Autopista'         THEN 1
            WHEN 'Boulevard'         THEN 2
            WHEN 'Avenida'           THEN 3
            WHEN 'Calzada'           THEN 4
            WHEN 'Eje vial'          THEN 5
            WHEN 'Periférico'        THEN 6
            WHEN 'Enlace'            THEN 7
            WHEN 'Corredor'          THEN 8
            WHEN 'Viaducto'          THEN 9
            WHEN 'Circunvalación'    THEN 10
            WHEN 'Circuito'          THEN 11
            WHEN 'Camino'            THEN 12
            WHEN 'Calle'             THEN 13
            WHEN 'Diagonal'          THEN 14
            WHEN 'Prolongación'      THEN 15
            WHEN 'Continuación'      THEN 16
            WHEN 'Ampliación'        THEN 17
            WHEN 'Privada'           THEN 18
            WHEN 'Cerrada'           THEN 19
            WHEN 'Retorno'           THEN 20
            WHEN 'Retorno U'         THEN 21
            WHEN 'Rampa de frenado'  THEN 22
            WHEN 'Glorieta'          THEN 23
            WHEN 'Andador'           THEN 24
            WHEN 'Peatonal'          THEN 25
            WHEN 'Vereda'            THEN 26
            WHEN 'Callejón'          THEN 27
            ELSE 28   -- Otro y no catalogados
        END                                                         AS jerarquia_tipo_vial,
        (TRIM(r.cond_pav) = 'Con pavimento')                        AS es_pavimentado,
        ST_X(vs.the_geom)                                           AS x1,
        ST_Y(vs.the_geom)                                           AS y1,
        ST_X(vt.the_geom)                                           AS x2,
        ST_Y(vt.the_geom)                                           AS y2
    FROM atlas.c_rnc r
    LEFT JOIN atlas.c_rnc_vertices_pgr vs ON vs.id = r.source
    LEFT JOIN atlas.c_rnc_vertices_pgr vt ON vt.id = r.target
    WHERE r.source IS NOT NULL
      AND r.target IS NOT NULL
),
costed AS (
    SELECT
        b.*,
        -- Prioridad 1: pavimentado. Prioridad 2: longitud real (más corto).
        -- Admin / tipo_vial solo desempatan (~30 m / ~10 m por nivel).
        (
            b.longitud_m
            + CASE WHEN NOT b.es_pavimentado THEN 500000.0 ELSE 0.0 END
            + (b.jerarquia_admin - 1) * 30.0
            + (b.jerarquia_tipo_vial - 1) * 10.0
        ) AS cost_pref,
        (
            b.longitud_m
            + CASE
                WHEN TRIM(COALESCE(b.peaje, 'No')) IN ('Si', 'Sí')
                  OR TRIM(COALESCE(b.tipo_vial, '')) ILIKE 'Autopista'
                  OR TRIM(COALESCE(b.tipo_vial, '')) ILIKE '%Cuota%'
                THEN 50000000.0 ELSE 0.0
              END
            + CASE WHEN NOT b.es_pavimentado THEN 5000000.0 ELSE 0.0 END
            + CASE WHEN TRIM(COALESCE(b.recubri, '')) IN ('Tierra', 'Grava')
                THEN 3000000.0 ELSE 0.0 END
            + CASE WHEN TRIM(COALESCE(b.tipo_vial, '')) IN (
                'Camino', 'Callejón', 'Vereda', 'Andador', 'Peatonal'
              ) THEN 1500000.0 ELSE 0.0 END
            + (b.jerarquia_admin - 1) * 30.0
            + (b.jerarquia_tipo_vial - 1) * 10.0
        ) AS cost_sin_peaje_pref,
        CASE
            WHEN TRIM(COALESCE(b.peaje, 'No')) IN ('Si', 'Sí')
              OR TRIM(COALESCE(b.tipo_vial, '')) ILIKE 'Autopista'
              OR TRIM(COALESCE(b.tipo_vial, '')) ILIKE '%Cuota%'
            THEN 'Si'
            ELSE COALESCE(NULLIF(TRIM(b.peaje), ''), 'No')
        END AS peaje_norm
    FROM base b
)
SELECT
    gid,
    source,
    target,
    GREATEST(cost_pref, 1.0)              AS cost,
    GREATEST(cost_pref, 1.0)              AS reverse_cost,
    GREATEST(cost_sin_peaje_pref, 1.0)   AS cost_sin_peaje,
    GREATEST(cost_sin_peaje_pref, 1.0)   AS reverse_cost_sin_peaje,
    x1, y1, x2, y2,
    tipo_vial                             AS tipo_camino,
    tipo_vial,
    velocidad_kmh,
  -- jerarquia_vial resumida (menor = mejor vía)
    (jerarquia_admin * 100 + jerarquia_tipo_vial)::SMALLINT AS jerarquia_vial,
    jerarquia_admin,
    jerarquia_tipo_vial,
    circula                               AS sentido,
    es_pavimentado,
    cond_pav,
    recubri                               AS recubrimiento,
    administra,
    peaje_norm                            AS peaje,
    nombre,
    longitud_m
FROM costed;

-- -----------------------------------------------------------------------------
-- 4) Índices
-- -----------------------------------------------------------------------------
CREATE INDEX idx_c_rnc_routing_source ON atlas.c_rnc_routing (source);
CREATE INDEX idx_c_rnc_routing_target ON atlas.c_rnc_routing (target);
CREATE INDEX idx_c_rnc_routing_admin  ON atlas.c_rnc_routing (administra);
CREATE INDEX idx_c_rnc_routing_tipo   ON atlas.c_rnc_routing (tipo_vial);
CREATE INDEX idx_c_rnc_routing_peaje  ON atlas.c_rnc_routing (peaje);

ANALYZE atlas.c_rnc_routing;

COMMIT;

-- -----------------------------------------------------------------------------
-- 5) Verificación rápida
-- -----------------------------------------------------------------------------
SELECT COUNT(*) AS filas FROM atlas.c_rnc_routing;

SELECT administra, COUNT(*) AS n, ROUND((SUM(longitud_m) / 1000.0)::numeric, 1) AS km
  FROM atlas.c_rnc_routing
 GROUP BY administra
 ORDER BY n DESC;

SELECT peaje, COUNT(*) FROM atlas.c_rnc_routing GROUP BY peaje;

SELECT COUNT(*) FILTER (WHERE x1 IS NULL OR x2 IS NULL) AS sin_coordenadas_astar
  FROM atlas.c_rnc_routing;
