"""
Verificación rápida del Cartography Engine (local o dentro del contenedor).

Uso:
  cd app_api
  python -m cartography_engine.scripts.verify_cartography
  python -m cartography_engine.scripts.verify_cartography --cve-mun 001 --geopdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verifica GroSIG Cartography Engine")
    parser.add_argument("--cve-mun", default="", help="Si se indica, genera croquis municipal")
    parser.add_argument(
        "--atlas-list",
        default="",
        help="Lista CVE separada por comas para atlas multipágina (p. ej. 001,029,035)",
    )
    parser.add_argument(
        "--geopdf",
        action="store_true",
        help="Además genera croquis/atlas en format=geopdf",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "tests"),
        help="Directorio de salida para PDFs",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from cartography_engine.layouts import build_layout
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map, health_payload

    layout = build_layout()
    assert layout.map_frame.width > 0
    print("layout: OK")

    health = health_payload()
    print("health:", health)
    assert "multi_page_atlas" in health.get("capabilities", [])
    assert "atlas_municipal" in health.get("templates", [])
    assert "plu_multipage" in health.get("capabilities", [])
    assert "geopdf" in health.get("capabilities", [])
    assert "geopdf" in health.get("formats", [])
    assert str(health.get("version", "")).startswith("1.4")

    pdf, name, _ = generate_map(GenerateMapRequest(template_id="demo_blank"))
    demo_path = out_dir / name
    demo_path.write_bytes(pdf)
    assert pdf.startswith(b"%PDF")
    print(f"demo: OK ({demo_path}, {len(pdf)} bytes)")

    svg, svg_name, _ = generate_map(GenerateMapRequest(template_id="demo_blank", format="svg"))
    svg_path = out_dir / svg_name
    svg_path.write_bytes(svg)
    assert b"<svg" in svg
    print(f"demo svg: OK ({svg_path}, {len(svg)} bytes)")

    if args.cve_mun:
        pdf2, name2, _ = generate_map(
            GenerateMapRequest(
                template_id="croquis_municipal",
                params={"cve_mun": args.cve_mun},
            )
        )
        croquis_path = out_dir / name2
        croquis_path.write_bytes(pdf2)
        assert pdf2.startswith(b"%PDF")
        print(f"croquis: OK ({croquis_path}, {len(pdf2)} bytes)")

        svg2, name_svg2, _ = generate_map(
            GenerateMapRequest(
                template_id="croquis_municipal",
                params={"cve_mun": args.cve_mun},
                format="svg",
            )
        )
        croquis_svg = out_dir / name_svg2
        croquis_svg.write_bytes(svg2)
        assert b"<svg" in svg2
        print(f"croquis svg: OK ({croquis_svg}, {len(svg2)} bytes)")

        if args.geopdf:
            geo, geo_name, _ = generate_map(
                GenerateMapRequest(
                    template_id="croquis_municipal",
                    params={"cve_mun": args.cve_mun},
                    format="geopdf",
                )
            )
            geo_path = out_dir / geo_name
            geo_path.write_bytes(geo)
            assert geo.startswith(b"%PDF")
            assert "_geo" in geo_name
            print(f"geopdf: OK ({geo_path}, {len(geo)} bytes)")
    else:
        print("croquis: skipped (pasa --cve-mun)")

    if args.atlas_list:
        cves = [p.strip() for p in args.atlas_list.replace(";", ",").split(",") if p.strip()]
        atlas_pdf, atlas_name, _ = generate_map(
            GenerateMapRequest(
                template_id="atlas_municipal",
                format="pdf",
                params={"cve_mun_list": cves, "cover": True},
            )
        )
        atlas_path = out_dir / atlas_name
        atlas_path.write_bytes(atlas_pdf)
        assert atlas_pdf.startswith(b"%PDF")
        print(f"atlas: OK ({atlas_path}, {len(atlas_pdf)} bytes, {len(cves)} municipios)")
        if args.geopdf:
            atlas_geo, atlas_geo_name, _ = generate_map(
                GenerateMapRequest(
                    template_id="atlas_municipal",
                    format="geopdf",
                    params={"cve_mun_list": cves, "cover": True},
                )
            )
            p = out_dir / atlas_geo_name
            p.write_bytes(atlas_geo)
            assert atlas_geo.startswith(b"%PDF")
            print(f"atlas geopdf: OK ({p}, {len(atlas_geo)} bytes)")
    else:
        print("atlas: skipped (pasa --atlas-list)")

    print("VERIFY_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"VERIFY_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
