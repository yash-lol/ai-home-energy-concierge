"""Offline trainer for the edge anomaly detector.

Generates a simulated household rhythm, injects known anomaly classes, fits a
logistic regression, and emits `hub/anomaly_model.py` as plain Python literals
so the board needs no numpy, no sklearn and no model file format.

Run:  python tools/train_anomaly.py

WHY PURE PYTHON GRADIENT DESCENT
    sklearn would be fine here (this runs on the hub, not the board), but a
    hand-rolled fit keeps the published accuracy reproducible on any machine
    with a bare interpreter, and removes a dependency from the one artifact that
    has to be defensible on stage. Everything is seeded.

HONESTY — READ THIS BEFORE QUOTING THE ACCURACY
    The training data is SIMULATED. The accuracy below is therefore a statement
    about separability of the synthetic distribution, NOT evidence the model
    works on a real home. That caveat is written into the generated file, into
    anomaly.model_provenance(), and into every evidence line the detector emits.
    In deployment this would retrain on real logged history.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))

import anomaly  # noqa: E402  — for FEATURE_NAMES and featurize

SEED = 20260806
TRAIN_DAYS = 14
HOLDOUT_FRACTION = 0.25
ANOMALIES_PER_CLASS_PER_DAY = 3
EPOCHS = 400
LEARNING_RATE = 0.5
L2 = 1e-3

OUT_PATH = ROOT / "hub" / "anomaly_model.py"


# --------------------------------------------------------------------------
# Simulated household rhythm
# --------------------------------------------------------------------------

def _jitter(rng: random.Random, value: float, pct: float = 0.12) -> float:
    """+/- pct proportional noise, so the model cannot latch onto exact values."""
    return value * (1.0 + rng.uniform(-pct, pct))


def _normal_sample(rng: random.Random, day: int, hour: int) -> Tuple[Dict, datetime]:
    """One rhythm-consistent sample.

    The rhythm: asleep overnight, up and lit in the early morning, out at work
    through the day (bright, empty, nothing on), home and drawing power in the
    evening peak, winding down late.
    """
    dt = datetime(2026, 7, 1) + timedelta(days=day, hours=hour,
                                          minutes=rng.randint(0, 59))
    loads: Dict[str, Dict] = {}

    if 0 <= hour < 6:                       # overnight: asleep, standby only
        occupancy, presence = True, "home"
        lux = 0
        temp = _jitter(rng, 20.5, 0.05)
        if rng.random() < 0.85:
            loads["living/standby"] = {"state": "on", "watts": _jitter(rng, 35)}
    elif 6 <= hour < 9:                     # morning: up, lights while dim
        occupancy, presence = True, "home"
        lux = int(_jitter(rng, 150 + (hour - 6) * 200, 0.25))
        temp = _jitter(rng, 21.0, 0.05)
        if rng.random() < 0.7:
            loads["living/lights"] = {"state": "on", "watts": _jitter(rng, 240)}
        loads["living/standby"] = {"state": "on", "watts": _jitter(rng, 35)}
    elif 9 <= hour < 17:                    # workday: out, bright, quiet
        occupancy, presence = False, "away"
        lux = int(_jitter(rng, 800, 0.2))
        temp = _jitter(rng, 22.5, 0.06)
        loads["living/standby"] = {"state": "on", "watts": _jitter(rng, 35)}
    elif 17 <= hour < 22:                   # evening peak: home, busy
        occupancy, presence = True, "home"
        lux = int(_jitter(rng, max(0, 400 - (hour - 17) * 120), 0.3))
        temp = _jitter(rng, 22.5, 0.06)
        loads["living/lights"] = {"state": "on", "watts": _jitter(rng, 240)}
        loads["living/standby"] = {"state": "on", "watts": _jitter(rng, 35)}
        if rng.random() < 0.45:
            loads["living/ac"] = {"state": "on", "watts": _jitter(rng, 1100)}
        if rng.random() < 0.15:
            loads["living/dryer"] = {"state": "on", "watts": _jitter(rng, 2400)}
    else:                                   # 22-24: winding down
        occupancy, presence = True, "home"
        lux = int(_jitter(rng, 80, 0.4))
        temp = _jitter(rng, 21.5, 0.05)
        if rng.random() < 0.5:
            loads["living/lights"] = {"state": "on", "watts": _jitter(rng, 240)}
        loads["living/standby"] = {"state": "on", "watts": _jitter(rng, 35)}

    snap = {"rooms": {"living": {"occupancy": occupancy, "lux": lux,
                                 "temp_c": round(temp, 1), "humidity": 50}},
            "loads": loads, "user": {"presence": presence}, "now": 0}
    return snap, dt


ANOMALY_CLASSES = ("hvac_3am", "lights_daylight_away", "dryer_2am", "all_on_empty")


def _anomalous_sample(rng: random.Random, day: int, kind: str) -> Tuple[Dict, datetime]:
    """One deliberately out-of-pattern sample, from a named class."""
    if kind == "hvac_3am":
        # The motivating case: nothing a fixed threshold expresses.
        dt = datetime(2026, 7, 1) + timedelta(days=day, hours=rng.choice([2, 3, 4]))
        snap = {"rooms": {"living": {"occupancy": True, "lux": 0,
                                     "temp_c": round(_jitter(rng, 24.0, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/ac": {"state": "on", "watts": _jitter(rng, 1100)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "home"}, "now": 0}
    elif kind == "lights_daylight_away":
        dt = datetime(2026, 7, 1) + timedelta(days=day, hours=rng.choice([11, 12, 13, 14]))
        snap = {"rooms": {"living": {"occupancy": False,
                                     "lux": int(_jitter(rng, 850, 0.15)),
                                     "temp_c": round(_jitter(rng, 23.0, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/lights": {"state": "on", "watts": _jitter(rng, 240)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    elif kind == "dryer_2am":
        dt = datetime(2026, 7, 1) + timedelta(days=day, hours=rng.choice([1, 2, 3]))
        snap = {"rooms": {"living": {"occupancy": False, "lux": 0,
                                     "temp_c": round(_jitter(rng, 21.0, 0.05), 1),
                                     "humidity": 50}},
                "loads": {"living/dryer": {"state": "on", "watts": _jitter(rng, 2400)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    else:  # all_on_empty
        dt = datetime(2026, 7, 1) + timedelta(days=day, hours=rng.randint(9, 16))
        snap = {"rooms": {"living": {"occupancy": False,
                                     "lux": int(_jitter(rng, 700, 0.2)),
                                     "temp_c": round(_jitter(rng, 24.5, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/lights": {"state": "on", "watts": _jitter(rng, 240)},
                          "living/ac": {"state": "on", "watts": _jitter(rng, 1100)},
                          "living/dryer": {"state": "on", "watts": _jitter(rng, 2400)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    return snap, dt


def build_dataset(rng: random.Random) -> Tuple[List[List[float]], List[int]]:
    X: List[List[float]] = []
    y: List[int] = []

    for day in range(TRAIN_DAYS):
        for hour in range(24):
            # Two samples an hour, so the normal class dominates as it should:
            # anomalies are rare by definition and the model should learn that.
            for _ in range(2):
                snap, dt = _normal_sample(rng, day, hour)
                X.append(anomaly.featurize(snap, dt))
                y.append(0)

        # Several anomalies a day, each class repeated with fresh jitter.
        # ANOMALIES_PER_CLASS_PER_DAY=1 gave only 14 examples per class, and the
        # hardest class (lights on in bright daylight while away) was left
        # under-determined: at midday the time features say "normal, everyone is
        # out", and lights_on is the ONLY feature arguing otherwise. It landed at
        # 0.697 against a 0.70 threshold. More examples of each class sharpens
        # that boundary honestly, which is the right fix — lowering the threshold
        # to make one test pass would just be fitting the test.
        for kind in ANOMALY_CLASSES:
            for _ in range(ANOMALIES_PER_CLASS_PER_DAY):
                snap, dt = _anomalous_sample(rng, day, kind)
                X.append(anomaly.featurize(snap, dt))
                y.append(1)

    return X, y


# --------------------------------------------------------------------------
# Logistic regression, plain Python
# --------------------------------------------------------------------------

def _sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit(X: List[List[float]], y: List[int]) -> Tuple[List[float], float]:
    """Batch gradient descent with L2. Class-weighted, because anomalies are
    deliberately the minority: without weighting the model can score 93% by
    always predicting 'normal' and detecting nothing at all."""
    n_features = len(X[0])
    w = [0.0] * n_features
    b = 0.0

    n_pos = sum(y) or 1
    n_neg = (len(y) - sum(y)) or 1
    w_pos = len(y) / (2.0 * n_pos)
    w_neg = len(y) / (2.0 * n_neg)

    for _ in range(EPOCHS):
        gw = [0.0] * n_features
        gb = 0.0
        total_w = 0.0
        for xi, yi in zip(X, y):
            z = b
            for j in range(n_features):
                z += w[j] * xi[j]
            p = _sigmoid(z)
            cw = w_pos if yi == 1 else w_neg
            err = (p - yi) * cw
            total_w += cw
            for j in range(n_features):
                gw[j] += err * xi[j]
            gb += err
        for j in range(n_features):
            w[j] -= LEARNING_RATE * (gw[j] / total_w + L2 * w[j])
        b -= LEARNING_RATE * (gb / total_w)

    return w, b


def predict(w: List[float], b: float, x: List[float]) -> float:
    z = b
    for j in range(len(w)):
        z += w[j] * x[j]
    return _sigmoid(z)


def confusion(w, b, X, y, threshold) -> Tuple[int, int, int, int]:
    tp = tn = fp = fn = 0
    for xi, yi in zip(X, y):
        pred = 1 if predict(w, b, xi) >= threshold else 0
        if yi == 1 and pred == 1:
            tp += 1
        elif yi == 0 and pred == 0:
            tn += 1
        elif yi == 0 and pred == 1:
            fp += 1
        else:
            fn += 1
    return tp, tn, fp, fn


def main() -> int:
    rng = random.Random(SEED)
    X, y = build_dataset(rng)

    idx = list(range(len(X)))
    rng.shuffle(idx)
    cut = int(len(idx) * (1 - HOLDOUT_FRACTION))
    tr, ho = idx[:cut], idx[cut:]
    Xtr, ytr = [X[i] for i in tr], [y[i] for i in tr]
    Xho, yho = [X[i] for i in ho], [y[i] for i in ho]

    print("=" * 78)
    print("TRAINING EDGE ANOMALY DETECTOR")
    print("=" * 78)
    print(f"  simulated days   : {TRAIN_DAYS}")
    print(f"  samples          : {len(X)}  ({sum(y)} anomalous, {len(y)-sum(y)} normal)")
    print(f"  train / holdout  : {len(Xtr)} / {len(Xho)}")
    print(f"  seed             : {SEED} (fixed — figures below reproduce exactly)")
    print("  NOTE: training data is SIMULATED. Accuracy measures separability of")
    print("        the synthetic distribution, not real-world performance.")
    print()

    w, b = fit(Xtr, ytr)

    th = anomaly.ANOMALY_THRESHOLD
    tp, tn, fp, fn = confusion(w, b, Xtr, ytr, th)
    train_acc = (tp + tn) / max(1, len(ytr))
    tp2, tn2, fp2, fn2 = confusion(w, b, Xho, yho, th)
    ho_acc = (tp2 + tn2) / max(1, len(yho))

    precision = tp2 / max(1, tp2 + fp2)
    recall = tp2 / max(1, tp2 + fn2)

    print(f"  train accuracy   : {train_acc:.4f}")
    print(f"  HOLDOUT accuracy : {ho_acc:.4f}")
    print(f"  holdout precision: {precision:.4f}   recall: {recall:.4f}")
    print()
    print("  holdout confusion matrix (threshold "
          f"{th}):")
    print("                 predicted")
    print("                 normal  anomalous")
    print(f"    normal       {tn2:6d}  {fp2:9d}")
    print(f"    anomalous    {fn2:6d}  {tp2:9d}")
    print()
    print("  learned weights:")
    for name, weight in sorted(zip(anomaly.FEATURE_NAMES, w),
                               key=lambda kv: -abs(kv[1])):
        print(f"    {name:16s} {weight:+8.4f}")
    print(f"    {'(bias)':16s} {b:+8.4f}")

    header = f'''"""Generated by tools/train_anomaly.py — DO NOT EDIT BY HAND.

Logistic-regression weights for the edge anomaly detector.

  trained          : {datetime.now().strftime("%Y-%m-%d")}
  simulated days   : {TRAIN_DAYS}
  samples          : {len(X)} ({sum(y)} anomalous, {len(y)-sum(y)} normal)
  train accuracy   : {train_acc:.4f}
  holdout accuracy : {ho_acc:.4f}
  holdout precision: {precision:.4f}
  holdout recall   : {recall:.4f}
  seed             : {SEED}

*** THE TRAINING DATA IS SIMULATED. ***

These figures describe how separable a synthetic household distribution is.
They are NOT evidence of real-world accuracy, and nothing that displays a score
derived from these weights may imply otherwise. In deployment the model would
retrain on real logged history. See anomaly.model_provenance().
"""

# Order matches anomaly.FEATURE_NAMES exactly.
FEATURE_NAMES = {anomaly.FEATURE_NAMES!r}

WEIGHTS = [
'''
    body = "".join(f"    {v!r},  # {n}\n" for n, v in zip(anomaly.FEATURE_NAMES, w))
    footer = f''']

BIAS = {b!r}

TRAIN_DAYS = {TRAIN_DAYS}
TRAIN_SAMPLES = {len(X)}
TRAIN_ACCURACY = {train_acc!r}
HOLDOUT_ACCURACY = {ho_acc!r}
HOLDOUT_PRECISION = {precision!r}
HOLDOUT_RECALL = {recall!r}
TRAINING_DATA_IS_SIMULATED = True
SEED = {SEED}
'''

    OUT_PATH.write_text(header + body + footer, encoding="utf-8", newline="\n")
    print()
    print(f"  wrote {OUT_PATH}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
