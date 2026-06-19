-- =============================================================================
-- Actualizar costos y peajes en atlas.c_rnc_routing (sin recrear la tabla)
-- Ejecutar en pgAdmin después de cambiar criterios de ruteo.
-- =============================================================================

BEGIN;

-- 1) Sincronizar peaje desde c_rnc (fuente de verdad)
UPDATE atlas.c_rnc_routing r
   SET peaje = TRIM(c.peaje)
  FROM atlas.c_rnc c
 WHERE c.gid = r.id
   AND TRIM(COALESCE(r.peaje, '')) IS DISTINCT FROM TRIM(COALESCE(c.peaje, ''));

-- 2) Refinar flag de peaje (heurísticas adicionales)
UPDATE atlas.c_rnc_routing r
   SET peaje = 'Si'
  FROM atlas.c_rnc c
 WHERE c.gid = r.id
   AND (
     TRIM(COALESCE(c.peaje, '')) IN ('Si', 'Sí')
     OR TRIM(COALESCE(c.tipo_vial, '')) ILIKE 'Autopista'
     OR TRIM(COALESCE(c.nombre, '')) ILIKE '%autopista%'
     OR TRIM(COALESCE(c.nombre, '')) ILIKE '%cuota%'
     OR TRIM(COALESCE(c.nombre, '')) ILIKE '%peaje%'
   );

-- 3) Recalcular costos
--    cost: prioridad pavimentado + jerarquía (permite peajes)
--    cost_sin_peaje: mismas reglas + penalización fuerte a peaje y terracerías
UPDATE atlas.c_rnc_routing r
   SET cost = sub.cost_con_peaje,
       reverse_cost = sub.cost_con_peaje,
       cost_sin_peaje = sub.cost_sin_peaje,
       reverse_cost_sin_peaje = sub.cost_sin_peaje
  FROM (
    SELECT id,
           GREATEST(
             longitud_m
             + CASE WHEN NOT es_pavimentado THEN 500000.0 ELSE 0.0 END
             + (jerarquia_admin - 1) * 30.0
             + (jerarquia_tipo_vial - 1) * 10.0,
             1.0
           ) AS cost_con_peaje,
           GREATEST(
             longitud_m
             * CASE
                 WHEN TRIM(COALESCE(tipo_vial, '')) = 'Carretera'
                  AND TRIM(COALESCE(administra, '')) = 'Federal'
                  AND es_pavimentado
                 THEN 0.35 ELSE 1.0
               END
             + CASE
                 WHEN UPPER(TRIM(COALESCE(peaje, 'No'))) IN ('SI', 'SÍ')
                   OR TRIM(COALESCE(tipo_vial, '')) ILIKE 'Autopista'
                   OR TRIM(COALESCE(tipo_vial, '')) ILIKE '%Cuota%'
                 THEN 50000000.0 ELSE 0.0
               END
             + CASE WHEN NOT es_pavimentado THEN 50000000.0 ELSE 0.0 END
             + CASE WHEN TRIM(COALESCE(recubrimiento, '')) IN ('Tierra', 'Grava')
                 THEN 40000000.0 ELSE 0.0 END
             + CASE WHEN TRIM(COALESCE(tipo_vial, '')) IN (
                 'Camino', 'Callejón', 'Vereda', 'Andador', 'Peatonal'
               ) THEN 10000000.0 ELSE 0.0 END
             + (jerarquia_admin - 1) * 30.0
             + (jerarquia_tipo_vial - 1) * 10.0,
             1.0
           ) AS cost_sin_peaje
      FROM atlas.c_rnc_routing
  ) sub
 WHERE r.id = sub.id;

ANALYZE atlas.c_rnc_routing;

COMMIT;

-- Verificación peajes
SELECT peaje, COUNT(*) AS tramos,
       ROUND((SUM(longitud_m) / 1000.0)::numeric, 1) AS km
  FROM atlas.c_rnc_routing
 GROUP BY peaje
 ORDER BY tramos DESC;
