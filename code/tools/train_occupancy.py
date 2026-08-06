#!/usr/bin/env python3
"""Train an occupancy classifier on a published benchmark, export it as JSON.

WHY THIS EXISTS
---------------
The system has no occupancy sensor. Occupancy is a simulator toggle, a Modulino
button override, or a ToF node if one happens to be attached — all declared, but
all thin. This turns it into an *inference*: given temperature, humidity and
ambient light, is anyone in the room?

That matters beyond filling a gap. R1 ("lights on in an empty room") is the rule
with the weakest input in the whole system, and "we infer occupancy from three
environmental signals, trained on a 20,000-sample published benchmark" is a
materially stronger claim than "someone flipped a switch in a web page".

DATASET
-------
UCI Occupancy Detection (id 357) — Candanedo & Feldheim, 2016, "Accurate
occupancy detection of an office room from light, temperature, humidity and CO2
measurements using statistical learning models", Energy and Buildings 112.
https://archive.ics.uci.edu/dataset/357/occupancy+detection

Columns: date, Temperature (C), Humidity (%), Light (lux), CO2 (ppm),
HumidityRatio, Occupancy (0/1). 8143 train rows, plus two official test splits
of 2665 and 9752 rows, which is what the accuracies below are measured on.

We use Temperature, Humidity and Light. CO2 and HumidityRatio are dropped
because this project has no sensor for either, and training on a feature you
cannot supply at inference time produces a number you are not entitled to.

TWO VARIANTS, AND WHY THE SECOND ONE MATTERS
--------------------------------------------
  full     Temperature + Humidity + Light
  no_light Temperature + Humidity

Light dominates the UCI room: it is an office where the lights go on when
somebody walks in, so a classifier using it is not far from a threshold on lux.
Two problems with shipping only that:

  1. It is circular here. R3 already fires on `lux > 300`, so occupancy inferred
     mainly from lux would make R1 and R3 two views of one signal while looking
     like independent findings.
  2. It is a fair question from anyone who knows the dataset, and "our model
     learned a lux threshold" is a bad answer to give on stage.

So both are trained, both are reported, and `OCCUPANCY_VARIANT` picks which one
runs. The coefficients are printed so the dominance is visible rather than
buried.

NO DEPENDENCIES
---------------
Plain-Python logistic regression: full-batch gradient descent on standardised
features. sklearn and numpy are deliberately not used — the demo machine is
Windows on ARM, where this project has already lost time to wheels that do not
exist for win_arm64 (see the cryptography note in requirements.txt). A model
that is four floats and a dot product needs no ML runtime, runs identically on
the X Elite hub and the UNO Q's A53, and can have its weights printed on a
slide.

Run:
    python tools/train_occupancy.py              # download, train, export
    python tools/train_occupancy.py --epochs 800
    python tools/train_occupancy.py --offline    # use an already-downloaded copy
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "uci"
MODEL_DIR = ROOT / "hub" / "models"
MODEL_PATH = MODEL_DIR / "occupancy_lr.json"

DATASET_URL = "https://archive.ics.uci.edu/static/public/357/occupancy+detection.zip"
DATASET_CITATION = (
    "Candanedo, L. & Feldheim, V. (2016). Accurate occupancy detection of an "
    "office room from light, temperature, humidity and CO2 measurements using "
    "statistical learning models. Energy and Buildings, 112, 28-39. "
    "UCI Machine Learning Repository, dataset 357."
)

TRAIN_FILE = "datatraining.txt"
TEST_FILES = ["datatest.txt", "datatest2.txt"]

VARIANTS: Dict[str, List[str]] = {
    "full": ["temp_c", "humidity", "lux"],
    "no_light": ["temp_c", "humidity"],
}

# UCI column name -> the name this project uses on the wire, so the artifact
# speaks the hub's vocabulary and nobody has to translate at inference time.
COLUMN_MAP = {"Temperature": "temp_c", "Humidity": "humidity", "Light": "lux"}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def fetch(offline: bool = False) -> Dict[str, List[Dict[str, float]]]:
    """Download (once) and parse the three official splits."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    local_zip = DATA_DIR / "occupancy-detection.zip"

    if not local_zip.exists():
        if offline:
            raise SystemExit(f"--offline given but {local_zip} is not there yet")
        print(f"  downloading {DATASET_URL}")
        with urllib.request.urlopen(DATASET_URL, timeout=60) as r:
            local_zip.write_bytes(r.read())
    print(f"  corpus: {local_zip}  ({local_zip.stat().st_size/1024:.0f} KB)")

    out: Dict[str, List[Dict[str, float]]] = {}
    with zipfile.ZipFile(local_zip) as z:
        for name in [TRAIN_FILE] + TEST_FILES:
            out[name] = _parse(z.read(name).decode("utf-8"))
    return out


def _parse(text: str) -> List[Dict[str, float]]:
    """Parse one split.

    Two quirks worth handling explicitly rather than discovering at 2 a.m.:
    the header names 7 columns but every data row carries 8 (an unnamed leading
    row index), and the quoting is inconsistent between files — datatest2.txt
    leaves its dates unquoted where the others quote them. Reading the LAST
    seven fields sidesteps both.
    """
    rows: List[Dict[str, float]] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader)[-7:]
    for raw in reader:
        if len(raw) < 7:
            continue
        fields = raw[-7:]
        rec = dict(zip(header, fields))
        try:
            row = {ours: float(rec[theirs]) for theirs, ours in COLUMN_MAP.items()}
            row["occupancy"] = float(rec["Occupancy"])
        except (KeyError, ValueError):
            continue          # a malformed line is dropped, never guessed at
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def standardiser(rows: Sequence[Dict[str, float]], features: Sequence[str]):
    """Per-feature mean and standard deviation, stored in the artifact.

    Inference has to apply the exact same transform, so these travel WITH the
    weights. A model whose scaler lives somewhere else is a model that will
    silently mispredict the first time someone copies the weights alone.
    """
    stats = {}
    for f in features:
        col = [r[f] for r in rows]
        sd = statistics.pstdev(col)
        stats[f] = {"mean": statistics.fmean(col),
                    # A zero-variance feature would divide by zero; 1.0 leaves
                    # it as a constant offset the bias term absorbs.
                    "std": sd if sd > 1e-9 else 1.0,
                    "min": min(col), "max": max(col)}
    return stats


def _design(rows, features, stats) -> Tuple[List[List[float]], List[float]]:
    X = [[(r[f] - stats[f]["mean"]) / stats[f]["std"] for f in features] for r in rows]
    y = [r["occupancy"] for r in rows]
    return X, y


def _sigmoid(z: float) -> float:
    # Split on the sign to avoid math.exp overflowing on large negative z.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def train_lr(X: List[List[float]], y: List[float], epochs: int = 400,
             lr: float = 0.5, l2: float = 1e-4) -> Tuple[List[float], float, List[float]]:
    """Full-batch gradient descent. Returns (weights, bias, loss_curve)."""
    n, d = len(X), len(X[0])
    w = [0.0] * d
    b = 0.0
    curve: List[float] = []

    for epoch in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        loss = 0.0
        for xi, yi in zip(X, y):
            z = b + sum(wj * xj for wj, xj in zip(w, xi))
            p = _sigmoid(z)
            err = p - yi
            for j in range(d):
                gw[j] += err * xi[j]
            gb += err
            # Clamp inside the log so a confident-and-correct prediction cannot
            # produce log(0) = -inf and poison the whole curve.
            loss -= (yi * math.log(max(p, 1e-12))
                     + (1 - yi) * math.log(max(1 - p, 1e-12)))
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * (gb / n)
        if epoch % 20 == 0 or epoch == epochs - 1:
            curve.append(round(loss / n, 6))
    return w, b, curve


def evaluate(X, y, w, b, threshold: float = 0.5) -> Dict:
    tp = tn = fp = fn = 0
    for xi, yi in zip(X, y):
        p = _sigmoid(b + sum(wj * xj for wj, xj in zip(w, xi)))
        pred = 1.0 if p >= threshold else 0.0
        if pred == 1 and yi == 1:
            tp += 1
        elif pred == 0 and yi == 0:
            tn += 1
        elif pred == 1 and yi == 0:
            fp += 1
        else:
            fn += 1
    n = max(tp + tn + fp + fn, 1)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"n": n, "accuracy": round((tp + tn) / n, 4),
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "tp": tp, "tn": tn, "fp": fp, "fn": fn}


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=400)
    ap.add_argument("--lr", type=float, default=0.5)
    ap.add_argument("--offline", action="store_true",
                    help="use the already-downloaded corpus, do not fetch")
    ap.add_argument("--out", default=str(MODEL_PATH))
    args = ap.parse_args()

    print("\n" + "=" * 78)
    print("  OCCUPANCY MODEL — training on UCI Occupancy Detection (dataset 357)")
    print("=" * 78 + "\n")

    splits = fetch(args.offline)
    train_rows = splits[TRAIN_FILE]
    base_rate = statistics.fmean(r["occupancy"] for r in train_rows)
    print(f"  train rows: {len(train_rows)}   occupied: {base_rate*100:.1f}%")
    for t in TEST_FILES:
        print(f"  test  rows: {len(splits[t])}  ({t})")

    artifact = {
        "schema": 1,
        "task": "binary occupancy from environmental signals",
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trained_on": "public_benchmark",
        "dataset": {"name": "UCI Occupancy Detection (357)", "url": DATASET_URL,
                    "citation": DATASET_CITATION,
                    "n_train": len(train_rows),
                    "base_rate_occupied": round(base_rate, 4),
                    "dropped_columns": ["CO2", "HumidityRatio"],
                    "dropped_because": "this project has no sensor for either"},
        "algorithm": {"kind": "logistic_regression", "solver": "full-batch GD",
                      "epochs": args.epochs, "lr": args.lr, "l2": 1e-4,
                      "standardised": True},
        "variants": {},
    }

    for variant, features in VARIANTS.items():
        print(f"\n  --- variant '{variant}': {', '.join(features)}")
        stats = standardiser(train_rows, features)
        Xtr, ytr = _design(train_rows, features, stats)

        t0 = time.perf_counter()
        w, b, curve = train_lr(Xtr, ytr, epochs=args.epochs, lr=args.lr)
        train_ms = (time.perf_counter() - t0) * 1000.0

        scores = {"train": evaluate(Xtr, ytr, w, b)}
        for t in TEST_FILES:
            Xte, yte = _design(splits[t], features, stats)
            scores[t] = evaluate(Xte, yte, w, b)

        print(f"      trained in {train_ms/1000:.1f}s   final loss {curve[-1]}")
        print(f"      bias {b:+.4f}")
        for f, wj in zip(features, w):
            # Standardised weights are directly comparable, which is the point:
            # this is where the Light dominance becomes visible.
            print(f"      w[{f:<9}] {wj:+.4f}   (per 1 sd: "
                  f"{stats[f]['std']:.2f} {f})")
        for name, s in scores.items():
            label = {"train": "train"}.get(name, name)
            print(f"      {label:<16} acc {s['accuracy']:.4f}  "
                  f"P {s['precision']:.4f}  R {s['recall']:.4f}  F1 {s['f1']:.4f}")

        artifact["variants"][variant] = {
            "features": features,
            "weights": [round(x, 6) for x in w],
            "bias": round(b, 6),
            "standardiser": {f: {k: round(v, 6) for k, v in s.items()}
                             for f, s in stats.items()},
            "scores": scores,
            "train_ms": round(train_ms, 1),
            "loss_curve": curve,
        }

    # Which variant ships by default. 'full' is more accurate; 'no_light' is the
    # one to use if the lux circularity with R3 is raised — see the module
    # docstring. Both are in the artifact either way; this only sets the default.
    artifact["default_variant"] = "full"
    artifact["caveats"] = [
        "Trained on an office room in Belgium, February 2015. This is a "
        "different building, different climate and different occupancy pattern "
        "from the demo room — treat it as a prior, not a calibrated sensor.",
        "Light is the dominant feature in the 'full' variant. Since R3 already "
        "thresholds on lux, inferred occupancy and R3 are not independent "
        "evidence. The 'no_light' variant exists for that reason.",
        "Inputs outside the training range are extrapolation. predict() returns "
        "in_domain=False rather than silently guessing.",
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8", newline="\n")
    print(f"\n  wrote {out}  ({out.stat().st_size/1024:.1f} KB)")
    print("\n" + "=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
