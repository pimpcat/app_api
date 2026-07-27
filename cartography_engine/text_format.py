"""Formato de texto para etiquetas cartográficas."""

from __future__ import annotations

# Conectores / artículos que van en minúscula en nombres propios (no al inicio).
_SMALL_WORDS = frozenset(
    {
        "DE",
        "DEL",
        "LA",
        "LAS",
        "LOS",
        "EL",
        "Y",
        "E",
        "O",
        "U",
        "AL",
        "A",
        "EN",
        "POR",
        "CON",
        "PARA",
    }
)


def to_proper_name(text: str) -> str:
    """Convierte a altas/bajas de nombre propio: ``LA HAMACA`` → ``La Hamaca``.

    Conserva códigos numéricos y tokens muy cortos en mayúsculas si parecen CVE.
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    # Multilínea: formatear cada línea
    if "\n" in raw:
        return "\n".join(to_proper_name(part) for part in raw.split("\n"))

    parts = raw.replace("_", " ").split()
    out: list[str] = []
    for i, word in enumerate(parts):
        w = word.strip()
        if not w:
            continue
        # CVE / números puros
        if w.isdigit() or (len(w) <= 4 and w.upper() == w and any(ch.isdigit() for ch in w)):
            out.append(w)
            continue
        if "-" in w:
            out.append("-".join(to_proper_name(p) if p else p for p in w.split("-")))
            continue
        up = "".join(ch for ch in w.upper() if ch.isalpha())
        if i > 0 and up in _SMALL_WORDS:
            out.append(up.lower())
            continue
        # Capitalizar primera letra; resto minúsculas (salvo acentos ya en el token)
        letters = list(w)
        # Si viene TODO MAYÚSCULAS o todo minúsculas, normalizar
        alpha = [ch for ch in w if ch.isalpha()]
        if alpha and (
            all(ch.isupper() for ch in alpha) or all(ch.islower() for ch in alpha)
        ):
            lower = w.lower()
            # Primera letra alfabética en mayúscula
            chars = list(lower)
            for j, ch in enumerate(chars):
                if ch.isalpha():
                    chars[j] = ch.upper()
                    break
            out.append("".join(chars))
        else:
            out.append(w)
    return " ".join(out)


def wrap_name_lines(name: str, *, max_lines: int = 3) -> list[str]:
    """Parte un nombre en hasta ``max_lines`` renglones de longitud similar.

    Pensado para etiquetas de localidad de área (bloque compacto y proporcional):
    nombres cortos → 1 línea; medios → 2; largos → hasta 3.
    """
    words = str(name or "").strip().split()
    if not words:
        return []
    if len(words) == 1:
        return [words[0]]

    ends: list[int] = []
    for i, w in enumerate(words):
        ends.append(len(w) if i == 0 else ends[-1] + 1 + len(w))
    total = ends[-1]

    if total <= 14:
        n_lines = 1
    elif total <= 28 and len(words) <= 4:
        # Nombres tipo «Chilpancingo de los Bravo» → 2 renglones equilibrados.
        n_lines = min(2, len(words))
    else:
        n_lines = min(3, len(words), max_lines)

    if n_lines <= 1:
        return [" ".join(words)]

    break_after: list[int] = []
    min_after = 0
    for k in range(1, n_lines):
        target = total * k / n_lines
        # Dejar al menos una palabra por cada renglón restante.
        max_j = len(words) - (n_lines - k) - 1
        best_j = min_after
        best_d = float("inf")
        for j in range(min_after, max_j + 1):
            d = abs(ends[j] - target)
            if d < best_d:
                best_d = d
                best_j = j
        break_after.append(best_j)
        min_after = best_j + 1

    lines: list[str] = []
    start = 0
    for b in break_after:
        lines.append(" ".join(words[start : b + 1]))
        start = b + 1
    lines.append(" ".join(words[start:]))
    return lines


def format_localidad_area_label(cve: str, name: str) -> str:
    """Etiqueta de localidad de área: clave + nombre en hasta 3 renglones.

    Ejemplo::

        0001
        Chilpancingo
        de los Bravo
    """
    code = str(cve or "").strip()
    name_lines = wrap_name_lines(str(name or "").strip(), max_lines=3)
    parts = ([code] if code else []) + name_lines
    return "\n".join(p for p in parts if p)
