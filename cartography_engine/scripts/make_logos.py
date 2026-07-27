"""Utilidad opcional: genera PNG tipográficos mínimos si faltan logos.

Los nombres salen de assets/branding.json (no hardcodear marcas).
Preferible colocar PNG oficiales (fondo transparente) en assets/logos/.

Uso: python -m cartography_engine.scripts.make_logos
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from cartography_engine.branding import clear_branding_cache, get_branding

ROOT = Path(__file__).resolve().parents[1] / "assets" / "logos"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rgba_rows)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _chunk(b"IDAT", zlib.compress(raw, 9)),
            _chunk(b"IEND", b""),
        ]
    )


def _blank(w: int, h: int, fill=(255, 255, 255, 0)) -> list[list[list[int]]]:
    r, g, b, a = fill
    return [[[r, g, b, a] for _ in range(w)] for _ in range(h)]


def _put(pix, x: int, y: int, rgba) -> None:
    h = len(pix)
    w = len(pix[0])
    if 0 <= x < w and 0 <= y < h:
        pix[y][x] = list(rgba)


def _draw_word(pix, w: int, text: str, cx: int, cy: int, color, scale: int = 2) -> None:
    # Glifo mínimo 3×5 (solo letras A–Z y espacio) para placeholders.
    glyphs = {
        "A": ["010", "101", "111", "101", "101"],
        "C": ["011", "100", "100", "100", "011"],
        "D": ["110", "101", "101", "101", "110"],
        "E": ["111", "100", "110", "100", "111"],
        "G": ["011", "100", "101", "101", "011"],
        "I": ["111", "010", "010", "010", "111"],
        "L": ["100", "100", "100", "100", "111"],
        "N": ["101", "111", "111", "101", "101"],
        "P": ["110", "101", "110", "100", "100"],
        "R": ["110", "101", "110", "101", "101"],
        "S": ["011", "100", "010", "001", "110"],
        " ": ["000", "000", "000", "000", "000"],
    }
    x = cx - (len(text) * 4 * scale) // 2
    for ch in text.upper():
        rows = glyphs.get(ch, glyphs[" "])
        for dy, row in enumerate(rows):
            for dx, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            _put(pix, x + dx * scale + sx, cy + dy * scale + sy, color)
        x += 4 * scale


def make_placeholder(path: Path, label: str, accent=(109, 27, 42, 255)) -> None:
    w, h = 220, 90
    pix = _blank(w, h, (255, 255, 255, 0))
    _draw_word(pix, w, label[:10], w // 2, 28, accent, scale=3)
    rows = [bytes(c for px in row for c in px) for row in pix]
    path.write_bytes(_png(w, h, rows))


def main() -> None:
    clear_branding_cache()
    branding = get_branding()
    ROOT.mkdir(parents=True, exist_ok=True)
    logos = list(branding.get("logos") or [])
    labels = list(branding.get("fallback_labels") or [])
    for i, name in enumerate(logos):
        path = ROOT / name
        if path.is_file():
            print(f"SKIP {path} (ya existe)")
            continue
        label = labels[i] if i < len(labels) else Path(name).stem.upper()
        make_placeholder(path, label)
        print(f"OK {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
