"""Session recorder — turns the running system into a dataset.

Until now nothing persisted. `power_history` is a 60-entry deque (five minutes)
and every other piece of state died with the process, so the system could not
learn from its own operation and there was no record of what it decided or why.

This module appends one JSON object per line to a session file. Two jobs:

  1. **Audit trail.** Every finding, every approval, every refusal, every
     hardware confirmation, in order, with the state that produced it. That is
     the same claim the `formula` string makes for arithmetic, extended to
     decisions.
  2. **Training substrate.** `tools/build_dataset.py` reads these files and
     emits features + labels. The labels come from the human: an approval is a
     positive, a guardrail refusal is a hard negative, a card that was shown and
     never acted on is a weak negative.

DESIGN RULES

*Never take the hub down.* Recording is strictly a side effect. Every public
function swallows its own exceptions, and after REPEATED failures the recorder
disables itself rather than printing on every tick. A dataset is worth less than
a working demo.

*Never launder provenance.* Rows carry the `*_src` stamps and `metered` flags
verbatim, plus a `provenance` summary computed from them. A model trained on
this data must be able to say what fraction of it was measured, declared
simulation, or synthetic — the same standard the rest of the project holds for
values on screen. See the `fake_lines()` note in arduino/uno_q_publisher.py for
what happens when a generated value inherits a measured value's label.

*Local only.* This file never leaves the machine. `cloud_report.py` sends a
computed digest, never these rows — the project claims occupancy data stays in
the house, and that has to remain true.

Env:
    RECORD_ENABLED=0    turn recording off entirely (default on)
    RECORD_DIR=<path>   where session files land (default code/data/sessions)
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

RECORD_ENABLED = os.environ.get("RECORD_ENABLED", "1") not in ("0", "false", "False")

ROOT = Path(__file__).resolve().parent.parent
RECORD_DIR = Path(os.environ.get("RECORD_DIR", str(ROOT / "data" / "sessions")))

# After this many consecutive write failures, stop trying. A full disk or a
# permissions problem must not produce one line of noise per evaluation tick for
# the rest of the demo.
MAX_CONSECUTIVE_FAILURES = 5

# Sensor source stamps that represent a real measurement from real silicon.
# Everything else is a declared simulation or an outright generated value.
# Keep in sync with arduino/sketch/sketch.ino, which writes these.
MEASURED_SRCS = {"hs3003", "ltr381", "tof"}
SIMULATED_SRCS = {"knob_sim", "button_override", "simulator"}
SYNTHETIC_SRCS = {"synthetic"}


def _classify_src(src: Optional[str]) -> str:
    """One sensor stamp -> measured | simulated | synthetic | unknown."""
    if not src or src == "none":
        # No stamp at all means the phone simulator POSTed it to /api/sensor.
        # That is a declared simulation, not an unknown quantity.
        return "simulated"
    if src in MEASURED_SRCS:
        return "measured"
    if src in SYNTHETIC_SRCS:
        return "synthetic"
    if src in SIMULATED_SRCS:
        return "simulated"
    if src.endswith("_bad_read"):
        return "unknown"
    # An unrecognised stamp is NOT assumed to be good. A future sensor added to
    # the firmware without updating MEASURED_SRCS should show up as unknown in
    # the dataset rather than silently counting as a measurement.
    return "unknown"


def summarize_provenance(rooms: Dict, loads: Dict) -> Dict:
    """Per-row provenance summary, so a trainer can filter or weight by it.

    Returns the classification of each sensor signal plus how many loads
    reported metered (measured) versus modelled power.
    """
    signals: Dict[str, str] = {}
    for room, rs in (rooms or {}).items():
        for key, stamp in (("temp_c", "temp_src"), ("lux", "lux_src"),
                           ("humidity", "hum_src"), ("occupancy", "occ_src")):
            if key in rs:
                signals[f"{room}.{key}"] = _classify_src(rs.get(stamp))

    metered = sum(1 for l in (loads or {}).values() if l.get("metered"))
    kinds = set(signals.values())
    if not kinds:
        overall = "empty"
    elif kinds == {"measured"}:
        overall = "measured"
    elif "synthetic" in kinds:
        overall = "contains_synthetic"
    elif kinds == {"simulated"}:
        overall = "simulated"
    else:
        overall = "mixed"

    return {"overall": overall, "signals": signals,
            "loads_metered": metered, "loads_total": len(loads or {})}


class SessionRecorder:
    """Append-only JSONL writer. One file per hub run."""

    def __init__(self, directory: Path = RECORD_DIR, enabled: bool = RECORD_ENABLED):
        self.enabled = enabled
        self.directory = directory
        # The random suffix is not decoration. Two recorders created in the same
        # second would otherwise share a file and interleave two independent
        # `seq` counters into it, which quietly corrupts any join done
        # downstream. That is easy to hit — importing this module gives you the
        # singleton, and a test that also builds its own instance collides with
        # it inside the same process, so a PID would not be enough either.
        self.run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.path = directory / f"session-{self.run_id}.jsonl"
        self.seq = 0
        self.counts: Dict[str, int] = {}
        self.failures = 0
        self.lock = threading.Lock()
        self.started_ts = time.time()

        if not self.enabled:
            print("[record] disabled (RECORD_ENABLED=0)")
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._write({"type": "session_start", "run_id": self.run_id,
                         "started_at": datetime.now().isoformat(timespec="seconds")})
            print(f"[record] session -> {self.path}")
        except Exception as exc:
            self.enabled = False
            print(f"[record] could not open {self.path} ({exc}) — recording off")

    # -- internals ---------------------------------------------------------

    def _write(self, row: Dict) -> None:
        """Append one row. Caller holds no lock; this takes it."""
        if not self.enabled:
            return
        with self.lock:
            self.seq += 1
            row = {"seq": self.seq, "ts": time.time(), "run_id": self.run_id, **row}
            try:
                # Open/close per write and flush: a demo machine gets killed with
                # Ctrl-C or closed lid, and a buffered tail would lose exactly the
                # rows describing whatever just went wrong.
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, default=str) + "\n")
                self.failures = 0
                self.counts[row["type"]] = self.counts.get(row["type"], 0) + 1
            except Exception as exc:
                self.failures += 1
                if self.failures <= 2:
                    print(f"[record] write failed: {exc}")
                if self.failures >= MAX_CONSECUTIVE_FAILURES:
                    self.enabled = False
                    print("[record] too many write failures — recording off "
                          "(the hub is unaffected)")

    # -- public API — every one of these is best-effort ---------------------

    def tick(self, state: Dict) -> None:
        """One row per evaluation tick: the fused situation the rules just saw.

        Stores the state pieces a model can learn from and drops the ones it
        cannot: `power_history` is redundant (it is the last 60 ticks, which are
        already rows of their own) and `recos` are recorded separately as
        `finding` rows so a card is not duplicated on every tick it survives.
        """
        try:
            rooms = state.get("rooms", {})
            loads = state.get("loads", {})
            self._write({
                "type": "tick",
                "rooms": rooms,
                "loads": loads,
                "user": state.get("user", {}),
                "total_watts": state.get("total_watts", 0.0),
                "tariff": state.get("tariff", {}),
                "mqtt_connected": state.get("mqtt_connected", False),
                "open_reco_ids": [r.get("id") for r in state.get("recos", [])],
                "provenance": summarize_provenance(rooms, loads),
            })
        except Exception:
            pass

    def finding(self, rec, is_new: bool = True) -> None:
        """A recommendation was produced and shown to the user."""
        try:
            self._write({
                "type": "finding",
                "reco_id": getattr(rec, "id", None),
                "rule_name": getattr(rec, "rule_name", None),
                "severity": getattr(rec, "severity", None),
                "room": getattr(rec, "room", None),
                "usd": getattr(rec, "usd", 0.0),
                "kwh": getattr(rec, "kwh", 0.0),
                "co2_kg": getattr(rec, "co2_kg", 0.0),
                "narrated_by": getattr(rec, "narrated_by", None),
                "title": getattr(rec, "title", None),
                "is_new": is_new,
            })
        except Exception:
            pass

    def apply(self, reco_id: str, load_key: str, action: str, approved_by: str,
              realized_usd: float, published: bool) -> None:
        """THE POSITIVE LABEL. A human looked at this advice and acted on it."""
        try:
            self._write({"type": "apply", "reco_id": reco_id, "load_key": load_key,
                         "action": action, "approved_by": approved_by,
                         "realized_usd": realized_usd, "published": published})
        except Exception:
            pass

    def refusal(self, reco_id: str, load_key: str, action: str, reason: str,
                gate: str) -> None:
        """THE HARD NEGATIVE. The guardrail refused an action a human ASKED FOR."""
        try:
            self._write({"type": "refusal", "reco_id": reco_id, "load_key": load_key,
                         "action": action, "reason": reason, "gate": gate})
        except Exception:
            pass

    def veto(self, reco_id: str, load_key: str, reason: str, gate: str,
             usd: float = 0.0) -> None:
        """A finding the guardrail withheld before anyone saw it.

        Deliberately NOT a `refusal` row, though both come from R7. A refusal is
        a human asking and being told no — a genuine preference signal. A veto is
        advice that was never offered, so nobody expressed anything about it, and
        scoring it as a negative would teach a relevance model that people
        dislike recommendations they were never shown. Audit evidence, not a
        label; `tools/build_dataset.py` reads it as such.
        """
        try:
            self._write({"type": "veto", "reco_id": reco_id, "load_key": load_key,
                         "reason": reason, "gate": gate, "usd_withheld": usd})
        except Exception:
            pass

    def actuation(self, load_key: str, payload: Dict) -> None:
        """What the hardware reported back — the ground truth for whether it worked."""
        try:
            self._write({"type": "actuation", "load_key": load_key,
                         "state": payload.get("state"), "source": payload.get("source"),
                         "reco_id": payload.get("reco_id"), "ok": payload.get("ok")})
        except Exception:
            pass

    def feedback(self, reco_id: str, useful: bool, note: str = "",
                 source: str = "dashboard") -> None:
        """EXPLICIT LABEL from the dashboard thumbs. Stronger than an ignored card:
        'not useful' is a deliberate negative rather than an absence of action."""
        try:
            self._write({"type": "feedback", "reco_id": reco_id, "useful": bool(useful),
                         "note": note[:200], "source": source})
        except Exception:
            pass

    def stats(self) -> Dict:
        """Live counters — the dashboard renders these so data collection is
        visible while it happens rather than being an invisible side effect."""
        with self.lock:
            return {
                "enabled": self.enabled,
                "run_id": self.run_id,
                "path": str(self.path.name),
                "rows": self.seq,
                "ticks": self.counts.get("tick", 0),
                "findings": self.counts.get("finding", 0),
                "approvals": self.counts.get("apply", 0),
                "refusals": self.counts.get("refusal", 0),
                "feedback": self.counts.get("feedback", 0),
                "minutes": round((time.time() - self.started_ts) / 60.0, 1),
            }


# Module-level singleton — the hub has exactly one session.
RECORDER = SessionRecorder()


# --------------------------------------------------------------------------
# Self-test — writes a throwaway session and reads it back.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "sessions"
    r = SessionRecorder(directory=tmp)

    state = {
        "rooms": {"living": {"occupancy": False, "lux": 620, "temp_c": 29.5,
                             "humidity": 47, "temp_src": "knob_sim",
                             "lux_src": "none", "occ_src": "none"}},
        "loads": {"living/lights": {"state": "on", "watts": 1.7, "metered": True},
                  "living/ac": {"state": "on", "watts": 1100, "metered": False}},
        "user": {"presence": "away", "distance_m": 2400},
        "total_watts": 1101.7,
        "tariff": {"rate": 0.58, "period": "on_peak", "clock": "18:30"},
        "recos": [{"id": "r2-living-ac"}],
    }

    class _Rec:
        id, rule_name, severity, room = "r2-living-ac", "away_with_hvac_on", "critical", "living"
        usd, kwh, co2_kg = 1.276, 2.2, 0.55
        narrated_by, title = "template", "Cooling an empty home"

    r.tick(state)
    r.finding(_Rec())
    r.refusal("r2-living-ac", "living/ac", "off",
              "Refused: living is 29.5C, above the 27C comfort limit", "comfort_guardrail")
    r.apply("r2-living-ac", "living/ac", "off", "phone", 1.276, True)
    r.actuation("living/ac", {"state": "off", "source": "kasa", "ok": True,
                              "reco_id": "r2-living-ac"})
    r.feedback("r2-living-ac", True, "correct call")

    print("\n" + "=" * 74)
    print("RECORDER SELF-TEST")
    print("=" * 74)
    rows = [json.loads(l) for l in r.path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        extra = ""
        if row["type"] == "tick":
            extra = f"  provenance={row['provenance']['overall']} " \
                    f"metered={row['provenance']['loads_metered']}/{row['provenance']['loads_total']}"
        print(f"  seq={row['seq']:<3} {row['type']:<14}{extra}")

    assert [x["type"] for x in rows] == ["session_start", "tick", "finding", "refusal",
                                         "apply", "actuation", "feedback"]
    assert rows[1]["provenance"]["overall"] == "simulated"
    assert rows[1]["provenance"]["loads_metered"] == 1

    print(f"\n  {len(rows)} rows -> {r.path}")
    print(f"  stats: {json.dumps(r.stats())}")

    # A recorder that cannot write must degrade, not raise. Point it at a path
    # whose parent is a FILE, which fails on every platform — an absolute path
    # to a bogus drive does not: on Windows "/nope" resolves against the current
    # drive and gets happily created.
    blocker = Path(tempfile.mkdtemp()) / "iam-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    broken = SessionRecorder(directory=blocker / "sessions")
    broken.tick(state)
    print(f"  unwritable directory -> enabled={broken.enabled} (must be False)")
    assert broken.enabled is False
    print("\n  ALL CHECKS PASSED\n")
