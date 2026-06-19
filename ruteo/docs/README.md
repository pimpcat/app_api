# Documentación del módulo de ruteo

| Archivo | Descripción |
|---------|-------------|
| [RUTEA_TABLA_ROUTING.md](RUTEA_TABLA_ROUTING.md) | Tabla `c_rnc_routing` — instalación y mantenimiento |
| [RUTEA_RUTEO_INTELIGENTE.md](RUTEA_RUTEO_INTELIGENTE.md) | Ruteo con prioridades y peajes |
| [RUTEA_PERFORMANCE.md](RUTEA_PERFORMANCE.md) | Análisis de rendimiento |
| [MOTOR_README.md](MOTOR_README.md) | Motor `routing_engine` y scoring |
| [RECOMMENDED_INDEXES.sql](RECOMMENDED_INDEXES.sql) | Índices propuestos |
| [sql/c_rnc_routing_inteligente.sql](sql/c_rnc_routing_inteligente.sql) | Script SQL completo |
| [sql/c_rnc_routing_refresh_costos.sql](sql/c_rnc_routing_refresh_costos.sql) | Refresco de costos |

Portal web: `htdocs/ruteo/docs/`

Para mover copias duplicadas desde `app_api/docs/` y el motor físico a este paquete:

```powershell
cd c:\Stack_Martin\app_api
ruteo\run_reorganize.bat
```
