"""
=============================================================================
  ATLAS — Catálogo de puntuación para ruteo (Red Nacional de Caminos)
=============================================================================

ÚNICO archivo que debe editar un desarrollador para ajustar heurísticas.

El motor de ruteo (``routing_engine/scoring.py``) lee estas tablas y genera
SQL dinámico.  No modifique factores en otros módulos.

Modelo
------
::

    costo_final = GREATEST(
        costo_base × factor_tipo × factor_velocidad × factor_peaje
                  × factor_superficie × factor_recubrimiento × factor_condicion
                  × factor_carriles × factor_administracion,
        1.0
    )

- **costo_base**: longitud en metros (modo distancia) o longitud en metros
  (modo tiempo; el tiempo se modela vía ``factor_velocidad``).
- **factor_circulacion**: no multiplica; define ``cost`` / ``reverse_cost``
  según sentido (ver ``CIRCULACION_*``).
- Valores **None** en ``FACTOR_CONDICION`` → arista EXCLUIDA del grafo (WHERE).

Columnas RNC utilizadas (si existen en la versión instalada)
-------------------------------------------------------------
tipo_vial, velocidad/velocidad_kmh, peaje, cond_pav, recubrimiento/recubri,
condicion, circulacion/circula, carriles, administra, estatus, longitud_m/longitud
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Costo base (modo tiempo)
# ---------------------------------------------------------------------------

# Velocidad de referencia (km/h).  factor_velocidad = BASELINE_KMH / vel_efectiva
# hace que una vía a 90 km/h tenga factor 1.0 en modo tiempo.
BASELINE_KMH: float = 90.0

# Velocidad mínima admisible (evita división por cero en SQL).
MIN_SPEED_KMH: float = 20.0

# ---------------------------------------------------------------------------
# factor_tipo — columna: tipo_vial
# ---------------------------------------------------------------------------

FACTOR_TIPO_DEFAULT: float = 1.25

FACTOR_TIPO_VIAL: dict[str, float] = {
    "Carretera": 1.00,
    "Autopista": 1.00,
    "Corredor": 1.00,
    "Calzada": 1.02,
    "Boulevard": 1.05,
    "Periférico": 1.05,
    "Circuito": 1.05,
    "Circunvalación": 1.05,
    "Viaducto": 1.05,
    "Avenida": 1.10,
    "Diagonal": 1.12,
    "Prolongación": 1.12,
    "Continuación": 1.12,
    "Ampliación": 1.12,
    "Calle": 1.20,
    "Privada": 1.22,
    "Cerrada": 1.22,
    "Callejón": 1.35,
    "Enlace": 1.05,
    "Glorieta": 1.08,
    "Retorno": 1.08,
    "Retorno U": 1.08,
    "Rampa de frenado": 1.10,
    "Camino": 1.50,
    "Vereda": 5.00,
    "Andador": 3.00,
    "Peatonal": 8.00,
}

VELOCIDAD_ESTIMADA_POR_TIPO: dict[str, float] = {
    "Autopista": 110.0,
    "Carretera": 90.0,
    "Corredor": 90.0,
    "Calzada": 70.0,
    "Boulevard": 60.0,
    "Periférico": 60.0,
    "Avenida": 50.0,
    "Calle": 35.0,
    "Camino": 40.0,
    "Vereda": 15.0,
    "Enlace": 50.0,
    "Glorieta": 40.0,
    "Peatonal": 5.0,
}

VELOCIDAD_ESTIMADA_DEFAULT: float = 45.0

# ---------------------------------------------------------------------------
# factor_peaje — columna: peaje
# ---------------------------------------------------------------------------

FACTOR_PEAJE: dict[str, float] = {
    "Si": 1.40,
    "Sí": 1.40,
    "SI": 1.40,
    "SÍ": 1.40,
    "No": 1.00,
    "NO": 1.00,
}

FACTOR_PEAJE_DEFAULT: float = 1.00
FACTOR_PEAJE_EVITAR: float = 50.0
FACTOR_PEAJE_AUTUPISTA_IMPLICITO: float = 1.40

# ---------------------------------------------------------------------------
# factor_superficie — columna: cond_pav
# ---------------------------------------------------------------------------

FACTOR_SUPERFICIE: dict[str, float] = {
    "Con pavimento": 1.00,
    "Sin pavimento": 1.80,
    "N/A": 1.05,
    "N/D": 1.05,
}

FACTOR_SUPERFICIE_DEFAULT: float = 1.10
TERRACERIA_EVITAR_MULTIPLIER: float = 8.0

# ---------------------------------------------------------------------------
# factor_recubrimiento — columna: recubrimiento / recubri
# ---------------------------------------------------------------------------

FACTOR_RECUBRIMIENTO: dict[str, float] = {
    "Asfalto": 1.00,
    "Concreto": 1.00,
    "Bloques": 1.10,
    "Grava": 1.50,
    "Tierra": 2.50,
    "N/A": 1.05,
    "N/D": 1.05,
}

FACTOR_RECUBRIMIENTO_DEFAULT: float = 1.15
RECUBRIMIENTO_EVITAR_TIERRA: float = 12.0
RECUBRIMIENTO_EVITAR_GRAVA: float = 6.0

# ---------------------------------------------------------------------------
# factor_condicion — columna: condicion
# ---------------------------------------------------------------------------

FACTOR_CONDICION: dict[str, float | None] = {
    "En operación": 1.00,
    "En construcción abierto": 4.00,
    "En construcción": 4.00,
    "En construcción - abierto": 4.00,
    "En construcción cerrado": None,
    "En construcción - cerrado": None,
    "Planeado": None,
    "Deshabilitado": None,
}

FACTOR_CONDICION_DEFAULT: float = 1.00
EXCLUIR_ESTATUS: tuple[str, ...] = ("Deshabilitado",)

# ---------------------------------------------------------------------------
# factor_carriles — columna: carriles
# ---------------------------------------------------------------------------

FACTOR_CARRILES: dict[str, float] = {
    "1": 1.10,
    "2": 1.00,
    "3": 0.98,
    "4": 0.96,
    "5": 0.95,
    "6": 0.94,
    "7": 0.93,
    "9": 0.92,
    "N/A": 1.00,
    "N/D": 1.00,
}

FACTOR_CARRILES_DEFAULT: float = 1.00

# ---------------------------------------------------------------------------
# factor_administracion — columna: administra
# ---------------------------------------------------------------------------

FACTOR_ADMINISTRACION: dict[str, float] = {
    "Federal": 1.00,
    "Estatal": 1.04,
    "Municipal": 1.12,
    "Particular": 1.15,
    "Otro": 1.10,
    "N/D": 1.08,
    "N/A": 1.08,
}

FACTOR_ADMINISTRACION_DEFAULT: float = 1.10
FACTOR_ADMIN_EVITAR_FEDERAL: float = 1.50
FACTOR_ADMIN_EVITAR_ESTATAL: float = 1.30

# ---------------------------------------------------------------------------
# factor_circulacion — columna: circulacion / circula
# ---------------------------------------------------------------------------

CIRCULACION_DOS_SENTIDOS: str = "Dos sentidos"
CIRCULACION_UN_SENTIDO: str = "Un sentido"
CIRCULACION_CERRADA: str = "Cerrada en ambos sentidos"
COSTO_IMPASABLE: float = -1.0
COSTO_MINIMO: float = 1.0
