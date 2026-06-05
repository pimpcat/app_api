"""Catálogo de capas exportables del visor (tablas renombradas)."""

from typing import Any, Dict, Optional

from tables import (
    T_A,
    T_AGUA_SANEA,
    T_AR,
    T_COL_ASE,
    T_E,
    T_L,
    T_LOC_PUNTO,
    T_M,
    T_RNC,
    T_USO_SUELO,
)


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
        "uso_suelo": {
            "label": "Uso de suelo",
            "table": T_USO_SUELO,
            "geom_type": "polygon",
            "mun_filter_cvegeo": False,
            "export_columns": [
                "gid", "cve_mun", "descripcio", "descrip_1", "descrip_2", "area", "perimetro",
            ],
        },
    }


def layer_config(layer_id: str) -> Optional[Dict[str, Any]]:
    return layer_catalog().get((layer_id or "").strip())
