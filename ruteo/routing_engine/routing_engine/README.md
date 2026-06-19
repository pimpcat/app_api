# Motor de ruteo (routing_engine)

## Scoring engine (heurísticas Atlas)

Toda la puntuación dinámica se configura en un solo archivo:

**`scoring_config.py`** — tablas de factores (tipo, peaje, recubrimiento, etc.)

**`scoring.py`** — funciones `factor_*()` que generan SQL multiplicador.

**`costs.py`** — `build_cost_sql()` ensambla:

```
costo_final = GREATEST(costo_base × Π factor_i, 1.0)
```

### Factores

| Función | Columna RNC | Rol |
|---------|-------------|-----|
| `factor_tipo` | tipo_vial | Jerarquía vial |
| `factor_velocidad` | velocidad, tipo_vial | Modo tiempo |
| `factor_peaje` | peaje, tipo_vial, nombre | Peaje explícito e implícito |
| `factor_superficie` | cond_pav | Pavimento / terracería |
| `factor_recubrimiento` | recubrimiento | Asfalto, tierra, grava… |
| `factor_condicion` | condicion | Obra / operación |
| `factor_carriles` | carriles | Capacidad |
| `factor_administracion` | administra | Federal, estatal… |
| `factor_circulacion` | circulacion | Sentido (cost / reverse_cost) |

Para cambiar un peso: editar solo `scoring_config.py`.

## Flujo del endpoint `/api/ruteo`

```
calcular_ruta_rnc (ruteo.py → fachada)
  → engine.calcular_ruta_rnc
    → cached_schema_snapshot()
    → runner.run_route()
      → try_stitched_corridor_route (strategies/od_corridor)  [solo sin peajes]
      → graph_candidates() → build_graph_sql() + build_cost_sql()
      → fetch_dijkstra_path()  [pgr_dijkstra]
      → build_route_geom_json() + build_route_resumen()
```

- **Con peajes**: `CostProfile.MATERIALIZED` + `c_rnc_routing.cost`
- **Sin peajes**: `CostProfile.LEGACY_SIN_PEAJE` (paridad histórica) vía `legacy_od_cost.py`
- **Stitch OD**: `CostProfile.DISTANCE_OD` + `GraphVariant.CORRIDOR_SUBGRAPH`
- **Futuro / tuning**: `CostProfile.SCORING` + `scoring_config.py`
