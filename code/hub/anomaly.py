"""Edge anomaly detector — tier 1 of the three-tier AI split.

WHAT THIS IS
    A trained logistic-regression classifier that scores how unusual the current
    home state is, given the rhythm it learned. It is deliberately NOT an LLM.

WHY IT EXISTS
    The seven hand-written rules can only detect what a fixed threshold can
    express. This case is real waste and every rule stays silent:

        3 AM · room occupied · 24 C · A/C running 1 h · user home · off-peak
        -> R1..R6 produce []

    Occupancy is true, temperature is in band, it is off-peak. Nothing trips.
    But it is wildly out of pattern for a household, and a learned model says so.

WHERE IT RUNS
    On the Dragonwing QRB2210 (quad Cortex-A53, CPU only — that board has no
    NPU/DSP stack at all). Hence: pure Python, standard library only, NO numpy
    and no sklearn. The board's ~/energy-venv has none of them and needs none.
    Verified on-board: Python 3.13.5, aarch64, numpy/sklearn/scipy all absent.

    The same file imports unchanged on the X Elite hub, so the score can be
    computed in either place without a second implementation to keep in sync.

HONESTY
    The score is a LEARNED value, not a measurement, and the model is trained on
    SIMULATED household data. Both facts must travel with it wherever it
    surfaces — see `model_provenance()` and the evidence lines in rules.py.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Order is load-bearing: it must match the weight vector emitted by the trainer.
FEATURE_NAMES: List[str] = [
    "hour_sin",        # cyclical time-of-day, so 23:00 and 01:00 are neighbours
    "hour_cos",
    "occupancy",       # 0/1  — is anyone in the room
    "lux_norm",        # lux / 1000, clamped 0..1
    "temp_norm",       # (temp_c - 16) / 16, clamped 0..1
    "watts_norm",      # total drawn watts / 4000, clamped 0..1
    "lights_on",       # 0/1
    "hvac_on",         # 0/1  — a/c or heater
    "presence_away",   # 0/1  — user geofence says away
]

# Above this, the detector emits a finding. Named, documented, tunable.
# Chosen from the holdout distribution in tools/train_anomaly.py: it sits above
# the normal-sample bulk while still catching every injected anomaly class.
ANOMALY_THRESHOLD = 0.70

# Human-readable reasons, used to explain the top contributing feature on stage
# and in the UI. Keyed by feature name.
FEATURE_EXPLANATIONS: Dict[str, str] = {
    "hour_sin": "the time of day",
    "hour_cos": "the time of day",
    "occupancy": "the room being occupied",
    "lux_norm": "the ambient light level",
    "temp_norm": "the indoor temperature",
    "watts_norm": "the total power being drawn",
    "lights_on": "lighting being on",
    "hvac_on": "heating/cooling running at an hour when this home is normally idle",
    "presence_away": "nobody being home",
}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _primary_room(snapshot: Dict) -> Dict:
    """The room to score. Single-room demo, but do not assume the name."""
    rooms = snapshot.get("rooms") or {}
    if not rooms:
        return {}
    if "living" in rooms:
        return rooms["living"] or {}
    return next(iter(rooms.values()), {}) or {}


def featurize(snapshot: Dict, now_dt: Optional[datetime] = None) -> List[float]:
    """Snapshot -> fixed-length feature vector. Pure arithmetic, no deps.

    Every lookup is defensive: this runs on live telemetry where a signal can be
    absent (the MCU emits PARTIAL payloads by design, so the simulator can own
    the rest). A missing signal must degrade the score, never raise.
    """
    if now_dt is None:
        now_dt = datetime.fromtimestamp(snapshot.get("now", 0) or 0)

    room = _primary_room(snapshot)
    loads = snapshot.get("loads") or {}
    user = snapshot.get("user") or {}

    # Cyclical encoding: midnight and 23:00 must be adjacent, which a raw hour
    # integer gets badly wrong (0 vs 23 looks maximally distant).
    hour = now_dt.hour + now_dt.minute / 60.0
    angle = 2.0 * math.pi * hour / 24.0

    total_watts = 0.0
    lights_on = 0.0
    hvac_on = 0.0
    for key, load in loads.items():
        if not isinstance(load, dict):
            continue
        if load.get("state") != "on":
            continue
        try:
            total_watts += float(load.get("watts") or 0.0)
        except (TypeError, ValueError):
            pass
        name = str(key).split("/")[-1].lower()
        if "light" in name:
            lights_on = 1.0
        if name in ("ac", "heater", "hvac"):
            hvac_on = 1.0

    def _num(v, default=0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return [
        math.sin(angle),
        math.cos(angle),
        1.0 if room.get("occupancy") else 0.0,
        _clamp(_num(room.get("lux")) / 1000.0),
        _clamp((_num(room.get("temp_c"), 21.0) - 16.0) / 16.0),
        _clamp(total_watts / 4000.0),
        lights_on,
        hvac_on,
        1.0 if str(user.get("presence", "")).lower() == "away" else 0.0,
    ]


def _sigmoid(z: float) -> float:
    # Split on the sign to avoid overflow in exp() for large |z|.
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _load_model():
    """Import the generated weights. Kept lazy so this module imports even
    before the trainer has ever been run (rules.py logs and carries on)."""
    try:
        from . import anomaly_model  # type: ignore
    except ImportError:
        import anomaly_model  # type: ignore
    return anomaly_model


def score(features: List[float]) -> Tuple[float, str]:
    """Return (anomaly_score 0..1, top_contributing_feature_name).

    p = sigmoid(w . x + b).

    The top contributor is the feature with the largest POSITIVE contribution
    w_i * x_i — i.e. what pushed this sample TOWARD anomalous.

    Deliberately not the largest |w_i * x_i|. On the motivating 3 AM case the
    biggest-magnitude term is `occupancy` (weight -3.78), but that term is what
    makes the sample look NORMAL; naming it as "the strongest signal" for an
    anomaly inverts the explanation and reads as a bug on stage. Taking the
    largest positive term instead yields `hvac_on` — "heating/cooling running at
    an hour when this home is normally idle" — which is the actual reason.

    Falls back to largest magnitude only if nothing pushed positive, which
    cannot happen for a score above the threshold but keeps the function total.
    """
    m = _load_model()
    weights = m.WEIGHTS
    bias = m.BIAS

    if len(features) != len(weights):
        raise ValueError(
            f"feature/weight length mismatch: {len(features)} vs {len(weights)} "
            "— retrain with tools/train_anomaly.py"
        )

    z = bias
    best_pos_i, best_pos = -1, 0.0
    best_mag_i, best_mag = 0, -1.0
    for i, (w, x) in enumerate(zip(weights, features)):
        contrib = w * x
        z += contrib
        if contrib > best_pos:
            best_pos, best_pos_i = contrib, i
        if abs(contrib) > best_mag:
            best_mag, best_mag_i = abs(contrib), i

    top = best_pos_i if best_pos_i >= 0 else best_mag_i
    return _sigmoid(z), FEATURE_NAMES[top]


def explain(feature_name: str) -> str:
    """Plain-English reason for a feature name, for evidence lines and the UI."""
    return FEATURE_EXPLANATIONS.get(feature_name, feature_name)


def model_provenance() -> str:
    """One line stating what the model is and that its training data is
    simulated. Anything that displays a score must display this too."""
    try:
        m = _load_model()
        return (f"logistic regression, {len(m.WEIGHTS)} features, trained on "
                f"{m.TRAIN_DAYS} SIMULATED days (holdout acc {m.HOLDOUT_ACCURACY:.2f})")
    except Exception:
        return "logistic regression (model not yet trained)"


def is_available() -> bool:
    try:
        _load_model()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Self-test — five hand-built snapshots. Run: python hub/anomaly.py
# --------------------------------------------------------------------------

def _snap(hour, occupancy, lux, temp_c, loads, presence):
    return (
        {"rooms": {"living": {"occupancy": occupancy, "lux": lux,
                              "temp_c": temp_c, "humidity": 50}},
         "loads": loads, "user": {"presence": presence}, "now": 0},
        datetime(2026, 8, 6, hour, 0),
    )


SCENARIOS = [
    ("3 AM, A/C running, someone home     [THE MOTIVATING CASE]",
     _snap(3, True, 0, 24.0, {"living/ac": {"state": "on", "watts": 1100}}, "home"),
     "high",
     "R1-R6 produce NOTHING here: occupancy true, temp in band, off-peak."),
    ("All loads on, nobody home, midday",
     _snap(13, False, 700, 24.5,
           {"living/lights": {"state": "on", "watts": 240},
            "living/ac": {"state": "on", "watts": 1100},
            "living/dryer": {"state": "on", "watts": 2400}}, "away"),
     "high",
     "Rules also catch this — agreement is the expected case, not the interesting one."),
    ("Dryer at 2 AM, nobody home",
     _snap(2, False, 0, 21.0,
           {"living/dryer": {"state": "on", "watts": 2400}}, "away"),
     "high",
     "Out of rhythm on both time and occupancy."),
    ("Normal evening: occupied, lights on, 21 C",
     _snap(20, True, 120, 21.0,
           {"living/lights": {"state": "on", "watts": 240}}, "home"),
     "low",
     "The single most common real state. A false positive here would be fatal."),
    ("Empty house, everything off, midday",
     _snap(11, False, 700, 22.0, {}, "away"),
     "low",
     "Away and bright is a normal workday, not an anomaly."),
    # KNOWN BOUNDARY, deliberately kept in the suite rather than hidden.
    # This differs from a normal workday (away, bright, nothing on) only by a
    # single 240 W load, so the learned model scores it ~0.63 — under threshold.
    # It is NOT a gap in coverage: R3 daylight_waste catches it deterministically
    # and always will. Documented because the honest claim is "the two tiers cover
    # different ground", not "the model catches everything". Raising the threshold
    # to capture this would cost false positives on ordinary weekdays.
    ("Lights in bright daylight, nobody home   [R3's territory, not the model's]",
     _snap(13, False, 900, 23.0,
           {"living/lights": {"state": "on", "watts": 240}}, "away"),
     "low",
     "Deterministic rule R3 owns this; the model is not required to duplicate it."),
]


if __name__ == "__main__":
    print("=" * 78)
    print("EDGE ANOMALY DETECTOR — self-test")
    print(f"model: {model_provenance()}")
    print(f"threshold: {ANOMALY_THRESHOLD}")
    print("=" * 78)

    if not is_available():
        print("\nNo trained model found. Run:  python tools/train_anomaly.py")
        raise SystemExit(1)

    failures = 0
    for label, (snap, dt), expect, note in SCENARIOS:
        feats = featurize(snap, dt)
        s, top = score(feats)
        flagged = s >= ANOMALY_THRESHOLD
        ok = (expect == "high" and flagged) or (expect == "low" and not flagged)
        failures += 0 if ok else 1
        print(f"\n{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      score {s:.3f}  ({'ANOMALOUS' if flagged else 'normal'}, "
              f"expected {expect})")
        print(f"      strongest signal: {top} — {explain(top)}")
        print(f"      {note}")

    print("\n" + "=" * 78)
    print(f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios behaved as expected")
    print("=" * 78)
    raise SystemExit(1 if failures else 0)
