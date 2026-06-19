# Consulta tabular del visor geográfico

## Resumen

Módulo para consultar y exportar atributos alfanuméricos de capas temáticas del **Visor geográfico**, filtrados por el municipio seleccionado en el explorador lateral.

Implementación:

| Componente | Ubicación |
|------------|-----------|
| Lógica PostGIS + Excel | `app_api/visor_tabular.py` |
| Endpoints REST | `app_api/routers/api.py` |
| UI (botón + modal) | `htdocs/atlas_gro/js/visorTabular.js` |
| Cliente HTTP | `htdocs/atlas_gro/js/visorTabularApi.js` |
| Estilos | `htdocs/atlas_gro/css/main.css` (`.visor-tabular-*`) |

## Flujo de usuario

1. En el visor, panel **Capas**, pulsar el botón con icono de tabla (derecha del texto «Activar / desactivar»).
2. En el modal, elegir la capa (por ahora solo **Localidades**).
3. Pulsar **Generar tabla** → consulta `atlas.c_loc_punto` filtrada por `cve_mun`.
4. Revisar el resumen (total de localidades) y la tabla en pantalla.
5. **Exportar Excel** → descarga `.xlsx` generado en el backend con **openpyxl**.

El municipio activo proviene de `state.selectedMunicipio` (`app.js` → `visorLayerPanelOptions()`).

## API REST

Base: mismo host que el Atlas (`/api/...` vía Nginx → FastAPI).

### Catálogo de capas tabulares

```
GET /api/visor/tabla/capas
```

Respuesta:

```json
{
  "ok": true,
  "layers": [
    { "id": "locspunto", "label": "Localidades", "table": "c_loc_punto" }
  ]
}
```

### Consulta JSON

```
GET /api/visor/tabla?layer=locspunto&cve_mun=007
```

Parámetros:

| Parámetro | Obligatorio | Descripción |
|-----------|-------------|-------------|
| `layer` | Sí | Id de capa (`locspunto`) |
| `cve_mun` | Sí | Clave municipal de 3 dígitos |

Respuesta (extracto):

```json
{
  "ok": true,
  "layer": "locspunto",
  "layer_label": "Localidades",
  "table": "c_loc_punto",
  "cve_mun": "007",
  "nom_mun": "Arcelia",
  "total_registros": 42,
  "columns": [{ "field": "cvegeo", "label": "Clave geográfica" }],
  "rows": [{ "cvegeo": "120070001", "...": "..." }]
}
```

### Exportación Excel

```
GET /api/visor/tabla/export?layer=locspunto&cve_mun=007&format=xlsx
```

- `Content-Type`: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- Incluye título, metadatos del municipio, total de localidades y tabla con encabezados legibles.

## Capa Localidades (`locspunto`)

Tabla: `atlas.c_loc_punto`

Columnas solicitadas (resolución dinámica vía `information_schema`):

| Campo respuesta | Encabezado |
|-----------------|------------|
| `cvegeo` | Clave geográfica |
| `cve_ent` | Clave de entidad |
| `nom_ent` | Nombre de entidad |
| `cve_mun` | Clave del municipio |
| `nom_mun` | Nombre del municipio |
| `cve_loc` | Clave de la localidad |
| `nom_loc` | Nombre de la localidad |
| `ambito` | Ámbito |
| `altitud` | Altitud |
| `pob_total` | Población total |
| `pob_mascul` | Población masculina |
| `pob_femeni` | Población femenina |
| `total_viv` | Total de viviendas |

Para viviendas se prueban, en orden: `total de v` (nombre literal en `c_loc_punto`), `total_de_v`, `total_de_viv`, `total_viv`, `vivtot`, `total_viviendas`.

Orden de filas: ascendente por **clave de localidad** (`cve_loc`, numérico 0001…9999); desempate por `cvegeo`.

Filtro municipal: `mun_where_sql()` (misma lógica que exportación KML/SHP).

Límite de filas: 25 000 por consulta.

## Códigos de error

| Código | HTTP | Significado |
|--------|------|-------------|
| `UNKNOWN_LAYER` | 400 | Capa no habilitada para tabular |
| `MISSING_CVE_MUN` | 400 | `cve_mun` vacío o inválido |
| `NO_ROWS` | 404 | Sin registros en el municipio |
| `EXPORT_FAILED` | 500 | Fallo al generar Excel |

## Extensión a nuevas capas

1. Añadir el id en `_TABULAR_LAYERS` (`visor_tabular.py`).
2. Implementar `fetch_<capa>_table()` con columnas y tabla.
3. Registrar la capa en `list_tabular_layers()`.
4. Ampliar el catálogo en frontend si se requiere lógica específica en el modal.

## Simbología de localidades en mapa

La capa `locsPunto` usa icono de chincheta (`mapLocsPuntoIcons.js`) en lugar de círculos azules. La leyenda **Simbología** refleja el mismo icono (`visorMapLegend.js`).

## Despliegue

Tras cambios en `app_api/`:

```bash
docker compose restart api_backend
```

Recargar el visor con **Ctrl+F5**.
