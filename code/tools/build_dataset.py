#!/usr/bin/env python3
"""Turn recorded sessions into a labelled dataset — and say honestly how good it is.

Reads the JSONL written by `hub/recorder.py` and emits one row per
recommendation that was shown to a human, with the situation that produced it
and whether they acted on it.

WHERE THE LABELS COME FROM
--------------------------
    apply       1   a human approved it and something physically happened
    feedback    1/0 an explicit thumb on the card — the strongest signal here,
                    because it is a judgement rather than an absence of one
    refusal     0   the R7 guardrail refused an action a human ASKED FOR. A HARD
                    negative: the advice was not merely unwanted, it was unsafe
    veto        --  withheld before anyone saw it. NOT a label — nobody expressed
                    a preference about a card that was never shown, and counting
                    it as a negative would teach the model that people dislike
                    advice they were never given. Audit evidence only.
    (nothing)   0*  shown and never touched. A WEAK negative — it may mean
                    wrong, or badly timed, or nobody was looking at the screen.
                    Emitted with weight 0.25 and marked, so a trainer can drop
                    it. Treating "ignored" as a confident no is how a model
                    learns to stop recommending things that were simply missed.

This file deliberately does NOT train anything. It ends by printing a verdict on
whether there is enough signal to train on at all — because the failure mode
that matters for this project is not a weak model, it is a confident number
quoted from twenty samples.

Run:
    python tools/build_dataset.py
    python tools/build_dataset.py --out data/reco_dataset.csv
    python tools/build_dataset.py --sessions "data/sessions/session-2026*.jsonl"
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SESSION_GLOB = str(ROOT / "data" / "sessions" / "session-*.jsonl")
DEFAULT_OUT = ROOT / "data" / "reco_dataset.csv"

# How long after a card appears an approval still counts as a response to it.
APPROVAL_WINDOW_S = 900

# Minimum positives before a trained number means anything. Not a statistical
# threshold so much as a blush threshold: below this, quoting an accuracy in
# front of judges is not defensible.
MIN_POSITIVES_TO_TRAIN = 30

WEAK_NEGATIVE_WEIGHT = 0.25

FEATURES = [
    "hour", "on_peak", "rate",
    "occupancy", "minutes_unoccupied", "presence_away", "minutes_away",
    "lux", "temp_c", "humidity",
    "total_watts", "load_watts", "load_metered",
    "severity_rank", "usd", "kwh",
    "occupancy_prob", "occupancy_in_domain",
]

SEVERITY_RANK = {"good": 0, "warning": 1, "serious": 2, "critical": 3}


def load_rows(pattern: str) -> List[Dict]:
    files = sorted(glob.glob(pattern))
    if not files:
        return []
    rows: List[Dict] = []
    for f in files:
        for i, line in enumerate(Path(f).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                # A half-written final line is normal — the hub gets Ctrl-C'd.
                print(f"  ! skipped malformed line {f}:{i}")
                continue
            r["_file"] = Path(f).name
            rows.append(r)
    rows.sort(key=lambda r: (r.get("_file", ""), r.get("seq", 0)))
    return rows


def _room_of(tick: Dict, room: Optional[str]) -> Dict:
    rooms = tick.get("rooms") or {}
    if room and room in rooms:
        return rooms[room]
    return next(iter(rooms.values()), {})


def _load_state(tick: Dict, room: Optional[str], rule: str) -> Dict:
    """The load a rule concerns. Mirrors hub/server.py _load_from_rule()."""
    by_rule = {"unoccupied_lights_on": "lights", "daylight_waste": "lights",
               "away_with_hvac_on": "ac", "hvac_with_window_open": "ac",
               "phantom_standby": "standby", "peak_hour_heavy_load": "dryer"}
    name = by_rule.get(rule, "lights")
    return (tick.get("loads") or {}).get(f"{room}/{name}", {})


def featurise(tick: Dict, finding: Dict) -> Dict:
    """Situation + finding -> a flat feature row. Missing values stay empty
    rather than becoming zero: a zero lux reading and an absent one are
    different facts, and only one of them means 'dark'."""
    room_name = finding.get("room")
    room = _room_of(tick, room_name)
    user = tick.get("user") or {}
    tariff = tick.get("tariff") or {}
    load = _load_state(tick, room_name, finding.get("rule_name", ""))
    ts = tick.get("ts") or finding.get("ts") or 0

    clock = tariff.get("clock") or ""
    try:
        hour = int(clock.split(":")[0])
    except Exception:
        hour = datetime.fromtimestamp(ts).hour if ts else ""

    now = tick.get("ts") or 0
    last_occ = room.get("last_occupied_ts")
    user_ts = user.get("ts")

    return {
        "hour": hour,
        "on_peak": 1 if tariff.get("period") == "on_peak" else 0,
        "rate": tariff.get("rate", ""),
        "occupancy": 1 if room.get("occupancy") else 0,
        "minutes_unoccupied": round((now - last_occ) / 60.0, 2)
        if (now and last_occ) else "",
        "presence_away": 1 if user.get("presence") == "away" else 0,
        "minutes_away": round((now - user_ts) / 60.0, 2)
        if (now and user_ts and user.get("presence") == "away") else "",
        "lux": room.get("lux", ""),
        "temp_c": room.get("temp_c", ""),
        "humidity": room.get("humidity", ""),
        "total_watts": tick.get("total_watts", ""),
        "load_watts": load.get("watts", ""),
        "load_metered": 1 if load.get("metered") else 0,
        "severity_rank": SEVERITY_RANK.get(finding.get("severity"), ""),
        "usd": finding.get("usd", ""),
        "kwh": finding.get("kwh", ""),
        # The learned tier's own view, carried through so a later model can be
        # compared against it — or trained to correct it.
        "occupancy_prob": room.get("occupancy_prob", ""),
        "occupancy_in_domain": 1 if room.get("occupancy_in_domain") else 0,
    }


def build(rows: List[Dict]) -> List[Dict]:
    """Join findings to the tick that preceded them and to their outcome."""
    # Outcomes, keyed by reco_id. Explicit feedback beats an inferred label.
    applied: Dict[str, Dict] = {}
    refused: Dict[str, Dict] = {}
    thumbs: Dict[str, Dict] = {}
    for r in rows:
        t = r.get("type")
        if t == "apply":
            applied.setdefault(r.get("reco_id"), r)
        elif t == "refusal":
            refused.setdefault(r.get("reco_id"), r)
        elif t == "feedback":
            thumbs[r.get("reco_id")] = r          # last thumb wins

    out: List[Dict] = []
    last_tick: Optional[Dict] = None
    seen_findings = set()

    for r in rows:
        if r.get("type") == "tick":
            last_tick = r
            continue
        if r.get("type") != "finding":
            continue

        rid = r.get("reco_id")
        # A finding re-narrated after the cooldown is the SAME decision, not a
        # second one. Counting it twice would inflate the dataset with copies.
        key = (r.get("_file"), rid)
        if key in seen_findings:
            continue
        seen_findings.add(key)

        if last_tick is None:
            continue                        # no situation to attribute it to

        fb = thumbs.get(rid)
        ap = applied.get(rid)
        rf = refused.get(rid)

        if fb is not None:
            label, source, weight = (1 if fb.get("useful") else 0), "feedback", 1.0
        elif ap is not None and abs(ap.get("ts", 0) - r.get("ts", 0)) <= APPROVAL_WINDOW_S:
            label, source, weight = 1, "apply", 1.0
        elif rf is not None:
            label, source, weight = 0, "refusal", 1.0
        else:
            label, source, weight = 0, "ignored", WEAK_NEGATIVE_WEIGHT

        row = featurise(last_tick, r)
        row.update({
            "label": label, "label_source": source, "weight": weight,
            "reco_id": rid, "rule_name": r.get("rule_name", ""),
            "room": r.get("room", ""), "narrated_by": r.get("narrated_by", ""),
            "provenance": (last_tick.get("provenance") or {}).get("overall", ""),
            "session": r.get("_file", ""), "ts": r.get("ts", ""),
        })
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=SESSION_GLOB)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    print("\n" + "=" * 78)
    print("  BUILD DATASET — recorded sessions -> labelled rows")
    print("=" * 78 + "\n")

    rows = load_rows(args.sessions)
    if not rows:
        print(f"  no session files matched {args.sessions}")
        print("  Start the hub (recording is on by default) and let it run.\n")
        return 1

    kinds = Counter(r.get("type") for r in rows)
    files = len({r.get("_file") for r in rows})
    print(f"  {len(rows)} rows from {files} session file(s)")
    for k, n in kinds.most_common():
        print(f"    {k:<14} {n}")

    vetoes = kinds.get("veto", 0)
    if vetoes:
        withheld = sum(r.get("usd_withheld", 0.0) for r in rows
                       if r.get("type") == "veto")
        print(f"\n  {vetoes} guardrail veto(es) recorded, ${withheld:.2f} of savings")
        print( "  withheld before anyone saw them — audit evidence, NOT training")
        print( "  labels. Nobody expressed a preference about a card never shown.")

    data = build(rows)
    if not data:
        print("\n  No findings recorded yet — nothing to label.")
        print("  Drive the simulator until recommendations appear, then re-run.\n")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = FEATURES + ["label", "label_source", "weight", "reco_id", "rule_name",
                       "room", "narrated_by", "provenance", "session", "ts"]
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(data)

    pos = sum(1 for d in data if d["label"] == 1)
    neg = len(data) - pos
    by_source = Counter(d["label_source"] for d in data)
    by_rule = Counter(d["rule_name"] for d in data)
    by_prov = Counter(d["provenance"] for d in data)
    strong = sum(1 for d in data if d["weight"] == 1.0)

    print(f"\n  wrote {out}")
    print(f"    rows      {len(data)}   positives {pos}   negatives {neg}")
    print(f"    strong    {strong} (weight 1.0)   weak {len(data)-strong} (ignored)")
    print("    labels by source:  " + ", ".join(f"{k}={v}" for k, v in by_source.most_common()))
    print("    rows by rule:      " + ", ".join(f"{k}={v}" for k, v in by_rule.most_common()))
    print("    data provenance:   " + ", ".join(f"{k}={v}" for k, v in by_prov.most_common()))

    print("\n  " + "-" * 74)
    if pos < MIN_POSITIVES_TO_TRAIN:
        print(f"  VERDICT: NOT ENOUGH TO TRAIN ON YET.")
        print(f"    {pos} positive example(s); {MIN_POSITIVES_TO_TRAIN} is the floor for")
        print(f"    quoting any number from it. The pipeline works — the corpus is thin.")
        print(f"    Say exactly that if asked: it is a better answer than an accuracy")
        print(f"    figure derived from {pos} samples.")
    elif pos < 3 * MIN_POSITIVES_TO_TRAIN:
        print(f"  VERDICT: trainable, but report it as preliminary ({pos} positives).")
        print(f"    Quote the sample count everywhere the accuracy is quoted.")
    else:
        print(f"  VERDICT: trainable ({pos} positives, {neg} negatives).")
    if by_prov and set(by_prov) - {"measured"}:
        share = 100.0 * sum(v for k, v in by_prov.items() if k != "measured") / len(data)
        print(f"\n  NOTE: {share:.0f}% of rows are not pure measurements "
              f"(simulated or synthetic).")
        print( "    That is fine — it is declared. It is not fine to describe a model")
        print( "    trained on it as having learned from a real household.")
    print("  " + "-" * 74 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
