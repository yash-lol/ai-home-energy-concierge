#!/usr/bin/env python3
"""Train the relevance model: will this household actually act on this advice?

THE QUESTION THIS ANSWERS
-------------------------
The rules decide what is *wasteful*. They have no idea what this particular
household will *do* about it. Someone who never touches lighting advice while
they are home, and never touches anything at 2 a.m., is being nagged by a system
that cannot learn. This model occupies the middle tier of the mandate table:

    rules + R7      what is LEGAL / SAFE     (cannot be overridden)
    this model      what is WORTH SHOWING    (advisory: rank and suppress only)
    the LLM         WORDING                  (no arithmetic)

It may reorder and it may hide. It may not change a number, and it may not
unlock anything the guardrail refused.

THE TEST THAT MATTERS
---------------------
A model trained on rule-generated data can score beautifully and know nothing —
it just re-derives R1-R8. So this script does not report accuracy on its own. It
reports accuracy **against two baselines**:

    majority   always predict the commoner class
    by-cost    rank by the dollar figure the rules already computed

If the model cannot beat *by-cost*, it has learned nothing the rules did not
already tell us, and it should not ship. That comparison is printed whether it
is flattering or not.

Pure Python, as with tools/train_occupancy.py: no numpy, no sklearn, and the
exported artifact is weights a person can read.

Run:
    python tools/train_relevance.py
    python tools/train_relevance.py --epochs 1200
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "reco_dataset.csv"
MODEL_PATH = ROOT / "hub" / "models" / "relevance_lr.json"

NUMERIC = ["hour", "on_peak", "occupancy", "presence_away", "lux", "temp_c",
           "humidity", "load_watts", "severity_rank", "usd",
           "minutes_unoccupied", "minutes_away"]
RULES = ["unoccupied_lights_on", "away_with_hvac_on", "daylight_waste",
         "hvac_with_window_open", "phantom_standby", "peak_hour_heavy_load",
         "peak_window_imminent"]

TEST_FRACTION = 0.30
MIN_POSITIVES = 30


def load(path: Path) -> List[Dict]:
    if not path.exists():
        raise SystemExit(f"no dataset at {path}\n"
                         f"  run: python tools/build_dataset.py")
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def featurise(rows: Sequence[Dict]) -> Tuple[List[str], List[List[float]],
                                             List[float], List[float]]:
    """Rows -> (names, X, y, sample_weights). Missing numerics take the median.

    An hour-of-day is also expanded into sin/cos, because 23:00 and 01:00 are
    adjacent in the world and 22 apart on a number line — and "never acts in the
    middle of the night" is exactly the pattern we are hoping it finds.
    """
    medians: Dict[str, float] = {}
    for k in NUMERIC:
        vals = [_f(r.get(k)) for r in rows]
        vals = [v for v in vals if v is not None]
        medians[k] = statistics.median(vals) if vals else 0.0

    names = list(NUMERIC) + ["hour_sin", "hour_cos"] + [f"rule={r}" for r in RULES]
    X, y, w = [], [], []
    for r in rows:
        row = [_f(r.get(k), medians[k]) for k in NUMERIC]
        h = _f(r.get("hour"), medians["hour"]) or 0.0
        row += [math.sin(2 * math.pi * h / 24.0), math.cos(2 * math.pi * h / 24.0)]
        row += [1.0 if r.get("rule_name") == name else 0.0 for name in RULES]
        X.append(row)
        y.append(_f(r.get("label"), 0.0))
        w.append(_f(r.get("weight"), 1.0))
    return names, X, y, w


def standardise(X: List[List[float]]):
    d = len(X[0])
    stats = []
    for j in range(d):
        col = [row[j] for row in X]
        sd = statistics.pstdev(col)
        stats.append({"mean": statistics.fmean(col), "std": sd if sd > 1e-9 else 1.0})
    Z = [[(row[j] - stats[j]["mean"]) / stats[j]["std"] for j in range(d)] for row in X]
    return Z, stats


def apply_scaler(X, stats):
    return [[(row[j] - stats[j]["mean"]) / stats[j]["std"] for j in range(len(stats))]
            for row in X]


def _sig(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def train(X, y, w, epochs=800, lr=0.3, l2=3e-3):
    """Weighted logistic regression. L2 is deliberately not tiny — the corpus is
    small and wide, and an unregularised fit here memorises rather than learns."""
    n, d = len(X), len(X[0])
    wt = [0.0] * d
    b = 0.0
    tot = sum(w) or 1.0
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi, si in zip(X, y, w):
            p = _sig(b + sum(a * c for a, c in zip(wt, xi)))
            e = (p - yi) * si
            for j in range(d):
                gw[j] += e * xi[j]
            gb += e
        for j in range(d):
            wt[j] -= lr * (gw[j] / tot + l2 * wt[j])
        b -= lr * (gb / tot)
    return wt, b


def predict(X, wt, b):
    return [_sig(b + sum(a * c for a, c in zip(wt, xi))) for xi in X]


def auc(scores: Sequence[float], y: Sequence[float]) -> float:
    """Rank-based AUC, ties averaged. 0.5 == coin flip."""
    pairs = sorted(zip(scores, y))
    ranks, i = [0.0] * len(pairs), 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    pos = sum(1 for _, t in pairs if t == 1)
    neg = len(pairs) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    s = sum(r for r, (_, t) in zip(ranks, pairs) if t == 1)
    return (s - pos * (pos + 1) / 2.0) / (pos * neg)


def score(p: Sequence[float], y: Sequence[float], thr=0.5) -> Dict:
    tp = sum(1 for a, b in zip(p, y) if a >= thr and b == 1)
    tn = sum(1 for a, b in zip(p, y) if a < thr and b == 0)
    fp = sum(1 for a, b in zip(p, y) if a >= thr and b == 0)
    fn = sum(1 for a, b in zip(p, y) if a < thr and b == 1)
    n = max(tp + tn + fp + fn, 1)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {"n": n, "accuracy": round((tp + tn) / n, 4),
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(2 * prec * rec / (prec + rec), 4) if (prec + rec) else 0.0,
            "auc": round(auc(p, y), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(CSV_PATH))
    ap.add_argument("--epochs", type=int, default=800)
    ap.add_argument("--out", default=str(MODEL_PATH))
    args = ap.parse_args()

    print("\n" + "=" * 78)
    print("  RELEVANCE MODEL — will this household act on this advice?")
    print("=" * 78 + "\n")

    rows = load(Path(args.csv))
    rows.sort(key=lambda r: _f(r.get("ts"), 0.0) or 0.0)
    pos = sum(1 for r in rows if r.get("label") == "1")
    provs = {r.get("provenance", "?") for r in rows}
    print(f"  {len(rows)} rows, {pos} positive, {len(rows)-pos} negative")
    print(f"  provenance: {', '.join(sorted(provs))}")
    if pos < MIN_POSITIVES:
        print(f"\n  {pos} positives is below the {MIN_POSITIVES} floor. Refusing to")
        print( "  quote a number from this. Generate or collect more first.\n")
        return 1

    # Chronological split, not random: the question is whether it generalises to
    # LATER behaviour, and a random split leaks the future into the training set.
    cut = int(len(rows) * (1 - TEST_FRACTION))
    names, X, y, w = featurise(rows)
    Xtr, ytr, wtr = X[:cut], y[:cut], w[:cut]
    Xte, yte = X[cut:], y[cut:]
    print(f"  chronological split: {len(Xtr)} train / {len(Xte)} held out")
    if sum(yte) == 0 or sum(yte) == len(yte):
        print("\n  held-out block is single-class — cannot evaluate. Need more data.\n")
        return 1

    Ztr, stats = standardise(Xtr)
    Zte = apply_scaler(Xte, stats)
    wt, b = train(Ztr, ytr, wtr, epochs=args.epochs)

    model_s = score(predict(Zte, wt, b), yte)

    # --- baselines, the part that decides whether this was worth doing -------
    majority = 1.0 if statistics.fmean(ytr) >= 0.5 else 0.0
    maj_s = score([majority] * len(yte), yte)
    # "by-cost": rank by the dollar figure the RULES already produced. If the
    # model cannot beat this, it has learned nothing new.
    costs = [_f(r.get("usd"), 0.0) or 0.0 for r in rows[cut:]]
    hi = max(costs) or 1.0
    cost_s = score([c / hi for c in costs], yte, thr=0.5)

    print("\n  " + "-" * 74)
    print(f"  {'':<22}{'acc':>8}{'prec':>8}{'rec':>8}{'F1':>8}{'AUC':>8}")
    for label, s in (("majority baseline", maj_s), ("by-cost baseline", cost_s),
                     ("LEARNED MODEL", model_s)):
        print(f"  {label:<22}{s['accuracy']:>8.3f}{s['precision']:>8.3f}"
              f"{s['recall']:>8.3f}{s['f1']:>8.3f}{s['auc']:>8.3f}")
    print("  " + "-" * 74)

    # Two different questions, and conflating them would mis-report the result.
    # AUC asks "does it ORDER them better"; F1 at a fixed threshold asks "is it
    # USABLE as a decision". A ranker can be excellent and still be unusable,
    # which is exactly what a raw cost score turns out to be.
    ranks_better = model_s["auc"] > cost_s["auc"] + 0.02
    decides_better = model_s["f1"] > cost_s["f1"] + 0.02

    print(f"  RANKING   : {'model' if ranks_better else 'no better than cost'}"
          f"  (AUC {model_s['auc']:.3f} vs {cost_s['auc']:.3f})")
    print(f"  DECIDING  : {'model' if decides_better else 'no better than cost'}"
          f"  (F1  {model_s['f1']:.3f} vs {cost_s['f1']:.3f}, "
          f"recall {model_s['recall']:.3f} vs {cost_s['recall']:.3f})")

    if ranks_better:
        verdict = ("Beats cost on ranking — it has learned something the rules "
                   "do not encode.")
    elif decides_better:
        verdict = ("Does NOT beat cost at ranking: the dollar figure the rules "
                   "already compute is an excellent ordering on this corpus. "
                   "What the model adds is a usable operating point — cost alone "
                   "cannot be thresholded without losing most of the positives. "
                   "Claim that, and nothing more.")
    else:
        verdict = ("Beats cost on neither axis. On this corpus it adds nothing "
                   "over the arithmetic already there. Say so rather than "
                   "shipping it as an improvement.")
    print("\n  VERDICT: " + verdict)
    beats = ranks_better or decides_better

    print("\n  --- what it learned (standardised weights, |w| > 0.05)")
    ranked = sorted(zip(names, wt), key=lambda kv: -abs(kv[1]))
    for name, coef in ranked:
        if abs(coef) > 0.05:
            arrow = "more likely to act" if coef > 0 else "less likely to act"
            print(f"      {name:<28} {coef:+.3f}   {arrow}")

    artifact = {
        "schema": 1,
        "task": "P(user acts on this recommendation)",
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trained_on": sorted(provs),
        "n_rows": len(rows), "n_positive": pos,
        "split": {"kind": "chronological", "train": len(Xtr), "held_out": len(Xte)},
        "features": names,
        "weights": [round(x, 6) for x in wt],
        "bias": round(b, 6),
        "standardiser": [{k: round(v, 6) for k, v in s.items()} for s in stats],
        "scores": {"model": model_s, "majority": maj_s, "by_cost": cost_s},
        "beats_cost_baseline": bool(beats),
        "beats_cost_ranking": bool(ranks_better),
        "beats_cost_deciding": bool(decides_better),
        "verdict": verdict,
        "mandate": "advisory only — may rank and suppress; may never alter a "
                   "figure or unlock an action the guardrail refused",
        "caveats": [
            "Trained on a SIMULATED household (tools/generate_sessions.py). It "
            "has learned that generator's occupant, not a real person.",
            "Report the sample count wherever the accuracy is reported.",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8", newline="\n")
    print(f"\n  wrote {out}  ({out.stat().st_size/1024:.1f} KB)")
    print("=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
