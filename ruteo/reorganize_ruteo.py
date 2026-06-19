#!/usr/bin/env python3
"""Una vez: mueve motor, scripts y docs al paquete ruteo/. Ejecutar desde app_api/."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT.parent

MOVES = [
    (APP / "routing_engine", ROOT / "routing_engine"),
    (APP / "ruteo.py", ROOT / "facade.py"),
    (APP / "ruteo_corredores.py", ROOT / "legacy" / "ruteo_corredores.py"),
    (APP / "scripts" / "test_peaje_ruteo.py", ROOT / "scripts" / "test_peaje_ruteo.py"),
    (APP / "scripts" / "diagnose_corridor_subgraph.py", ROOT / "scripts" / "diagnose_corridor_subgraph.py"),
]

DOCS = list((APP / "docs").glob("RUTEA*.md")) if (APP / "docs").is_dir() else []
SQL = list((APP / "docs" / "sql").glob("c_rnc_routing*.sql")) if (APP / "docs" / "sql").is_dir() else []
OUTPUTS = [
    APP / "corridor_subgraph_diag.geojson",
    APP / "corridor_subgraph_diag.json",
    APP / "candidate_join_edges.csv",
]


def main() -> None:
    for sub in ("scripts", "docs", "docs/sql", "legacy", "output"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)

    for src, dst in MOVES:
        if not src.exists():
            continue
        if dst.exists():
            # Puente temporal: solo __init__.py en ruteo/routing_engine
            if src.name == "routing_engine" and dst.is_dir():
                bridge = dst / "__init__.py"
                others = [p for p in dst.iterdir() if p.name != "__init__.py"]
                if bridge.exists() and not others:
                    bridge.unlink()
                    print("removed bridge __init__.py")
                elif others:
                    print(f"skip (exists): {dst}")
                    continue
            else:
                print(f"skip (exists): {dst}")
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"moved: {src.name} -> {dst.relative_to(APP)}")

    # facade ya en ruteo/
    facade = ROOT / "facade.py"
    if facade.exists():
        _patch_facade_imports(facade)

    for src in DOCS:
        dst = ROOT / "docs" / src.name
        if dst.exists():
            continue
        if not src.exists():
            continue
        shutil.copy2(str(src), str(dst))
        print(f"copied doc: {src.name}")

    for src in SQL:
        dst = ROOT / "docs" / "sql" / src.name
        if dst.exists():
            continue
        shutil.move(str(src), str(dst))
        print(f"moved sql: {src.name}")

    for src in OUTPUTS:
        if not src.exists():
            continue
        dst = ROOT / "output" / src.name
        if dst.exists():
            continue
        shutil.move(str(src), str(dst))
        print(f"moved output: {src.name}")

    print("done.")
    _patch_imports(ROOT / "routing_engine")


def _patch_facade_imports(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    new = text.replace("from routing_engine.", "from ruteo.routing_engine.")
    if new != text:
        path.write_text(new, encoding="utf-8")
        print("patched facade imports")


def _patch_imports(tree: Path) -> None:
    """Actualiza imports internos tras mover routing_engine."""
    if not tree.is_dir():
        return
    for py in tree.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        new = text.replace("from routing_engine.", "from ruteo.routing_engine.")
        new = new.replace("import routing_engine.", "import ruteo.routing_engine.")
        if new != text:
            py.write_text(new, encoding="utf-8")
            print(f"patched imports: {py.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
