"""Nombres de tablas PostGIS en esquema atlas (renombres 2026)."""

from config import get_settings

# Esquema PostGIS dentro de la base (ATLAS_SCHEMA en .env). No confundir con DB_NAME.
SCHEMA = get_settings()["schema"]

# Marco municipal y contexto
T_MUN = "c_mun"
T_CONTEXTO = "c_contexto"
T_MUN_CONTEOS = "municipio_conteos"
T_DENUE = "c_denue"

# Capas cartográficas (antes 12*)
T_LOC_PUNTO = "c_loc_punto"
T_COL_ASE = "c_col_ase"
T_RNC = "c_rnc"
T_RNC_ROUTING = "c_rnc_routing"
T_RNC_LOC = "c_rnc_loc"
T_RNC_VERTICES = "c_rnc_vertices_pgr"
T_AGUA_SANEA = "c_agua_sanea"
T_A = "c_a"
T_AR = "c_ar"
T_E = "c_e"
T_L = "c_l"
T_M = "c_m"
T_ENT = "c_ent"
T_RESIDUO = "c_residuo_solido"
T_CLUES = "c_clues"
T_HCORRIENTES = "hcorrientes"
T_HCUERPOS = "hcuerpos"
T_CURNIVEL = "curnivel"

# Tablas de indicadores (sin cambio de nombre)
T_TAB_MUNICIPAL = "tab_municipal"
T_TAB_NACIONAL = "tab_nacional"
T_C_INV = "c_inv"
T_ITER = "iter"
T_USO_SUELO = "usosuelo"


def qualified(table: str) -> str:
    """Identificador atlas.tabla entre comillas."""
    safe = table.replace('"', '""')
    return f'{SCHEMA}."{safe}"'
