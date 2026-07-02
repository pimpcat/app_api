"""API admin del catálogo del Visor geográfico (Fase 1–3)."""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from visor_icon_upload import register_custom_icon
from visor_shp_import import import_shapefile
from auth.deps import require_admin_user
from visor_catalog_admin_service import (
    admin_meta,
    create_group_from_payload,
    create_layer_from_payload,
    delete_catalog_group,
    delete_managed_layer,
    get_layer_admin_detail,
    list_catalog_audit,
    list_catalog_groups,
    list_managed_layers,
    list_publishable_tables,
    list_table_columns,
    list_column_distinct_values,
    record_indexes_audit,
    record_shp_import_audit,
    table_publish_status,
    update_catalog_group_label,
    update_layer_from_payload,
    validate_new_layer,
)
from visor_table_indexes import apply_table_indexes, build_index_plan, list_table_indexes

router = APIRouter(prefix="/api/visor/admin", tags=["visor-admin"])


class LayerStyleBody(BaseModel):
    color: Optional[str] = None
    halo_color: Optional[str] = None
    width: Optional[float] = None
    halo_width: Optional[float] = None
    opacity: Optional[float] = None
    icon_key: Optional[str] = None


class DataFilterBody(BaseModel):
    field: Optional[str] = Field(None, max_length=128)
    values: Optional[List[str]] = None
    codigo_act: Optional[List[int]] = None


class LayerDataBody(BaseModel):
    table: str = Field(..., min_length=1, max_length=128)
    mun_filter: Union[str, bool] = "cve_mun"
    export_columns: Optional[List[str]] = None
    filter: Optional[DataFilterBody] = None


class LayerCapabilitiesBody(BaseModel):
    export: List[str] = Field(default_factory=list)
    tabular: bool = False
    spatial_analysis: bool = False


class IdentifyFieldBody(BaseModel):
    column: str = Field(..., min_length=1, max_length=128)
    label: Optional[str] = Field(None, max_length=200)


class IdentifyBody(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    fields: List[Union[str, IdentifyFieldBody]] = Field(default_factory=list)


class LabelsBody(BaseModel):
    enabled: Optional[bool] = True
    field: Optional[str] = Field(None, max_length=128)
    minzoom: Optional[float] = Field(None, ge=8, le=20)
    above_icon: Optional[bool] = True
    color: Optional[str] = Field(None, max_length=32)
    offset: Optional[List[float]] = None
    source: Optional[str] = Field(None, max_length=32)


class StyleClassBody(BaseModel):
    value: str = Field(..., min_length=1, max_length=64)
    color: str = Field(..., min_length=4, max_length=32)
    label: Optional[str] = Field(None, max_length=120)


class ClusterStyleBody(BaseModel):
    enabled: Optional[bool] = False
    preset: Optional[str] = Field(None, max_length=32)


class LayerStyleBodyExtended(BaseModel):
    color: Optional[str] = None
    halo_color: Optional[str] = None
    width: Optional[float] = None
    halo_width: Optional[float] = None
    opacity: Optional[float] = None
    icon_key: Optional[str] = None
    field: Optional[str] = Field(None, max_length=128)
    default_color: Optional[str] = Field(None, max_length=32)
    classes: Optional[List[StyleClassBody]] = None
    outline_color: Optional[str] = Field(None, max_length=32)
    outline_width: Optional[float] = None
    outline_opacity: Optional[float] = None
    radius: Optional[float] = None
    stroke_color: Optional[str] = Field(None, max_length=32)
    stroke_width: Optional[float] = None
    line_dash: Optional[List[float]] = None
    icon_scale: Optional[float] = None
    icon_offset: Optional[List[float]] = None
    minzoom: Optional[float] = Field(None, ge=0, le=22)
    cluster: Optional[ClusterStyleBody] = None


class DenueBody(BaseModel):
    codigo_act: List[int] = Field(default_factory=list)
    use_template: bool = True


class SearchBody(BaseModel):
    enabled: bool = False
    tipo: Optional[str] = Field(None, max_length=120)
    name_column: Optional[str] = Field(None, max_length=128)
    id_column: Optional[str] = Field(None, max_length=128)
    search_columns: Optional[List[str]] = None
    scope: Optional[str] = Field(None, max_length=32)
    geom_mode: Optional[str] = Field(None, max_length=32)
    highlight: Optional[bool] = True


class SpatialFieldBody(BaseModel):
    columna: str = Field(..., min_length=1, max_length=128)
    etiqueta: Optional[str] = Field(None, max_length=200)
    agregacion: Optional[str] = Field("sum", pattern="^(sum|avg)$")


class SpatialDetailColumnBody(BaseModel):
    columna: str = Field(..., min_length=1, max_length=128)
    etiqueta: Optional[str] = Field(None, max_length=200)


class SpatialUiBody(BaseModel):
    unidad_registro: Optional[str] = Field(None, max_length=120)
    empty_msg: Optional[str] = Field(None, max_length=300)


class SpatialSectionBody(BaseModel):
    titulo: Optional[str] = Field(None, max_length=120)
    campos: List[SpatialFieldBody] = Field(default_factory=list)


class SpatialAnalysisBody(BaseModel):
    modo: Optional[str] = Field(None, pattern="^(agregacion|conteo)$")
    geom_column: Optional[str] = Field(None, max_length=128)
    grupo: Optional[str] = Field(None, max_length=64)
    detail_table: bool = False
    sections: Optional[List[SpatialSectionBody]] = None
    detail_columns: Optional[List[SpatialDetailColumnBody]] = None
    ui: Optional[SpatialUiBody] = None


class TabularColumnBody(BaseModel):
    field: str = Field(..., min_length=1, max_length=128)
    label: Optional[str] = Field(None, max_length=200)
    candidates: Optional[List[str]] = None


class TabularBody(BaseModel):
    preset: Optional[str] = Field(None, max_length=32)
    columns: Optional[List[TabularColumnBody]] = None


class ApplyIndexItemBody(BaseModel):
    column: str = Field(..., min_length=1, max_length=128)
    method: str = Field("btree", pattern="^(gist|btree)$")
    index_name: Optional[str] = Field(None, max_length=128)
    reason: Optional[str] = Field(None, max_length=300)


class ApplyIndexesBody(BaseModel):
    items: List[ApplyIndexItemBody] = Field(default_factory=list)
    only_missing: bool = True


class CreateGroupBody(BaseModel):
    label: str = Field(..., min_length=2, max_length=120)
    group_id: Optional[str] = Field(None, min_length=2, max_length=64)


class UpdateGroupBody(BaseModel):
    label: str = Field(..., min_length=2, max_length=120)


class CreateLayerBody(BaseModel):
    layer_id: str = Field(..., min_length=2, max_length=64)
    label: str = Field(..., min_length=2, max_length=200)
    group_id: str = Field(..., min_length=2, max_length=64)
    geometry: str = Field(..., pattern="^(point|line|polygon)$")
    style_preset: str = Field(..., min_length=2, max_length=64)
    style: LayerStyleBodyExtended = Field(default_factory=LayerStyleBodyExtended)
    data: LayerDataBody
    capabilities: LayerCapabilitiesBody = Field(default_factory=LayerCapabilitiesBody)
    identify: Optional[IdentifyBody] = None
    labels: Optional[LabelsBody] = None
    denue: Optional[DenueBody] = None
    search: Optional[SearchBody] = None
    spatial_analysis: Optional[SpatialAnalysisBody] = None
    tabular: Optional[TabularBody] = None
    overlay_key: Optional[str] = None
    checkbox_id: Optional[str] = None


def _http_from_value_error(exc: ValueError) -> HTTPException:
    raw = str(exc)
    if raw.startswith("VALIDATION:"):
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": "VALIDATION", "message": raw.split(":", 1)[1]},
        )
    if raw.startswith("LAYER_EXISTS:"):
        return HTTPException(
            status_code=409,
            detail={"ok": False, "error": "LAYER_EXISTS", "message": raw.split(":", 1)[1]},
        )
    if raw.startswith("LAYER_NOT_FOUND:"):
        return HTTPException(
            status_code=404,
            detail={"ok": False, "error": "LAYER_NOT_FOUND", "message": raw.split(":", 1)[1]},
        )
    if raw == "LAYER_NOT_MANAGED":
        return HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "error": "LAYER_NOT_MANAGED",
                "message": "Solo se pueden editar capas publicadas desde Visor Studio",
            },
        )
    if raw.startswith("UNKNOWN_GROUP:"):
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": "UNKNOWN_GROUP", "message": raw.split(":", 1)[1]},
        )
    if raw.startswith("GROUP_EXISTS:"):
        return HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "error": "GROUP_EXISTS",
                "message": f"Ya existe un grupo con id «{raw.split(':', 1)[1]}»",
            },
        )
    if raw.startswith("GROUP_NOT_FOUND:"):
        return HTTPException(
            status_code=404,
            detail={
                "ok": False,
                "error": "GROUP_NOT_FOUND",
                "message": f"Grupo «{raw.split(':', 1)[1]}» no encontrado",
            },
        )
    if raw.startswith("GROUP_NOT_EMPTY:"):
        parts = raw.split(":")
        count = parts[2] if len(parts) > 2 else "?"
        return HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "error": "GROUP_NOT_EMPTY",
                "message": f"El grupo aún tiene {count} capa(s). Muévalas a otro grupo o despublíquelas antes de eliminarlo.",
            },
        )
    if raw in ("INVALID_GROUP_ID", "INVALID_GROUP_LABEL"):
        messages = {
            "INVALID_GROUP_ID": "Identificador de grupo inválido (mín. 2 caracteres, minúsculas, números y _)",
            "INVALID_GROUP_LABEL": "Etiqueta del grupo inválida (mín. 2 caracteres)",
        }
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": raw, "message": messages.get(raw, raw)},
        )
    if raw.startswith("MARTIN_UNAVAILABLE:"):
        return HTTPException(
            status_code=503,
            detail={"ok": False, "error": "MARTIN_UNAVAILABLE", "message": "Martin no responde"},
        )
    if raw == "INVALID_TABLE" or raw == "TABLE_NOT_FOUND":
        return HTTPException(
            status_code=404,
            detail={"ok": False, "error": raw, "message": "Tabla no encontrada en PostGIS"},
        )
    if raw == "TABLE_NOT_PUBLISHABLE":
        return HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "error": raw,
                "message": "La tabla no está en Martin ni en el catálogo del visor",
            },
        )
    if raw == "INVALID_IDENTIFIER":
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": raw, "message": "Identificador SQL inválido"},
        )
    if raw == "INVALID_COLUMN" or raw == "COLUMN_NOT_FOUND":
        return HTTPException(
            status_code=404,
            detail={"ok": False, "error": raw, "message": "Columna no encontrada en la tabla"},
        )
    if raw.startswith("TABLE_EXISTS:"):
        return HTTPException(
            status_code=409,
            detail={"ok": False, "error": "TABLE_EXISTS", "message": f"La tabla {raw.split(':', 1)[1]} ya existe"},
        )
    if raw.startswith("ICON_EXISTS:"):
        return HTTPException(
            status_code=409,
            detail={"ok": False, "error": "ICON_EXISTS", "message": f"El icono {raw.split(':', 1)[1]} ya existe"},
        )
    if raw in ("UNSUPPORTED_FORMAT", "ZIP_WITHOUT_SHP", "ZIP_PATH_TRAVERSAL", "SHP_NOT_FOUND", "EMPTY_FILE", "INVALID_SVG", "INVALID_SVG_SIZE", "INVALID_ICON_KEY"):
        messages = {
            "UNSUPPORTED_FORMAT": "Use .shp o .zip con shapefile",
            "ZIP_WITHOUT_SHP": "El ZIP no contiene archivo .shp",
            "ZIP_PATH_TRAVERSAL": "El ZIP contiene rutas no permitidas",
            "SHP_NOT_FOUND": "No se encontró .shp",
            "EMPTY_FILE": "Archivo vacío",
            "INVALID_SVG": "El archivo debe ser SVG válido",
            "INVALID_SVG_SIZE": "SVG demasiado grande (máx. 512 KB)",
            "INVALID_ICON_KEY": "Clave inválida (minúsculas, números y _, empieza con letra)",
        }
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": raw, "message": messages.get(raw, raw)},
        )
    return HTTPException(status_code=400, detail={"ok": False, "error": "BAD_REQUEST", "message": raw})


def _http_from_runtime_error(exc: RuntimeError) -> HTTPException:
    raw = str(exc)
    if raw.startswith("OGR2OGR:"):
        return HTTPException(
            status_code=400,
            detail={"ok": False, "error": "SHP_IMPORT_FAILED", "message": raw.split(":", 1)[1]},
        )
    return HTTPException(status_code=500, detail={"ok": False, "error": "SERVER_ERROR", "message": raw})


def _identify_fields_to_payload(fields: List[Union[str, IdentifyFieldBody]]) -> List[Any]:
    out: List[Any] = []
    for item in fields or []:
        if isinstance(item, str):
            col = item.strip()
            if col:
                out.append(col)
        else:
            col = (item.column or "").strip()
            if col:
                label = (item.label or "").strip()
                out.append({"column": col, "label": label or col})
    return out


def _body_to_payload(body: CreateLayerBody) -> Dict[str, Any]:
    style = {k: v for k, v in body.style.model_dump().items() if v is not None}
    data = {k: v for k, v in body.data.model_dump().items() if v is not None}
    if data.get("export_columns") == []:
        data.pop("export_columns", None)
    payload: Dict[str, Any] = {
        "layer_id": body.layer_id,
        "label": body.label,
        "group_id": body.group_id,
        "geometry": body.geometry,
        "style_preset": body.style_preset,
        "style": style,
        "data": data,
        "capabilities": body.capabilities.model_dump(),
    }
    if body.identify:
        identify: Dict[str, Any] = {}
        title = (body.identify.title or "").strip()
        if title:
            identify["title"] = title
        fields = _identify_fields_to_payload(body.identify.fields)
        if fields:
            identify["fields"] = fields
        if identify:
            payload["identify"] = identify
    if body.labels and body.labels.enabled is not False and (body.labels.field or "").strip():
        payload["labels"] = {
            "field": body.labels.field.strip(),
            "minzoom": body.labels.minzoom,
            "above_icon": body.labels.above_icon,
            "color": body.labels.color,
        }
        if body.labels.offset is not None and len(body.labels.offset) >= 2:
            payload["labels"]["offset"] = [float(body.labels.offset[0]), float(body.labels.offset[1])]
        if body.labels.source:
            payload["labels"]["source"] = body.labels.source.strip()
    if body.denue and str((body.data.table or "")).lower() == "c_denue":
        payload["denue"] = body.denue.model_dump()
    if body.search and body.search.enabled:
        search = {k: v for k, v in body.search.model_dump().items() if v is not None}
        if search.get("name_column"):
            payload["search"] = search
    if body.spatial_analysis and body.capabilities.spatial_analysis:
        spatial = {k: v for k, v in body.spatial_analysis.model_dump().items() if v is not None}
        if spatial:
            payload["spatial_analysis"] = spatial
    if body.tabular and body.capabilities.tabular:
        tabular = {k: v for k, v in body.tabular.model_dump().items() if v is not None}
        if tabular:
            payload["tabular"] = tabular
    if body.overlay_key:
        payload["overlay_key"] = body.overlay_key
    if body.checkbox_id:
        payload["checkbox_id"] = body.checkbox_id
    return payload


@router.get("/meta")
def visor_admin_meta(_user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    return {"ok": True, **admin_meta()}


@router.get("/tables")
def visor_admin_tables(_user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    try:
        tables = list_publishable_tables()
    except RuntimeError as exc:
        raise _http_from_value_error(ValueError(str(exc))) from exc
    return {"ok": True, "tables": tables}


@router.get("/tables/{table_name}/columns")
def visor_admin_table_columns(table_name: str, _user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    try:
        columns = list_table_columns(table_name)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, "table": table_name, "columns": columns}


@router.get("/tables/{table_name}/columns/{column_name}/distinct")
def visor_admin_column_distinct(
    table_name: str,
    column_name: str,
    limit: int = 32,
    _user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = list_column_distinct_values(table_name, column_name, limit=limit)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result}


@router.get("/tables/{table_name}/status")
def visor_admin_table_status(table_name: str, _user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    try:
        status = table_publish_status(table_name)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **status}


@router.get("/audit")
def visor_admin_audit_log(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    layer_id: Optional[str] = None,
    table: Optional[str] = None,
    _user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = list_catalog_audit(
            limit=limit,
            offset=offset,
            action=action,
            layer_id=layer_id,
            table=table,
        )
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result}


@router.get("/tables/{table_name}/indexes")
def visor_admin_table_indexes(table_name: str, _user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    try:
        result = list_table_indexes(table_name)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result}


@router.post("/tables/{table_name}/indexes/plan")
def visor_admin_table_index_plan(
    table_name: str,
    body: CreateLayerBody,
    _user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        payload = _body_to_payload(body)
        if (payload.get("data") or {}).get("table", "").lower() != table_name.strip().lower():
            raise ValueError("VALIDATION:La tabla del cuerpo no coincide con la URL")
        result = build_index_plan(table_name, payload)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result}


@router.post("/tables/{table_name}/indexes")
def visor_admin_apply_table_indexes(
    table_name: str,
    body: ApplyIndexesBody,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    if not body.items:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "EMPTY_ITEMS", "message": "Indique al menos un índice a crear"},
        )
    try:
        items = [item.model_dump() for item in body.items]
        result = apply_table_indexes(table_name, items, only_missing=body.only_missing)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    try:
        record_indexes_audit(int(user["id"]), table_name.strip(), result)
    except (KeyError, TypeError, ValueError):
        pass
    created_n = len(result.get("created") or [])
    skipped_n = len(result.get("skipped") or [])
    err_n = len(result.get("errors") or [])
    if created_n:
        msg = f"Se crearon {created_n} índice(s)."
    elif err_n:
        msg = "No se pudo crear ningún índice."
    else:
        msg = f"No hubo cambios ({skipped_n} ya existían)."
    return {"ok": err_n == 0, **result, "message": msg}


@router.post("/upload/shp")
async def visor_admin_upload_shp(
    file: UploadFile = File(...),
    table_name: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    content = await file.read()
    if len(content) > 80 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"ok": False, "error": "FILE_TOO_LARGE", "message": "Máx. 80 MB"})
    try:
        result = import_shapefile(content, file.filename or "upload.shp", table_name)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    except RuntimeError as exc:
        raise _http_from_runtime_error(exc) from exc
    try:
        record_shp_import_audit(
            int(user["id"]),
            table=str(result.get("table") or table_name or ""),
            filename=file.filename or "upload.shp",
            feature_count=int(result.get("feature_count") or 0),
            geometry=str(result.get("geometry") or ""),
        )
    except (KeyError, TypeError, ValueError):
        pass
    return {
        "ok": True,
        **result,
        "message": "Shapefile importado. Reinicie Martin antes de publicar la capa.",
    }


@router.post("/upload/icon")
async def visor_admin_upload_icon(
    file: UploadFile = File(...),
    icon_key: str = Form(...),
    label: Optional[str] = Form(None),
    overwrite: bool = Form(False),
    _user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    content = await file.read()
    try:
        result = register_custom_icon(icon_key, label or icon_key, content, overwrite=overwrite)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail={"ok": False, "error": "ICON_SAVE_FAILED", "message": str(exc)}) from exc
    return {"ok": True, **result, "message": "Icono registrado. Recargue el visor (Ctrl+F5)."}


@router.post("/validate")
def visor_admin_validate(body: CreateLayerBody, _user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    result = validate_new_layer(_body_to_payload(body))
    return {"ok": True, **result}


@router.get("/groups")
def visor_admin_list_groups(_user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    return {"ok": True, "groups": list_catalog_groups()}


@router.post("/groups")
def visor_admin_create_group(
    body: CreateGroupBody,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = create_group_from_payload(body.model_dump(), int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {
        "ok": True,
        **result,
        "message": "Grupo creado. Ya puede asignar capas a este apartado al publicar o editar.",
    }


@router.patch("/groups/{group_id}")
def visor_admin_update_group(
    group_id: str,
    body: UpdateGroupBody,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = update_catalog_group_label(group_id, body.label, int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result, "message": "Nombre del grupo actualizado."}


@router.delete("/groups/{group_id}")
def visor_admin_delete_group(
    group_id: str,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = delete_catalog_group(group_id, int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result, "message": "Grupo eliminado del catálogo."}


@router.get("/layers")
def visor_admin_list_layers(_user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    return {"ok": True, "layers": list_managed_layers()}


@router.get("/layers/{layer_id}")
def visor_admin_get_layer(layer_id: str, _user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    try:
        detail = get_layer_admin_detail(layer_id)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **detail}


@router.put("/layers/{layer_id}")
def visor_admin_update_layer(
    layer_id: str,
    body: CreateLayerBody,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = update_layer_from_payload(layer_id, _body_to_payload(body), int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result, "message": "Capa actualizada. Recargue el visor (Ctrl+F5)."}


@router.delete("/layers/{layer_id}")
def visor_admin_delete_layer(
    layer_id: str,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = delete_managed_layer(layer_id, int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result, "message": "Capa despublicada del catálogo."}


@router.post("/layers")
def visor_admin_create_layer(
    body: CreateLayerBody,
    user: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        result = create_layer_from_payload(_body_to_payload(body), int(user["id"]))
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"ok": True, **result, "message": "Capa publicada. Recargue el visor (Ctrl+F5)."}
