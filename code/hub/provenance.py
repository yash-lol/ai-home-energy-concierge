"""Numeric provenance verifier — mechanical checking, not a promise.

THE PROBLEM
    Every project that puts an LLM near numbers says "we told it not to invent
    figures." That is an instruction, and instructions are not enforcement. The
    honest answer to "how do you know the model didn't make that up?" should be
    a mechanism and a violation count, not a prompt excerpt.

WHAT THIS DOES
    After any LLM response, extract EVERY numeric literal the model emitted and
    check each one appears in the deterministic source the model was given.
    Anything left over is flagged.

WHAT IT DELIBERATELY ALLOWS
    Small integers 0-24 (clock hours, ranks, counts) and rule ids R1-R7. Without
    that whitelist "turn it off before 9 PM" or "rank 2" trips the checker
    constantly and the signal drowns in noise. The tolerance is one-directional:
    it can only ever ACCEPT a number, never reject a real figure, and the
    whitelist is small enough to state out loud on stage.

WHAT IT IS NOT
    Not a hallucination detector for prose. A model can still write something
    false without using a digit. This checks numbers, which is where this system
    makes falsifiable claims, and it says so rather than implying more.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Tuple

# Matches 1, 1.5, $1.28, 0.550, 89%, 1,234.5 — the shapes that actually appear.
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Clock hours, ranks, small counts. See module docstring for why.
SMALL_INT_MAX = 24

# "R1".."R7" are rule identifiers, not quantities.
_RULE_ID_RE = re.compile(r"\bR[1-7]\b")

# Clock times. "18:30" is a reference to when something happened, not a figure
# the model computed — but its minutes field parses as 30, which is above
# SMALL_INT_MAX and was flagging perfectly honest answers ("shifting from peak
# at 18:30 to off-peak") as hallucinations. A verifier that cries wolf on a
# correct answer trains everyone to ignore the badge, which costs more than the
# check is worth. Same reasoning as rule ids: strip, do not whitelist a range.
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def extract_numbers(text: str) -> List[str]:
    """Every numeric literal in the text, in order of appearance."""
    if not text:
        return []
    # Drop rule ids and clock times first, so R7 does not register as 7 and
    # 18:30 does not register as 30.
    cleaned = _RULE_ID_RE.sub(" ", text)
    cleaned = _CLOCK_RE.sub(" ", cleaned)
    return [m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(cleaned)]


def _canonical(value: str) -> str:
    """Canonical form so 1.28 == $1.28 == 1.280 == 1.2800 compare equal."""
    s = str(value).strip().lstrip("$").rstrip("%").replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return s
    if f == int(f):
        return str(int(f))
    # Trim trailing zeros without going through float formatting twice.
    return repr(round(f, 6)).rstrip("0").rstrip(".")


def _allowed_forms(allowed: Dict[str, object]) -> set:
    """Every acceptable rendering of every figure the model was given.

    A model told `usd=0.319` may legitimately write 0.32 or 0.3 — it is quoting
    the figure at lower precision, not inventing one. Rounded forms are accepted
    explicitly rather than by a fuzzy distance test, so what counts as "the same
    number" stays inspectable.
    """
    forms = set()
    for raw in allowed.values():
        c = _canonical(raw)
        forms.add(c)
        try:
            f = float(c)
        except ValueError:
            continue
        for places in (0, 1, 2, 3, 4):
            forms.add(_canonical(f"{f:.{places}f}"))
        if f == int(f):
            forms.add(str(int(f)))
    return forms


def verify(text: str, allowed: Dict[str, object]) -> Tuple[bool, List[str]]:
    """Return (ok, unverified_numbers).

    `allowed` is the flat dict of every figure the model was given, e.g.
    {"r2-living-ac.usd": "0.319", "r2-living-ac.kwh": "0.5500"}.
    """
    forms = _allowed_forms(allowed)
    unverified: List[str] = []

    for token in extract_numbers(text):
        c = _canonical(token)
        if c in forms:
            continue
        try:
            f = float(c)
        except ValueError:
            continue
        # Whitelisted small integers: hours, ranks, counts.
        if f == int(f) and 0 <= abs(f) <= SMALL_INT_MAX:
            continue
        unverified.append(token)

    return (not unverified), unverified


def audit(text: str, allowed: Dict[str, object], label: str = "") -> Dict:
    """verify() plus a small structured record, for logging and the deck."""
    ok, unverified = verify(text, allowed)
    return {"label": label, "ok": ok, "unverified": unverified,
            "checked": len(extract_numbers(text)), "allowed_figures": len(allowed)}


# --------------------------------------------------------------------------
# Self-test — MUST catch a planted hallucinated number.
# Run: python hub/provenance.py
# --------------------------------------------------------------------------

_ALLOWED = {
    "r2-living-ac.usd": "0.319",
    "r2-living-ac.kwh": "0.5500",
    "r2-living-ac.co2": "0.1400",
    "r2-living-ac.watts": "1100.0",
    "r6-living-dryer.usd": "0.390",
}

_CASES: Sequence[Tuple[str, str, bool]] = (
    ("clean — quotes given figures exactly",
     "Turn off the A/C to save $0.319 and 0.55 kWh.", True),
    ("clean — rounds a given figure, which is quoting not inventing",
     "You could save about $0.32 today.", True),
    ("clean — clock hour and rank are whitelisted",
     "Rank 1: delay the dryer until 9 PM.", True),
    ("clean — rule id is not a quantity",
     "Suppressed by R7 comfort guardrail.", True),
    ("HALLUCINATION — invents a total nobody computed",
     "Turn off the A/C to save $0.319; that is $47.28 a year.", False),
    ("HALLUCINATION — invents a percentage",
     "This would cut your bill by 63%.", False),
    ("HALLUCINATION — does arithmetic it was told not to do",
     "The A/C and dryer together waste $0.709.", False),
)

if __name__ == "__main__":
    print("=" * 78)
    print("NUMERIC PROVENANCE VERIFIER — self-test")
    print(f"allowed figures: {sorted(set(_canonical(v) for v in _ALLOWED.values()))}")
    print("=" * 78)

    failures = 0
    for label, text, expect_ok in _CASES:
        ok, unverified = verify(text, _ALLOWED)
        correct = (ok == expect_ok)
        failures += 0 if correct else 1
        print(f"\n{'PASS' if correct else 'FAIL'}  {label}")
        print(f"      text     : {text}")
        print(f"      verdict  : {'verified' if ok else 'UNVERIFIED'}"
              f"  (expected {'verified' if expect_ok else 'UNVERIFIED'})")
        if unverified:
            print(f"      not in source: {unverified}")

    print("\n" + "=" * 78)
    print(f"{len(_CASES) - failures}/{len(_CASES)} cases behaved as expected")
    if failures:
        print("A hallucinated number went unnoticed — this check is the whole point.")
    print("=" * 78)
    raise SystemExit(1 if failures else 0)
