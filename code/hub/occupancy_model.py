"""Occupancy inference — the learned tier, kept on a short leash.

Loads the artifact written by `tools/train_occupancy.py` and answers: given
temperature, humidity and ambient light, is this room occupied? Trained on the
UCI Occupancy Detection benchmark (Candanedo & Feldheim, 2016).

WHAT THIS IS ALLOWED TO DO
--------------------------
The project's credibility rests on a strict hierarchy, and adding a model must
not weaken it:

    deterministic rules + R7   decide what is LEGAL / SAFE   (cannot be overridden)
    the learned model          decides what is LIKELY        (advisory)
    the LLM                    decides WORDING               (no arithmetic)

So this module produces a *probability with its provenance*, and nothing else.
It never touches a dollar figure, never unlocks an action the guardrail refused,
and by default never overwrites a reported sensor value.

MODES — env `OCCUPANCY_MODEL`
    0   off
    1   shadow (default). Predict, record, display alongside the reported value.
        The model's answer is visible and auditable but drives no rule, so a bad
        prediction cannot change a recommendation or an actuation.
    2   fill. As shadow, plus: when NO other source has supplied occupancy for
        the room, the prediction fills it in, stamped `occ_src="model_uci_lr"`.
        It still never overwrites a value someone else reported.

Shadow is the default deliberately. It is also the better demo: showing the
model's call next to the simulator's toggle is a live accuracy read-out, which
says more than a number on a slide.

`OCCUPANCY_VARIANT` selects `full` (temp+humidity+lux) or `no_light`
(temp+humidity). See the trainer's docstring for why the second one exists —
lux is the dominant feature, and R3 already thresholds on lux.

No dependencies. Inference is a dot product over two or three floats.
"""

from __future__ import annotations

import json
import os
import math
from pathlib import Path
from typing import Dict, Optional

MODEL_PATH = Path(os.environ.get(
    "OCCUPANCY_MODEL_PATH",
    str(Path(__file__).resolve().parent / "models" / "occupancy_lr.json")))

MODE = int(os.environ.get("OCCUPANCY_MODEL", "1") or 0)
VARIANT = os.environ.get("OCCUPANCY_VARIANT", "").strip()

# Above/below these the call is reported as confident. Between them the model is
# saying "I don't know", and a shrug should look like a shrug rather than a coin
# flip dressed as a decision.
CONFIDENT_HIGH = 0.70
CONFIDENT_LOW = 0.30


class OccupancyModel:
    """Logistic regression over standardised environmental features."""

    def __init__(self, path: Path = MODEL_PATH, variant: str = VARIANT):
        self.path = path
        self.ok = False
        self.reason = ""
        self.artifact: Dict = {}
        self.variant = ""
        self.features: list = []
        self.weights: list = []
        self.bias = 0.0
        self.scaler: Dict = {}

        try:
            self.artifact = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.reason = (f"no model at {path.name} — run "
                           f"`python tools/train_occupancy.py`")
            return
        except Exception as exc:
            self.reason = f"unreadable model artifact: {exc}"
            return

        name = variant or self.artifact.get("default_variant", "full")
        spec = (self.artifact.get("variants") or {}).get(name)
        if not spec:
            self.reason = f"variant {name!r} not in artifact"
            return

        self.variant = name
        self.features = list(spec["features"])
        self.weights = list(spec["weights"])
        self.bias = float(spec["bias"])
        self.scaler = spec["standardiser"]
        self.ok = True

    # -- introspection, for the dashboard and the audit panel ---------------

    def info(self) -> Dict:
        spec = (self.artifact.get("variants") or {}).get(self.variant, {})
        scores = spec.get("scores", {})
        held_out = [k for k in scores if k.startswith("datatest")]
        return {
            "enabled": bool(self.ok and MODE > 0),
            "mode": MODE,
            "ok": self.ok,
            "reason": self.reason,
            "variant": self.variant,
            "features": self.features,
            "weights": dict(zip(self.features, self.weights)),
            "bias": self.bias,
            "dataset": (self.artifact.get("dataset") or {}).get("name", ""),
            "trained_on": self.artifact.get("trained_on", ""),
            # Quote a HELD-OUT number, never the training accuracy.
            "accuracy": (scores.get(held_out[0], {}) or {}).get("accuracy")
            if held_out else None,
            "held_out_split": held_out[0] if held_out else None,
        }

    # -- inference ----------------------------------------------------------

    def predict(self, temp_c: Optional[float] = None,
                humidity: Optional[float] = None,
                lux: Optional[float] = None) -> Optional[Dict]:
        """Return a prediction dict, or None when it cannot honestly be made.

        None (rather than a default) when a required feature is missing: a room
        with no humidity reading has no occupancy estimate, and inventing one by
        substituting a mean is exactly the class of quiet fabrication this
        project keeps finding and removing.
        """
        if not self.ok or MODE <= 0:
            return None

        supplied = {"temp_c": temp_c, "humidity": humidity, "lux": lux}
        values = {}
        for f in self.features:
            v = supplied.get(f)
            if v is None:
                return None
            try:
                values[f] = float(v)
            except (TypeError, ValueError):
                return None

        z = self.bias
        contributions = {}
        in_domain = True
        for f, w in zip(self.features, self.weights):
            s = self.scaler[f]
            xs = (values[f] - s["mean"]) / s["std"]
            term = w * xs
            contributions[f] = round(term, 4)
            z += term
            # The UCI room was 19-24 C at 16-40% RH. A knob turned to 31 C is
            # far outside anything this model has seen, and it will still emit a
            # confident-looking number. Say so instead.
            if not (s["min"] <= values[f] <= s["max"]):
                in_domain = False

        p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
        confident = p >= CONFIDENT_HIGH or p <= CONFIDENT_LOW

        # The single feature that moved the answer furthest — what the UI shows
        # as "because ...", and how the lux dominance stays visible at runtime
        # rather than only in the training log.
        driver = max(contributions, key=lambda k: abs(contributions[k])) \
            if contributions else None

        return {
            "occupied": bool(p >= 0.5),
            "probability": round(p, 4),
            "confident": confident,
            "in_domain": in_domain,
            "driver": driver,
            "contributions": contributions,
            "variant": self.variant,
            "src": f"model_uci_lr:{self.variant}",
        }


MODEL = OccupancyModel()


def annotate_room(room_state: Dict) -> Dict:
    """Attach a shadow prediction to one room's state, in place.

    Returns the same dict so it can be used inline. Never raises — a model
    failure must not stop a sensor reading from being ingested.
    """
    try:
        if not MODEL.ok or MODE <= 0:
            return room_state
        pred = MODEL.predict(room_state.get("temp_c"),
                             room_state.get("humidity"),
                             room_state.get("lux"))
        if pred is None:
            # Clear any stale prediction rather than leaving the last one to
            # look current next to fresh sensor values.
            for k in ("occupancy_pred", "occupancy_prob", "occupancy_pred_src",
                      "occupancy_driver", "occupancy_in_domain"):
                room_state.pop(k, None)
            return room_state

        room_state["occupancy_pred"] = pred["occupied"]
        room_state["occupancy_prob"] = pred["probability"]
        room_state["occupancy_pred_src"] = pred["src"]
        room_state["occupancy_driver"] = pred["driver"]
        room_state["occupancy_in_domain"] = pred["in_domain"]

        # Mode 2 only FILLS A GAP. If any source reported occupancy, that wins —
        # a measurement is never replaced by an inference.
        if MODE >= 2 and room_state.get("occupancy") is None:
            room_state["occupancy"] = pred["occupied"]
            room_state["occ_src"] = pred["src"]
    except Exception as exc:
        print(f"[occupancy] prediction skipped: {exc}")
    return room_state


# --------------------------------------------------------------------------
# Self-test — replay real held-out rows from the benchmark.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import csv
    import io
    import zipfile

    print("\n" + "=" * 78)
    print("  OCCUPANCY MODEL SELF-TEST")
    print("=" * 78)

    info = MODEL.info()
    if not MODEL.ok:
        print(f"\n  NOT LOADED: {MODEL.reason}\n")
        raise SystemExit(1)

    print(f"\n  variant   : {info['variant']}  ({', '.join(info['features'])})")
    print(f"  dataset   : {info['dataset']}")
    print(f"  held-out  : {info['held_out_split']} acc {info['accuracy']}")
    print(f"  weights   : " + "  ".join(f"{k} {v:+.3f}"
                                        for k, v in info["weights"].items())
          + f"   bias {info['bias']:+.3f}")

    print("\n  --- hand-checkable cases")
    cases = [
        ("bright, warm, humid  (office in use)", 23.7, 26.3, 585.0),
        ("dark, cool, dry      (nobody there)", 20.2, 22.0, 0.0),
        ("dark but warm        (just left?)", 22.5, 27.0, 0.0),
        ("knob at 31 C         (out of domain)", 31.0, 47.0, 110.0),
    ]
    for label, t, h, l in cases:
        p = MODEL.predict(t, h, l)
        flag = "" if p["in_domain"] else "   [EXTRAPOLATING]"
        print(f"    {label:<38} p={p['probability']:.3f} "
              f"-> {'OCCUPIED' if p['occupied'] else 'EMPTY':<8} "
              f"driver={p['driver']}{flag}")

    print("\n  --- missing feature must yield None, never a guess")
    assert MODEL.predict(23.0, None, 500.0) is None
    print("    humidity absent -> None  OK")

    # Replay the official held-out split and confirm the artifact's own claim.
    zp = Path(__file__).resolve().parent.parent / "data" / "uci" / "occupancy-detection.zip"
    if zp.exists():
        with zipfile.ZipFile(zp) as z:
            text = z.read("datatest.txt").decode()
        reader = csv.reader(io.StringIO(text))
        header = next(reader)[-7:]
        hits = n = 0
        for raw in reader:
            if len(raw) < 7:
                continue
            rec = dict(zip(header, raw[-7:]))
            p = MODEL.predict(float(rec["Temperature"]), float(rec["Humidity"]),
                              float(rec["Light"]))
            if p is None:
                continue
            n += 1
            hits += int(p["occupied"] == bool(float(rec["Occupancy"])))
        acc = hits / max(n, 1)
        print(f"\n  --- replay datatest.txt through predict(): "
              f"{hits}/{n} = {acc:.4f}")
        claimed = info["accuracy"]
        print(f"      artifact claims {claimed} — "
              f"{'MATCH' if abs(acc - claimed) < 0.005 else 'MISMATCH'}")
        assert abs(acc - claimed) < 0.005, "inference disagrees with the trainer"
    else:
        print("\n  (corpus not downloaded; skipping replay)")

    print("\n  ALL CHECKS PASSED\n")
