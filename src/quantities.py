"""
Build-time parsing of Spanish ingredient lines and serving strings
into exact fractions, so the client can scale recipes (servings x pantry)
without float drift. The original strings stay the source of truth for
display at factor 1; this module only annotates.

Line shapes in data/recipes.yaml:
  "1 taza dal moong partido amarillo"      unit   (amount + unit word + rest)
  "1/2 cdta comino molido"                 unit   (ASCII fraction)
  "1.5 tazas agua caliente"                unit   (decimal, '.' or ',')
  "300 g pechuga de pollo sin hueso"       unit   (grams, invariant plural)
  "4 dientes de ajo finamente picados"     unit   (diente/dientes pair)
  "2 tomates medianos triturados"          count  (bare countable noun)
  "8–10 hojas de curry"                    count  (en-dash range, both ends scaled)
  "Zumo de 1/2 lima"                       mid    (embedded amount after prose)
  "Sal al gusto" / "Aceite para cocinar"   prose  (unscaled, shown verbatim)
"""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction

# Amount: ASCII fraction first (else "1" would eat "1/2"), then decimal, then int.
_AMT = r"(\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)"
_RANGE = rf"{_AMT}\s*[–—-]\s*{_AMT}"
_LEAD = re.compile(rf"^{_AMT}(?:\s*[–—-]\s*{_AMT})?\s+(\S+)\s*(.*)$")
_MID = re.compile(rf"^(.*?\s+){_AMT}\s+(\S+)(?:\s+(.*))?$")

# Unit vocabulary as it appears in recipes.yaml (abbreviations included).
_UNIT_RE = re.compile(r"cdtas?|cdas?|tazas?|kg|g|ml|l|dientes(?:\s+de)?")

# (display singular, display plural) pairs. Bare countable nouns that appear
# after an amount without a unit word; generate.py validate fails when data
# uses a countable first word missing from this map.
_NOUN_PAIRS = [
    ("porción", "porciones"),
    ("chilla", "chillas"),
    ("dosa", "dosas"),
    ("idli", "idlis"),
    ("paratha", "parathas"),
    ("cebolla", "cebollas"),
    ("tomate", "tomates"),
    ("chile", "chiles"),
    ("zanahoria", "zanahorias"),
    ("pimiento", "pimientos"),
    ("hoja", "hojas"),
    ("clavo", "clavos"),
    ("dátil", "dátiles"),
    ("almendra", "almendras"),
    ("rama", "ramas"),
    ("vaina", "vainas"),
    ("lima", "limas"),
    ("limón", "limones"),
    ("diente", "dientes"),
    ("pizca", "pizcas"),
]

# Bidirectional: singular <-> plural, accent-accurate display forms.
NOUN_PLURAL: dict[str, str] = {}
for _s, _p in _NOUN_PAIRS:
    NOUN_PLURAL[_s] = _p
    NOUN_PLURAL[_p] = _s

# Accent-stripped shadow for tolerant lookup (keys/values of NOUN_PLURAL
# normalized); exact match is always tried first.
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

_LOOKUP: dict[str, str] = {_strip_accents(k): v for k, v in NOUN_PLURAL.items()}

# Unit (singular, plural) display pairs. g/kg/ml/l are invariant.
_UNIT_FORMS = {
    "cdta": ("cdta", "cdtas"),
    "cda": ("cda", "cdas"),
    "taza": ("taza", "tazas"),
    "kg": ("kg", "kg"),
    "g": ("g", "g"),
    "ml": ("ml", "ml"),
    "l": ("l", "l"),
    "dientes": ("diente", "dientes"),
}


# Canonical (singular) key for any surface unit word, incl. plurals.
_UNIT_CANON = {"cdta": "cdta", "cdtas": "cdta", "cda": "cda", "cdas": "cda",
               "taza": "taza", "tazas": "taza", "kg": "kg", "g": "g",
               "ml": "ml", "l": "l", "dientes": "dientes"}


def _noun_other(word: str) -> str | None:
    """The other grammatical number of a noun word, or None if unknown."""
    if word in NOUN_PLURAL:
        return NOUN_PLURAL[word]
    return _LOOKUP.get(_strip_accents(word))


def _frac(text: str) -> tuple[int, int]:
    f = Fraction(text.strip().replace(",", "."))
    return (f.numerator, f.denominator)


def _swap_first_word(rest: str) -> tuple[str, str] | None:
    """(singular-led, plural-led) rest with only the first word swapped."""
    parts = rest.split(None, 1)
    if not parts:
        return None
    first = parts[0]
    tail = f" {parts[1]}" if len(parts) > 1 else ""
    other = _noun_other(first)
    if other is None:
        return None
    # Work out which of the two forms is the plural.
    plural = first if NOUN_PLURAL.get(first) == other and first.endswith("s") else other
    singular = other if plural == first else first
    return (singular + tail, plural + tail)


def parse_servings(text: str) -> tuple[int, str]:
    """'2 porciones (4 chillas)' -> (2, 'porciones'). Defaults to (2, 'porciones')."""
    m = re.match(r"^(\d+)\s+(\S+)", text or "")
    if not m:
        return (2, "porciones")
    n = int(m.group(1))
    other = _noun_other(m.group(2))
    unit = m.group(2) if other is None else (other if m.group(2).endswith("s") or not other.endswith("s") else other)
    # Normalize to the plural display form when known.
    if other is not None:
        unit = m.group(2) if m.group(2).endswith("s") else other
    return (n, unit)


def parse_ingredient(line: str) -> dict:
    """Classify one ingredient line; see module docstring for shapes.

    Returns dict with kind in {unit, count, mid, prose} plus
    q/q2 (tuple|None), u1/un, r1/rn, pre, noun_mapped (count/mid only).
    """
    m = _LEAD.match(line)
    if m:
        a1, a2, word, rest = m.groups()
        q = _frac(a1)
        q2 = _frac(a2) if a2 else None
        um = _UNIT_RE.fullmatch(word)
        if um:
            canon = _UNIT_CANON[word.split()[0]]
            u1, un = _UNIT_FORMS[canon]
            if canon == "dientes" and rest.lower().startswith("de "):
                rest = rest[3:]  # client rejoins with " de "
            return {"kind": "unit", "q": q, "q2": q2, "u1": u1, "un": un,
                    "r1": rest, "rn": rest, "pre": "", "noun_mapped": True}
        if re.match(r"[^\W\d_]", word, re.UNICODE):
            swapped = _swap_first_word(f"{word}{' ' + rest if rest else ''}")
            if swapped:
                return {"kind": "count", "q": q, "q2": q2, "u1": "", "un": "",
                        "r1": swapped[0], "rn": swapped[1], "pre": "",
                        "noun_mapped": True}
            return {"kind": "count", "q": q, "q2": q2, "u1": "", "un": "",
                    "r1": f"{word}{' ' + rest if rest else ''}",
                    "rn": f"{word}{' ' + rest if rest else ''}", "pre": "",
                    "noun_mapped": False}
    m = _MID.match(line)
    if m:
        pre, a1, word, tail = m.groups()
        q = _frac(a1)
        tail = tail or ""
        um = _UNIT_RE.fullmatch(word)
        if um:
            canon = _UNIT_CANON[word.split()[0]]
            u1, un = _UNIT_FORMS[canon]
            if canon == "dientes" and tail.lower().startswith("de "):
                tail = tail[3:]
            return {"kind": "mid", "q": q, "q2": None, "u1": u1, "un": un,
                    "r1": tail, "rn": tail, "pre": pre, "noun_mapped": True}
        swapped = _swap_first_word(f"{word}{' ' + tail if tail else ''}")
        if swapped:
            return {"kind": "mid", "q": q, "q2": None, "u1": "", "un": "",
                    "r1": swapped[0], "rn": swapped[1], "pre": pre,
                    "noun_mapped": True}
        return {"kind": "mid", "q": q, "q2": None, "u1": "", "un": "",
                "r1": f"{word}{' ' + tail if tail else ''}",
                "rn": f"{word}{' ' + tail if tail else ''}", "pre": pre,
                "noun_mapped": False}
    return {"kind": "prose", "q": None, "q2": None, "u1": None, "un": None,
            "r1": line, "rn": line, "pre": "", "noun_mapped": True}


def structure_ingredients(recipe) -> list[dict]:
    """JS-ready per-line dicts, index-aligned with recipe.ingredients.

    q of null marks an unscaled line; q/q2 are [num, den] lists for exact
    fraction math client-side.
    """
    out = []
    for line in recipe.ingredients:
        d = parse_ingredient(line)
        out.append({
            "q": list(d["q"]) if d["q"] is not None else None,
            "q2": list(d["q2"]) if d["q2"] is not None else None,
            "u1": d["u1"], "un": d["un"], "r1": d["r1"], "rn": d["rn"],
            "pre": d["pre"],
        })
    return out


def coverage(recipes) -> dict:
    """Parse census across all recipes for `generate.py validate`.

    scalable/total below 0.80, any unmapped countable noun, or any
    sBase < 1 makes validate exit 1 (same contract as macro warnings).
    """
    kinds: dict[str, int] = {}
    unmapped: list[tuple[str, str]] = []
    bad_servings: list[str] = []
    total = scalable = 0
    for r in recipes:
        if parse_servings(r.servings)[0] < 1:
            bad_servings.append(r.id)
        for line in r.ingredients:
            d = parse_ingredient(line)
            kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
            total += 1
            if d["kind"] != "prose":
                scalable += 1
            if d.get("noun_mapped") is False:
                unmapped.append((r.id, line))
    return {"total": total, "scalable": scalable, "kinds": kinds,
            "unmapped": unmapped, "bad_servings": bad_servings}
