"""
Exportación CSV de aristas candidatas para unir componentes del corredor.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from ruteo.routing_engine.diagnostics.edge_attributes import BREAK_EDGE_FIELDS

CSV_COLUMNS = [f for f in BREAK_EDGE_FIELDS if f != "kind"]


def write_candidate_join_edges_csv(
    path: str | Path,
    rows: List[Dict[str, Any]],
) -> Path:
    """Escribe ``candidate_join_edges.csv`` con atributos de vialidad."""
    out = Path(path)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    return out.resolve()
