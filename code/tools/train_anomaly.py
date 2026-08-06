"""Offline trainer for the edge anomaly detector.

Fits a logistic regression and emits `hub/anomaly_model.py` as plain Python
literals, so the board needs no numpy, no sklearn and no model file format.

Run:  python tools/train_anomaly.py
      python tools/train_anomaly.py --synthetic-only     # ignore the corpus
      python tools/train_anomaly.py --no-gate            # write even on regression

WHERE THE NORMAL CLASS COMES FROM
    Preferred: the RECORDED SESSION CORPUS in `data/sessions/*.jsonl`. Those are
    real `tick` rows written by hub/recorder.py — the same rows the live hub
    writes while the demo runs — so the normal class is the household's actual
    observed rhythm rather than a second, parallel idea of one.

    This matters beyond tidiness. The first version of this file contained its
    own five-branch household (`_normal_sample`, kept below as the fallback),
    which disagreed with the household in tools/generate_sessions.py: fixed
    departure at 09:00 every single day, no weekends, no weather, no hot days.
    A model trained on that learned a rhythm nothing else in the repo believes
    in. Reading the corpus means there is ONE household simulator, and when real
    sessions exist the same code path trains on them with no changes.

    Fallback: `_normal_sample` below, so a fresh clone with no corpus still
    trains. It is a worse dataset and the run says so.

THE LABELLING PROBLEM, AND THE HONEST ANSWER
    The corpus is not uniformly normal. That household forgets lights when it
    leaves (`forgets_lights_p`), so some recorded ticks are genuine waste —
    including the exact situation the `lights_daylight_away` anomaly class
    describes. Labelling every recorded tick "normal" would put a hard label
    conflict in the training set and teach the model that lights-on-while-away
    is unremarkable.

    So the normal class is recorded ticks WHERE NO RULE FIRED. Tick rows carry
    `open_reco_ids`; a tick with any open finding is excluded entirely — it is
    neither clean-normal nor the model's target, because R1-R8 already own it.
    The count of excluded ticks is printed, because it is a real property of the
    data and not a footnote.

HOW IT IS EVALUATED, AND WHY THE OLD NUMBER WAS TOO GOOD
    The previous holdout was a RANDOM SHUFFLE, and it reported 0.9714. With four
    hand-written anomaly templates plus jitter, a randomly held-out anomaly is a
    near-duplicate of one the model trained on: that number measured "can it
    recognise jittered copies of things it has seen", which is not the question.

    Now:
      * the split is BY DAY, so no sample's own hour-neighbours leak across it;
      * per-class recall is reported, not one pooled accuracy;
      * LEAVE-ONE-CLASS-OUT retrains with a whole anomaly class removed and
        reports recall on that unseen class. That is the number that says
        whether it generalises past its own templates, and it is much lower than
        the pooled figure. Both are printed. The low one is the honest one.

THE SCENARIO GATE
    After fitting, the candidate weights are run against hub/anomaly.py's own
    scenarios BEFORE anything is written. If a state that must stay quiet starts
    firing, or the motivating 3 AM case stops firing, the run refuses to write
    and exits non-zero. Retraining the evening before a demo should not be able
    to silently break the demo. `--no-gate` overrides, loudly.

HONESTY — READ THIS BEFORE QUOTING ANY ACCURACY
    Today the corpus is itself SIMULATED (generate_sessions.py), so these
    figures still describe separability of a synthetic distribution, NOT
    evidence the model works on a real home. The generated file records which
    source was used and how many ticks came from each. In deployment this
    retrains on real logged history through the same path.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))

import anomaly  # noqa: E402  — for FEATURE_NAMES, featurize, SCENARIOS

SEED = 20260806
TRAIN_DAYS = 14                  # synthetic fallback only
HOLDOUT_FRACTION = 0.25
ANOMALIES_PER_CLASS_PER_DAY = 3
EPOCHS = 400
LEARNING_RATE = 0.5
L2 = 1e-3

CORPUS_DIR = ROOT / "data" / "sessions"
# Below this many usable ticks the corpus is not worth preferring over the
# synthetic fallback — a few smoke-test rows are not a household.
MIN_CORPUS_TICKS = 500
# Cap, so a 30 MB corpus does not make a pure-Python fit take minutes. Sampled
# evenly across the whole corpus, never truncated to the first N, which would
# quietly train on only the earliest days.
MAX_CORPUS_TICKS = 6000

OUT_PATH = ROOT / "hub" / "anomaly_model.py"


# --------------------------------------------------------------------------
# Source 1 (preferred): the recorded session corpus
# --------------------------------------------------------------------------

def corpus_files(directory: Path = CORPUS_DIR) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"))


def load_corpus(paths: Sequence[Path]) -> Tuple[List[List[float]], List[str], Dict]:
    """Recorded `tick` rows -> (features, day keys, stats).

    Returns one feature vector per USABLE tick, plus the calendar day each came
    from so the split can be made by day. Rows that had an open finding are
    dropped — see the module docstring.
    """
    feats: List[List[float]] = []
    days: List[str] = []
    stats = {"files": 0, "rows": 0, "ticks": 0, "excluded_open_reco": 0,
             "unparsable": 0, "synthetic_ticks": 0, "measured_ticks": 0}

    for path in paths:
        stats["files"] += 1
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats["rows"] += 1
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    stats["unparsable"] += 1
                    continue
                if row.get("type") != "tick":
                    continue
                stats["ticks"] += 1

                # A tick with an open finding is already claimed by a rule.
                if row.get("open_reco_ids"):
                    stats["excluded_open_reco"] += 1
                    continue

                ts = row.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromtimestamp(float(ts))
                except (ValueError, OSError, OverflowError):
                    continue

                snap = {"rooms": row.get("rooms") or {},
                        "loads": row.get("loads") or {},
                        "user": row.get("user") or {},
                        "now": float(ts)}
                if not snap["rooms"]:
                    continue

                try:
                    feats.append(anomaly.featurize(snap, dt))
                except Exception:
                    stats["unparsable"] += 1
                    continue
                days.append(dt.strftime("%Y-%m-%d"))

                # Provenance, carried into the generated model file. A run that
                # trained on invented sensor values must never be reported the
                # same way as one that trained on measured ones.
                prov = (row.get("provenance") or {})
                if str(prov.get("verdict", "")).startswith("measured"):
                    stats["measured_ticks"] += 1
                else:
                    stats["synthetic_ticks"] += 1

    return feats, days, stats


def subsample(feats: List[List[float]], days: List[str],
              cap: int) -> Tuple[List[List[float]], List[str]]:
    """Even stride across the whole corpus, so every day stays represented."""
    if len(feats) <= cap:
        return feats, days
    step = len(feats) / float(cap)
    idx = [int(i * step) for i in range(cap)]
    return [feats[i] for i in idx], [days[i] for i in idx]


# --------------------------------------------------------------------------
# Source 2 (fallback): the original inline household
# --------------------------------------------------------------------------

def _jitter(rng: random.Random, value: float, pct: float = 0.12) -> float:
    """+/- pct proportional noise, so the model cannot latch onto exact values."""
    return value * (1.0 + rng.uniform(-pct, pct))


def _normal_sample(rng: random.Random, day: int, hour: int) -> Tuple[Dict, datetime]:
    """One rhythm-consistent sample.

    Kept so a fresh clone with no recorded corpus still trains. Deliberately not
    improved any further: the corpus path is the one that should get better, and
    two households that drift apart again is the exact problem this replaced.
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


# --------------------------------------------------------------------------
# The anomaly classes
# --------------------------------------------------------------------------
# Six, not four. Two were added so leave-one-class-out has enough folds to say
# something, and because the original four left whole regions of the feature
# space unexampled: nothing covered "lights burning all night in an empty
# house", and nothing covered a high-draw state while the household is asleep.

ANOMALY_CLASSES = ("hvac_3am", "lights_daylight_away", "dryer_2am",
                   "all_on_empty", "lights_all_night_away", "everything_on_asleep")


def _anomalous_sample(rng: random.Random, day_dt: datetime,
                      kind: str) -> Tuple[Dict, datetime]:
    """One deliberately out-of-pattern sample, from a named class.

    `day_dt` is the calendar day to place it on, so injected anomalies span the
    same days as the corpus and a by-day split holds out both classes together.
    """
    def at(hour_choices) -> datetime:
        return day_dt.replace(hour=rng.choice(hour_choices),
                              minute=rng.randint(0, 59), second=0, microsecond=0)

    if kind == "hvac_3am":
        # The motivating case: nothing a fixed threshold expresses.
        dt = at([2, 3, 4])
        snap = {"rooms": {"living": {"occupancy": True, "lux": 0,
                                     "temp_c": round(_jitter(rng, 24.0, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/ac": {"state": "on", "watts": _jitter(rng, 1100)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "home"}, "now": 0}
    elif kind == "lights_daylight_away":
        dt = at([11, 12, 13, 14])
        snap = {"rooms": {"living": {"occupancy": False,
                                     "lux": int(_jitter(rng, 850, 0.15)),
                                     "temp_c": round(_jitter(rng, 23.0, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/lights": {"state": "on", "watts": _jitter(rng, 240)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    elif kind == "dryer_2am":
        dt = at([1, 2, 3])
        snap = {"rooms": {"living": {"occupancy": False, "lux": 0,
                                     "temp_c": round(_jitter(rng, 21.0, 0.05), 1),
                                     "humidity": 50}},
                "loads": {"living/dryer": {"state": "on", "watts": _jitter(rng, 2400)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    elif kind == "lights_all_night_away":
        # Left the lights on and went away for the night. Distinct from
        # dryer_2am (which is a heavy load) and from lights_daylight_away
        # (which is bright): here it is dark, empty, and small but constant.
        dt = at([0, 1, 2, 3, 4, 5])
        snap = {"rooms": {"living": {"occupancy": False,
                                     "lux": rng.randint(0, 15),
                                     "temp_c": round(_jitter(rng, 20.5, 0.05), 1),
                                     "humidity": 50}},
                "loads": {"living/lights": {"state": "on", "watts": _jitter(rng, 240)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "away"}, "now": 0}
    elif kind == "everything_on_asleep":
        # Home and occupied, so occupancy argues "normal" hard — the only signal
        # is that the draw is a waking-hours draw at an hour nobody is awake.
        dt = at([2, 3, 4, 5])
        snap = {"rooms": {"living": {"occupancy": True,
                                     "lux": int(_jitter(rng, 180, 0.3)),
                                     "temp_c": round(_jitter(rng, 23.5, 0.06), 1),
                                     "humidity": 50}},
                "loads": {"living/lights": {"state": "on", "watts": _jitter(rng, 240)},
                          "living/ac": {"state": "on", "watts": _jitter(rng, 1100)},
                          "living/standby": {"state": "on", "watts": _jitter(rng, 35)}},
                "user": {"presence": "home"}, "now": 0}
    else:  # all_on_empty
        dt = at(list(range(9, 17)))
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


# --------------------------------------------------------------------------
# Assembling the dataset
# --------------------------------------------------------------------------

class Dataset:
    """Features, labels, per-sample day key and per-sample anomaly class."""

    def __init__(self) -> None:
        self.X: List[List[float]] = []
        self.y: List[int] = []
        self.days: List[str] = []
        self.klass: List[str] = []     # "" for normals

    def add(self, x, label, day, klass="") -> None:
        self.X.append(x)
        self.y.append(label)
        self.days.append(day)
        self.klass.append(klass)

    def __len__(self) -> int:
        return len(self.X)


def build_from_corpus(rng: random.Random, feats, days) -> Dataset:
    ds = Dataset()
    for x, d in zip(feats, days):
        ds.add(x, 0, d)

    # Inject anomalies onto the corpus's OWN days, scaled so the anomaly rate
    # stays realistic: anomalies are rare, and a model that is told otherwise
    # learns to cry wolf. Roughly 1 anomaly per 12 normal ticks.
    day_keys = sorted(set(days))
    per_day = max(1, round(len(feats) / max(1, len(day_keys)) / 12.0
                           / len(ANOMALY_CLASSES)))
    for dk in day_keys:
        day_dt = datetime.strptime(dk, "%Y-%m-%d")
        for kind in ANOMALY_CLASSES:
            for _ in range(per_day):
                snap, dt = _anomalous_sample(rng, day_dt, kind)
                ds.add(anomaly.featurize(snap, dt), 1, dk, kind)
    return ds


def build_synthetic(rng: random.Random) -> Dataset:
    ds = Dataset()
    base = datetime(2026, 7, 1)
    for day in range(TRAIN_DAYS):
        dk = (base + timedelta(days=day)).strftime("%Y-%m-%d")
        for hour in range(24):
            for _ in range(2):
                snap, dt = _normal_sample(rng, day, hour)
                ds.add(anomaly.featurize(snap, dt), 0, dk)
        for kind in ANOMALY_CLASSES:
            for _ in range(ANOMALIES_PER_CLASS_PER_DAY):
                snap, dt = _anomalous_sample(rng, base + timedelta(days=day), kind)
                ds.add(anomaly.featurize(snap, dt), 1, dk, kind)
    return ds


def split_by_day(ds: Dataset, holdout_fraction: float) -> Tuple[List[int], List[int]]:
    """Chronological split on WHOLE DAYS.

    A random per-sample shuffle leaks: two ticks minutes apart are nearly the
    same sample, so the holdout ends up containing near-copies of training rows
    and the reported accuracy is optimistic. Holding out whole days removes
    that, and matches how the model would actually be used — fit on history,
    applied to a day it has never seen.
    """
    day_keys = sorted(set(ds.days))
    if len(day_keys) < 2:
        cut = int(len(ds) * (1 - holdout_fraction))
        return list(range(cut)), list(range(cut, len(ds)))
    n_hold = max(1, int(round(len(day_keys) * holdout_fraction)))
    hold_days = set(day_keys[-n_hold:])
    train = [i for i, d in enumerate(ds.days) if d not in hold_days]
    hold = [i for i, d in enumerate(ds.days) if d in hold_days]
    return train, hold


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


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def per_class_recall(w, b, ds: Dataset, idx: Sequence[int],
                     threshold: float) -> Dict[str, Tuple[int, int]]:
    """{class: (caught, total)} over the given sample indices."""
    out: Dict[str, List[int]] = {}
    for i in idx:
        if ds.y[i] != 1:
            continue
        k = ds.klass[i] or "(unlabelled)"
        slot = out.setdefault(k, [0, 0])
        slot[1] += 1
        if predict(w, b, ds.X[i]) >= threshold:
            slot[0] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def leave_one_class_out(ds: Dataset, threshold: float) -> Dict[str, Tuple[int, int]]:
    """Retrain with each anomaly class removed; report recall on the unseen one.

    This is the generalisation question: given five kinds of unusual, does the
    model recognise a sixth it was never shown? Pooled holdout accuracy cannot
    answer it, because every holdout anomaly has same-class siblings in train.
    """
    out: Dict[str, Tuple[int, int]] = {}
    for held in ANOMALY_CLASSES:
        tr = [i for i in range(len(ds)) if ds.klass[i] != held]
        te = [i for i in range(len(ds)) if ds.klass[i] == held]
        if not te:
            continue
        w, b = fit([ds.X[i] for i in tr], [ds.y[i] for i in tr])
        caught = sum(1 for i in te if predict(w, b, ds.X[i]) >= threshold)
        out[held] = (caught, len(te))
    return out


def correlation(ds: Dataset, a: str, b: str) -> Optional[float]:
    """Pearson r between two feature columns, over the whole dataset."""
    try:
        ia, ib = anomaly.FEATURE_NAMES.index(a), anomaly.FEATURE_NAMES.index(b)
    except ValueError:
        return None
    xs = [row[ia] for row in ds.X]
    ys = [row[ib] for row in ds.X]
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# --------------------------------------------------------------------------
# The scenario gate
# --------------------------------------------------------------------------
# Which of hub/anomaly.py's scenarios may BLOCK a write. Matched on a substring
# of the scenario label so this stays readable and so anomaly.py — a file the
# board imports — does not have to grow a field to support the trainer.
#
# Blocking: the two ordinary states that must stay quiet (a false positive on a
# normal evening is fatal on stage) and the motivating case the whole tier
# exists to catch.
#
# NOT blocking: "R3's territory". That scenario documents a known boundary the
# deterministic rule already owns, and a better dataset may legitimately push it
# over the threshold. Blocking on it would mean refusing improvements for
# agreeing with R3, so the run reports the change instead of vetoing it.
BLOCKING_MARKERS = ("Normal evening", "Empty house", "THE MOTIVATING CASE")


def scenario_report(w, b, threshold: float) -> Tuple[List[Tuple], bool]:
    """[(label, score, flagged, expect, ok, blocking)], and whether to allow."""
    rows = []
    allow = True
    for label, (snap, dt), expect, _note in anomaly.SCENARIOS:
        s = predict(w, b, anomaly.featurize(snap, dt))
        flagged = s >= threshold
        ok = (expect == "high" and flagged) or (expect == "low" and not flagged)
        blocking = any(m in label for m in BLOCKING_MARKERS)
        if blocking and not ok:
            allow = False
        rows.append((label, s, flagged, expect, ok, blocking))
    return rows, allow


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--synthetic-only", action="store_true",
                    help="ignore data/sessions and use the inline household")
    ap.add_argument("--no-gate", action="store_true",
                    help="write the model even if a blocking scenario regressed")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--max-ticks", type=int, default=MAX_CORPUS_TICKS)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    th = anomaly.ANOMALY_THRESHOLD

    print("=" * 78)
    print("TRAINING EDGE ANOMALY DETECTOR")
    print("=" * 78)

    source = "synthetic_inline"
    cstats: Dict = {}
    ds: Optional[Dataset] = None

    if not args.synthetic_only:
        feats, days, cstats = load_corpus(corpus_files())
        if len(feats) >= MIN_CORPUS_TICKS:
            kept, kdays = subsample(feats, days, args.max_ticks)
            print(f"  source           : RECORDED CORPUS ({CORPUS_DIR})")
            print(f"  files read       : {cstats['files']}  "
                  f"({cstats['rows']} rows, {cstats['ticks']} ticks)")
            print(f"  usable normals   : {len(feats)}  "
                  f"(excluded {cstats['excluded_open_reco']} ticks that already "
                  f"had an open finding)")
            if len(kept) < len(feats):
                print(f"  subsampled to    : {len(kept)} "
                      f"(even stride across {len(set(days))} days)")
            ds = build_from_corpus(rng, kept, kdays)
            source = "recorded_corpus"
        else:
            print(f"  corpus           : {len(feats)} usable ticks "
                  f"(< {MIN_CORPUS_TICKS}) — falling back to the inline household")
            print("                     generate one: python tools/generate_sessions.py")

    if ds is None:
        ds = build_synthetic(rng)
        print("  source           : INLINE SYNTHETIC HOUSEHOLD (fallback)")
        print(f"  simulated days   : {TRAIN_DAYS}")

    n_days = len(set(ds.days))
    n_pos = sum(ds.y)
    print(f"  samples          : {len(ds)}  ({n_pos} anomalous, "
          f"{len(ds) - n_pos} normal, {n_pos / len(ds) * 100:.1f}% anomaly rate)")
    print(f"  calendar days    : {n_days}")
    print(f"  seed             : {args.seed} (fixed — figures below reproduce exactly)")
    if source == "recorded_corpus":
        print(f"  provenance       : {cstats['measured_ticks']} measured / "
              f"{cstats['synthetic_ticks']} simulated ticks")
    print("  NOTE: the normal class is only as real as the corpus. Today that")
    print("        corpus is generated, so these figures measure separability of")
    print("        a synthetic distribution, not real-world performance.")
    print()

    tr, ho = split_by_day(ds, HOLDOUT_FRACTION)
    print(f"  split            : BY DAY — {len(tr)} train / {len(ho)} holdout "
          f"({len(set(ds.days[i] for i in ho))} unseen days)")
    print()

    w, b = fit([ds.X[i] for i in tr], [ds.y[i] for i in tr])

    tp, tn, fp, fn = confusion(w, b, [ds.X[i] for i in tr],
                               [ds.y[i] for i in tr], th)
    train_acc = (tp + tn) / max(1, len(tr))
    tp2, tn2, fp2, fn2 = confusion(w, b, [ds.X[i] for i in ho],
                                   [ds.y[i] for i in ho], th)
    ho_acc = (tp2 + tn2) / max(1, len(ho))
    precision = tp2 / max(1, tp2 + fp2)
    recall = tp2 / max(1, tp2 + fn2)

    print(f"  train accuracy   : {train_acc:.4f}")
    print(f"  HOLDOUT accuracy : {ho_acc:.4f}   (unseen days)")
    print(f"  holdout precision: {precision:.4f}   recall: {recall:.4f}")
    print()
    print(f"  holdout confusion matrix (threshold {th}):")
    print("                 predicted")
    print("                 normal  anomalous")
    print(f"    normal       {tn2:6d}  {fp2:9d}")
    print(f"    anomalous    {fn2:6d}  {tp2:9d}")
    print()

    print("  per-class recall on the holdout:")
    for k, (caught, total) in per_class_recall(w, b, ds, ho, th).items():
        print(f"    {k:24s} {caught:4d}/{total:<4d}  {caught / max(1, total):.2f}")
    print()

    print("  LEAVE-ONE-CLASS-OUT — recall on a class never trained on.")
    print("  This is the generalisation number. It is lower than the pooled")
    print("  figure above, and it is the one to quote when asked whether the")
    print("  model does anything beyond recognising its own templates.")
    loco = leave_one_class_out(ds, th)
    caught_total = sum(c for c, _ in loco.values())
    n_total = sum(n for _, n in loco.values())
    for k, (caught, total) in loco.items():
        verdict = "generalises" if caught / max(1, total) >= 0.5 else "MISSED"
        print(f"    {k:24s} {caught:4d}/{total:<4d}  "
              f"{caught / max(1, total):.2f}  {verdict}")
    print(f"    {'overall':24s} {caught_total:4d}/{n_total:<4d}  "
          f"{caught_total / max(1, n_total):.2f}")
    print()

    r = correlation(ds, "occupancy", "presence_away")
    if r is not None:
        print(f"  occupancy vs presence_away correlation: r = {r:+.3f}")
        if abs(r) > 0.8:
            print("    Near-collinear: these two features carry almost the same")
            print("    information in this data, so their individual weights split")
            print("    one effect and should not be read as two pieces of evidence.")
        print()

    print("  learned weights:")
    for name, weight in sorted(zip(anomaly.FEATURE_NAMES, w),
                               key=lambda kv: -abs(kv[1])):
        print(f"    {name:16s} {weight:+8.4f}")
    print(f"    {'(bias)':16s} {b:+8.4f}")
    print()

    rows, allow = scenario_report(w, b, th)
    print("  SCENARIO GATE — hub/anomaly.py's own cases, run BEFORE writing:")
    for label, s, flagged, expect, ok, blocking in rows:
        tag = "PASS" if ok else ("FAIL" if blocking else "changed")
        mark = "  [blocking]" if blocking else ""
        print(f"    {tag:8s} {s:.3f} ({'flag' if flagged else 'quiet'}, "
              f"want {expect}){mark}  {label[:44]}")
    print()

    if not allow and not args.no_gate:
        print("  REFUSING TO WRITE: a blocking scenario regressed.")
        print("  The model on disk is unchanged, so the demo still behaves as")
        print("  rehearsed. Fix the data, or re-run with --no-gate if you have")
        print("  decided the new behaviour is correct.")
        print("=" * 78)
        return 1
    if not allow:
        print("  --no-gate: writing anyway, over a blocking scenario failure.")

    loco_pairs = {k: list(v) for k, v in loco.items()}
    header = f'''"""Generated by tools/train_anomaly.py — DO NOT EDIT BY HAND.

Logistic-regression weights for the edge anomaly detector.

  trained          : {datetime.now().strftime("%Y-%m-%d")}
  training source  : {source}
  calendar days    : {n_days}
  samples          : {len(ds)} ({n_pos} anomalous, {len(ds) - n_pos} normal)
  split            : by day, {len(tr)} train / {len(ho)} holdout
  train accuracy   : {train_acc:.4f}
  holdout accuracy : {ho_acc:.4f}
  holdout precision: {precision:.4f}
  holdout recall   : {recall:.4f}
  unseen-class recall (leave-one-class-out): {caught_total / max(1, n_total):.4f}
  seed             : {args.seed}

*** THE TRAINING DATA IS SIMULATED. ***

The normal class comes from recorded session ticks, but those sessions are
generated by tools/generate_sessions.py, so these figures describe how separable
a synthetic household distribution is. They are NOT evidence of real-world
accuracy, and nothing that displays a score derived from these weights may imply
otherwise. In deployment the same path retrains on real logged history.

QUOTE THE UNSEEN-CLASS NUMBER, NOT THE HOLDOUT NUMBER, when asked whether this
generalises: holdout anomalies have same-class siblings in training, so the
holdout figure measures recognition, not generalisation.
"""

# Order matches anomaly.FEATURE_NAMES exactly.
FEATURE_NAMES = {anomaly.FEATURE_NAMES!r}

WEIGHTS = [
'''
    body = "".join(f"    {v!r},  # {n}\n" for n, v in zip(anomaly.FEATURE_NAMES, w))
    footer = f''']

BIAS = {b!r}

TRAIN_DAYS = {n_days}
TRAIN_SAMPLES = {len(ds)}
TRAIN_ACCURACY = {train_acc!r}
HOLDOUT_ACCURACY = {ho_acc!r}
HOLDOUT_PRECISION = {precision!r}
HOLDOUT_RECALL = {recall!r}
UNSEEN_CLASS_RECALL = {caught_total / max(1, n_total)!r}
LEAVE_ONE_CLASS_OUT = {loco_pairs!r}
ANOMALY_CLASSES = {list(ANOMALY_CLASSES)!r}
TRAINING_SOURCE = {source!r}
TRAINING_DATA_IS_SIMULATED = True
SEED = {args.seed}
'''

    OUT_PATH.write_text(header + body + footer, encoding="utf-8", newline="\n")
    print(f"  wrote {OUT_PATH}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
