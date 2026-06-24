"""Catálogo de capas exportables del visor (tablas renombradas)."""

from typing import Any, Dict, Optional, Sequence

from tables import (
    T_A,
    T_AGUA_SANEA,
    T_AR,
    T_CLUES,
    T_COL_ASE,
    T_CURNIVEL,
    T_DENUE,
    T_E,
    T_HCORRIENTES,
    T_HCUERPOS,
    T_L,
    T_LOC_PUNTO,
    T_M,
    T_RNC,
    T_RESIDUO,
    T_USO_SUELO,
    qualified,
)

DENUE_KML_EXPORT_COLUMNS = [
    "gid",
    "cve_mun",
    "municipio",
    "codigo_act",
    "nom_estab",
    "nombre_act",
    "localidad",
]

DENUE_ESCUELAS_CODIGOS = (
    611112,
    611122,
    611132,
    611142,
    611152,
    611162,
    611172,
    611182,
    611212,
    611312,
    611422,
    611432,
    611512,
    611612,
    611622,
    611632,
)


def _codigo_act_predicate(codigos: Sequence[int]) -> str:
    codes = ", ".join(f"'{int(c)}'" for c in codigos)
    return f"regexp_replace(TRIM(codigo_act::text), '[^0-9]', '', 'g') IN ({codes})"


def _denue_from_sql(codigos: Sequence[int]) -> str:
    where_codes = _codigo_act_predicate(codigos)
    return f"""(
        SELECT *
          FROM {qualified(T_DENUE)}
         WHERE {where_codes}
    ) AS src"""


def _denue_layer_cfg(label: str, codigos: Sequence[int]) -> Dict[str, Any]:
    return {
        "label": label,
        "from_sql": _denue_from_sql(codigos),
        "gid_table": T_DENUE,
        "geom_type": "point",
        "mun_filter_cvegeo": False,
        "export_columns_kml": DENUE_KML_EXPORT_COLUMNS,
        "shp_all_table_columns": True,
    }


def layer_catalog() -> Dict[str, Dict[str, Any]]:
    return {
        "locspunto": {
            "label": "Localidades",
            "table": T_LOC_PUNTO,
            "geom_type": "point",
            "export_columns": [
                "gid", "cve_mun", "cvegeo", "nom_loc", "nom_mun", "ambito",
                "latitud", "longitud", "altitud", "pob_total", "pob_femeni", "pob_mascul",
            ],
        },
        "locsatlas": {"label": "Localidades con amanzanamiento", "table": T_L, "geom_type": "polygon"},
        "colonias": {"label": "Colonias", "table": T_COL_ASE, "geom_type": "polygon"},
        "ageb_urbanas": {"label": "AGEBS Urbanas", "table": T_A, "geom_type": "polygon"},
        "ageb_rurales": {"label": "AGEBS Rurales", "table": T_AR, "geom_type": "polygon"},
        "manzanas": {"label": "Manzanas", "table": T_M, "geom_type": "polygon"},
        "vialidades": {
            "label": "Vialidades",
            "table": T_E,
            "geom_type": "line",
            "export_columns": [
                "gid", "cve_mun", "cvegeo", "nomvial", "nomvial1", "tipo", "orden",
                "ambito", "carretera", "longitud", "velocidad", "condicion",
            ],
        },
        "rnc": {
            "label": "Red Nacional de Caminos",
            "from_sql": f"""(
                SELECT gid, cve_mun, tipo_vial, NULL::text AS cvegeo,
                       ST_Simplify(the_geom, 8.0) AS the_geom
                  FROM atlas."{T_RNC}"
            ) AS src""",
            "geom_type": "line",
        },
        "saneamiento_agua": {
            "label": "Servicios de Agua y Saneamiento",
            "table": T_AGUA_SANEA,
            "geom_type": "point",
        },
        "clues": {
            "label": "Establecimientos de salud",
            "table": T_CLUES,
            "geom_type": "point",
            "export_columns": [
                "gid",
                "cve_mun",
                "clues",
                "nom_insti",
                "nom_comer",
                "nom_insadm",
            ],
        },
        "residuo_solido": {
            "label": "Residuos solidos urbanos",
            "table": T_RESIDUO,
            "geom_type": "point",
            "export_columns": [
                "gid", "cve_mun", "cvegeo", "tipo", "nom_tipo",
            ],
        },
        "uso_suelo": {
            "label": "Uso de suelo",
            "table": T_USO_SUELO,
            "geom_type": "polygon",
            "mun_filter_cvegeo": False,
            "export_columns": [
                "gid", "cve_mun", "descripcio", "descrip_1", "descrip_2", "area", "perimetro",
            ],
        },
        "hidro_corrientes": {
            "label": "Hidrografía (corrientes de agua)",
            "table": T_HCORRIENTES,
            "geom_type": "line",
            "export_columns": ["gid", "cve_mun", "nombre", "condicion", "tipo", "longitud"],
        },
        "hidro_cuerpos": {
            "label": "Hidrografía (cuerpos de agua)",
            "table": T_HCUERPOS,
            "geom_type": "polygon",
            "export_columns": ["gid", "cve_mun", "nombre", "condicion", "tipo", "area"],
        },
        "curvas_nivel": {
            "label": "Curvas de nivel",
            "table": T_CURNIVEL,
            "geom_type": "line",
            "export_columns": ["gid", "cve_mun", "elev", "tipo"],
        },
        "denue_rastros": _denue_layer_cfg("Rastros", (311611,)),
        "denue_gasolinerias": _denue_layer_cfg("Gasolinerías", (468411,)),
        "denue_gaseras": _denue_layer_cfg("Gaseras", (468412,)),
        "denue_escuelas": _denue_layer_cfg("Escuelas", DENUE_ESCUELAS_CODIGOS),
        "denue_hospitales": _denue_layer_cfg("Hospitales (DENUE)", (622112,)),
        "denue_museos": _denue_layer_cfg("Museos", (712112,)),
        "denue_cementerios": _denue_layer_cfg("Cementerios", (812322,)),
        "denue_iglesias": _denue_layer_cfg("Iglesias/Templos", (813210,)),
    }


def layer_config(layer_id: str) -> Optional[Dict[str, Any]]:
    key = (layer_id or "").strip().lower()
    if not key:
        return None
    return layer_catalog().get(key)


_DENUE_LAYER_CODIGOS: Dict[str, Sequence[int]] = {
    "denue_rastros": (311611,),
    "denue_gasolinerias": (468411,),
    "denue_gaseras": (468412,),
    "denue_escuelas": DENUE_ESCUELAS_CODIGOS,
    "denue_hospitales": (622112,),
    "denue_museos": (712112,),
    "denue_cementerios": (812322,),
    "denue_iglesias": (813210,),
}


def denue_codigos_for_layer(layer_id: str) -> Optional[Sequence[int]]:
    """Códigos SCIAN (codigo_act) asociados a una capa DENUE del visor."""
    key = (layer_id or "").strip().lower()
    codes = _DENUE_LAYER_CODIGOS.get(key)
    return codes if codes else None
