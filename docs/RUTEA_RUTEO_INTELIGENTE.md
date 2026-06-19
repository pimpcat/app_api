# Ruteo inteligente — Guía SQL (pgAdmin)

Instrucciones para ampliar `atlas.c_rnc_routing` con prioridades viales, peajes, coordenadas A* y atributos para el **resumen de ruta** (km por tipo vial, administración, pavimento, etc.).

**Script listo para ejecutar:** [`sql/c_rnc_routing_inteligente.sql`](sql/c_rnc_routing_inteligente.sql)

**Relacionado:** [`RUTEA_TABLA_ROUTING.md`](RUTEA_TABLA_ROUTING.md) (versión básica anterior)

---

## 1. Objetivo

| Necesidad | Solución en BD |
|-----------|----------------|
| Priorizar Federales > Estatales > Municipales > … | Penalización en `cost` según `administra` |
| Preferir pavimentado (`Con pavimento`) | +2500 m de penalización si no está pavimentado |
| Priorizar `tipo_vial` (Carretera, Avenida, Calle…) | Penalización según jerarquía de tipo |
| Evitar peajes (toggle en UI) | Columnas `cost_sin_peaje` / `reverse_cost_sin_peaje` |
| `pgr_astar` más rápido | Columnas `x1`, `y1`, `x2`, `y2` desde vértices |
| Resumen km por categoría | Atributos almacenados en cada arista de la ruta |

La API elegirá en tiempo de ejecución:
- **Con peajes:** `cost`, `reverse_cost`
- **Sin peajes:** `cost_sin_peaje`, `reverse_cost_sin_peaje`

La **distancia real** en km del resumen se calcula sumando `longitud_m` de los tramos (no el costo penalizado).

---

## 2. Campos nuevos en `c_rnc_routing`

| Campo | Origen en `c_rnc` | Uso |
|-------|-------------------|-----|
| `tipo_camino` | `tipo_vial` | Alias / catálogo |
| `tipo_vial` | `tipo_vial` | Resumen por tipo |
| `velocidad_kmh` | `velocidad` (numérico) | Estadística / futuro |
| `jerarquia_admin` | `administra` | 1=Federal … 7=N/A |
| `jerarquia_tipo_vial` | `tipo_vial` | 1=Carretera … 28=Otro |
| `jerarquia_vial` | calculado | Combinada (admin×100 + tipo) |
| `sentido` | `circula` | Resumen (Dos sentidos, Un sentido…) |
| `es_pavimentado` | `cond_pav = 'Con pavimento'` | Booleano |
| `cond_pav` | `cond_pav` | Resumen |
| `recubrimiento` | `recubri` | Resumen (Asfalto, Tierra…) |
| `administra` | `administra` | Resumen |
| `peaje` | `peaje` | Si / No |
| `longitud_m` | `cost`, `longitud` o `ST_Length` | Km reales del tramo |
| `x1`,`y1`,`x2`,`y2` | vértices `source`/`target` | `pgr_astar` |

---

## 3. Prioridades configuradas (v2 — camino corto pavimentado)

**Orden de decisión del algoritmo:**

1. **Pavimentado** (`Con pavimento`) — tramos sin pavimentar reciben +500 000 m de penalización (casi excluidos).
2. **Distancia real** (`longitud_m`) — entre opciones pavimentadas gana la más corta.
3. **Administración / tipo vial** — solo desempate leve (+30 m / +10 m por nivel), **sin** forzar federales largas.

### 3.1 Administración (`jerarquia_admin`) — desempate

| Valor `administra` | Rango | Penalización extra |
|--------------------|-------|--------------------|
| Federal | 1 | 0 m |
| Estatal | 2 | +30 m |
| Municipal | 3 | +60 m |
| Otro | 4 | +90 m |
| Particular | 5 | +120 m |
| N/D | 6 | +150 m |
| N/A u otros | 7 | +180 m |

### 3.2 Tipo vial — desempate (+10 m por nivel)

Carretera / Autopista = 1 … Otro = 28 (ver lista completa en script SQL).

### 3.3 Pavimento

| `cond_pav` | Penalización |
|------------|--------------|
| Con pavimento | 0 |
| Sin pavimento / N/A | **+500 000 m** |

### 3.4 Peajes (toggle UI «Usar carreteras de peaje»)

| Modo | Comportamiento |
|------|----------------|
| **ON** | Grafo completo |
| **OFF** | **Excluye** aristas con `peaje = Si` (y tipo Autopista / Cuota) del grafo |

Tras cambiar prioridades, ejecutar: [`sql/c_rnc_routing_refresh_costos.sql`](sql/c_rnc_routing_refresh_costos.sql)

---

## 4. Qué ejecutar en pgAdmin

### Opción A — Script completo (recomendado)

1. Abrir pgAdmin → base **`atlas`** → Query Tool.
2. Abrir el archivo  
   `Stack_Martin/app_api/docs/sql/c_rnc_routing_inteligente.sql`
3. Ejecutar **todo** (F5).
4. Revisar que al final aparezcan resultados de verificación sin errores.

### Opción B — Pasos manuales resumidos

```sql
-- 1) Borrar tabla anterior
DROP TABLE IF EXISTS atlas.c_rnc_routing CASCADE;

-- 2) Crear + poblar + índices
-- (copiar el contenido completo de sql/c_rnc_routing_inteligente.sql)

-- 3) Reiniciar API
-- docker compose restart api_backend
```

> **Importante:** este script **reemplaza** la tabla `c_rnc_routing` anterior. Los índices se recrean automáticamente.

---

## 5. Verificación después de ejecutar

```sql
-- Total de aristas
SELECT COUNT(*) AS filas FROM atlas.c_rnc_routing;

-- Distribución por administración
SELECT administra,
       COUNT(*) AS tramos,
       ROUND((SUM(longitud_m) / 1000.0)::numeric, 2) AS km
  FROM atlas.c_rnc_routing
 GROUP BY administra
 ORDER BY MIN(jerarquia_admin);

-- Pavimento
SELECT cond_pav,
       es_pavimentado,
       COUNT(*) AS tramos,
       ROUND((SUM(longitud_m) / 1000.0)::numeric, 2) AS km
  FROM atlas.c_rnc_routing
 GROUP BY cond_pav, es_pavimentado;

-- Peajes
SELECT peaje, COUNT(*) FROM atlas.c_rnc_routing GROUP BY peaje;

-- Coordenadas A* (debería ser 0 o muy bajo)
SELECT COUNT(*) FILTER (WHERE x1 IS NULL OR x2 IS NULL) AS sin_xy
  FROM atlas.c_rnc_routing;

-- Muestra de costos
SELECT id, administra, tipo_vial, peaje,
       ROUND(longitud_m::numeric, 1) AS m_real,
       ROUND(cost::numeric, 1) AS costo_ruta,
       ROUND(cost_sin_peaje::numeric, 1) AS costo_sin_peaje
  FROM atlas.c_rnc_routing
 WHERE administra = 'Federal'
 LIMIT 5;
```

---

## 6. Reiniciar la API

Tras recrear la tabla, reiniciar el contenedor para limpiar caché de esquema:

```powershell
cd c:\Stack_Martin
docker compose restart api_backend
```

---

## 7. Mantenimiento (cuando cambie `c_rnc`)

No basta con `INSERT` parcial: hay que **regenerar** toda la tabla porque los costos penalizados dependen de los atributos.

**Vuelva a ejecutar el script completo**  
`sql/c_rnc_routing_inteligente.sql`  
o guarde este bloque como «Refresco ruteo inteligente»:

```sql
TRUNCATE atlas.c_rnc_routing;
-- Luego ejecutar solo el bloque INSERT … FROM del script principal
-- y ANALYZE atlas.c_rnc_routing;
```

La forma más segura es **re-ejecutar el script entero** (DROP + CREATE + INSERT).

---

## 8. Resumen de ruta (lo que mostrará la API)

Tras implementar el backend, cada respuesta incluirá agregados por los tramos de la ruta:

```json
"resumen": {
  "longitud_km": 42.0,
  "por_tipo_vial": { "Carretera": 28.5, "Calle": 8.2, "Camino": 5.3 },
  "por_administracion": { "Federal": 30.1, "Estatal": 11.9 },
  "por_cond_pav": { "Con pavimento": 40.0, "Sin pavimento": 2.0 },
  "por_recubrimiento": { "Asfalto": 38.0, "Tierra": 2.0 },
  "por_peaje": { "Si": 12.0, "No": 30.0 },
  "tramos_peaje": 3,
  "evitar_peajes": false
}
```

Los km se obtienen de `SUM(longitud_m) / 1000` agrupando por cada categoría.

---

## 9. Ajustar agresividad de las prioridades

Si el ruteo se desvía demasiado o poco respecto a carreteras federales, edite en el script las constantes:

```sql
(b.jerarquia_admin - 1)    * 800.0   -- administración
(b.jerarquia_tipo_vial - 1) * 150.0   -- tipo vial
CASE WHEN NOT b.es_pavimentado THEN 2500.0 ELSE 0.0 END
CASE WHEN TRIM(b.peaje) = 'Si' THEN 50000.0 ELSE 0.0 END
```

Vuelva a ejecutar el script completo tras cualquier cambio.

---

## 10. Próximo paso (código — pendiente)

Una vez ejecutado el SQL en pgAdmin, el desarrollo en aplicación incluirá:

1. **API** — parámetro `evitar_peajes=true|false` y uso de `cost` vs `cost_sin_peaje`.
2. **API** — bloque `resumen` con desglose de km.
3. **UI** — switch pequeño «Considerar peajes» en el panel de ruteo.
4. Opcional — migrar de `pgr_dijkstra` a `pgr_astar` usando `x1,y1,x2,y2`.

Indique cuando haya ejecutado el SQL y procedemos con la API y el frontend.

---

*Atlas Municipal de Guerrero — RNC / pgRouting*
