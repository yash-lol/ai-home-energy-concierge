"""Deterministic waste detection.

Pure logic, no LLM, no I/O. Given a fused state snapshot, return findings. Keeping
this deterministic is what makes the system explainable: every recommendation
traces to a named rule, a named threshold, and a list of triggering facts.

Rules R1-R6 ADD findings. R7 is a comfort guardrail that REMOVES them — the
system is not allowed to recommend something that would make the home
uncomfortable, which is the difference between an assistant and a thermostat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from energy_model import WasteEstimate, waste_estimate

# --------------------------------------------------------------------------
# Thresholds — every one named, no magic numbers inline
# --------------------------------------------------------------------------

UNOCCUPIED_GRACE_S = 600          # 10 min unoccupied before lights count as wasted
AWAY_HVAC_GRACE_S = 300           # 5 min away before HVAC counts as wasted
DAYLIGHT_LUX_THRESHOLD = 300      # above this, artificial light is redundant
PHANTOM_AWAY_S = 7200             # 2 h away before standby draw is worth flagging
PEAK_DEFERRABLE_MIN_WATTS = 1000  # only flag genuinely heavy deferrable loads
HVAC_STALL_TEMP_C = 0.3           # temp must fall at least this much per AC runtime window
HVAC_STALL_MIN_RUNTIME_S = 900    # 15 min of AC runtime before judging effectiveness
HVAC_STALL_HUMIDITY_PCT = 60      # high humidity + no temp drop suggests outside air

COMFORT_MAX_C = 27.0              # never recommend cutting cooling above this
COMFORT_MIN_C = 16.0              # never recommend cutting heating below this

# Upper bound on the duration any single finding may charge for.
#
# Protects against clock discontinuities (the demo simulator jumps the virtual
# clock to "next morning", and a stale `on_since` would otherwise be billed as
# many hours of waste). Also honest: we only claim what we plausibly observed.
MAX_CHARGEABLE_S = 4 * 3600

# Loads that can be time-shifted without affecting comfort.
DEFERRABLE_LOADS = {"clothes_dryer", "dishwasher"}

# Map a load key in the snapshot to an energy_model LOADS key.
LOAD_KEY_MAP = {
    "lights": "incandescent_set",
    "ac": "window_ac",
    "heater": "space_heater",
    "tv": "tv_65",
    "pc": "desktop_pc",
    "dryer": "clothes_dryer",
    "dishwasher": "dishwasher",
    "standby": "standby_phantom",
}

HVAC_LOADS = {"ac", "heater"}


def _fmt_hour(t) -> str:
    """Format a time as '4PM' — platform-independent (no %-I, which fails on Windows)."""
    h = t.hour % 12 or 12
    return f"{h}{'AM' if t.hour < 12 else 'PM'}"


@dataclass
class Finding:
    """One detected waste condition, with its evidence and its cost."""

    id: str
    rule_name: str
    severity: str                      # critical | serious | warning | good
    room: str
    load_key: str
    seconds_wasted: float
    headline: str                      # factual, no LLM flourish
    evidence: List[str] = field(default_factory=list)
    suggested_actions: List[str] = field(default_factory=list)
    estimate: Optional[WasteEstimate] = None

    # Set when a guardrail vetoed this finding. The finding is still computed and
    # still costed — it is simply not offered. Keeping it (rather than dropping
    # it on the floor) is what lets the UI show the decisions the system
    # CONSIDERED AND REJECTED, which is the visible half of having judgement.
    suppressed_by: str = ""            # "" = live advice; else the rule that vetoed
    suppressed_reason: str = ""

    @property
    def usd(self) -> float:
        return self.estimate.usd if self.estimate else 0.0

    @property
    def suppressed(self) -> bool:
        return bool(self.suppressed_by)


def _model_key(load_name: str) -> str:
    """Translate a snapshot load name to an energy_model LOADS key."""
    return LOAD_KEY_MAP.get(load_name, "standby_phantom")


def _attach(finding: Finding, now_dt: datetime) -> Finding:
    """Attach a deterministic, auditable estimate to a finding.

    Duration is clamped to MAX_CHARGEABLE_S and to non-negative, so a clock jump
    or a stale timestamp can never manufacture a huge dollar figure.
    """
    finding.seconds_wasted = max(0.0, min(float(finding.seconds_wasted), MAX_CHARGEABLE_S))
    finding.estimate = waste_estimate(
        _model_key(finding.load_key.split("/")[-1]),
        finding.seconds_wasted,
        now_dt,
    )
    return finding


def _loads_in_room(snapshot: Dict, room: str) -> Dict[str, Dict]:
    """Return {load_name: load_state} for one room."""
    out = {}
    for key, val in snapshot.get("loads", {}).items():
        if "/" in key:
            r, name = key.split("/", 1)
            if r == room:
                out[name] = val
    return out


# --------------------------------------------------------------------------
# R1 — lights on in an unoccupied room
# --------------------------------------------------------------------------

def r1_unoccupied_lights_on(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    findings = []
    now = snapshot["now"]
    for room, rs in snapshot.get("rooms", {}).items():
        if rs.get("occupancy"):
            continue
        unoccupied_s = now - rs.get("last_occupied_ts", now)
        if unoccupied_s < UNOCCUPIED_GRACE_S:
            continue
        for name, load in _loads_in_room(snapshot, room).items():
            if name != "lights" or load.get("state") != "on":
                continue
            findings.append(_attach(Finding(
                id=f"r1-{room}-lights",
                rule_name="unoccupied_lights_on",
                severity="serious",
                room=room,
                load_key=f"{room}/lights",
                seconds_wasted=unoccupied_s,
                headline=f"Lights on in an empty {room} room for {unoccupied_s/60:.0f} min",
                evidence=[
                    f"No motion detected in the {room} room for {unoccupied_s/60:.0f} minutes",
                    f"Lights reported ON, drawing {load.get('watts', 0):.0f} W",
                    f"Grace period is {UNOCCUPIED_GRACE_S/60:.0f} minutes, exceeded",
                ],
                suggested_actions=["Turn off the lights", "Enable a 10-minute motion timer"],
            ), now_dt))
    return findings


# --------------------------------------------------------------------------
# R2 — HVAC running while the user is away
# --------------------------------------------------------------------------

def r2_away_with_hvac_on(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    findings = []
    user = snapshot.get("user", {})
    if user.get("presence") != "away":
        return findings

    now = snapshot["now"]
    away_s = now - user.get("ts", now)
    away_s = max(away_s, AWAY_HVAC_GRACE_S)  # presence ts is the transition moment

    for room in snapshot.get("rooms", {}):
        for name, load in _loads_in_room(snapshot, room).items():
            if name not in HVAC_LOADS or load.get("state") != "on":
                continue
            findings.append(_attach(Finding(
                id=f"r2-{room}-{name}",
                rule_name="away_with_hvac_on",
                severity="critical",
                room=room,
                load_key=f"{room}/{name}",
                seconds_wasted=away_s,
                headline=f"{name.upper()} cooling an empty home — you are {user.get('distance_m', 0)} m away",
                evidence=[
                    f"Phone reports presence AWAY, {user.get('distance_m', 0)} m from home",
                    f"{name.upper()} reported ON at {load.get('watts', 0):.0f} W",
                    f"Running unattended for {away_s/60:.0f} minutes",
                ],
                suggested_actions=[f"Turn off the {name}", "Set an away temperature", "Link HVAC to geofence"],
            ), now_dt))
    return findings


# --------------------------------------------------------------------------
# R3 — artificial light redundant in daylight
# --------------------------------------------------------------------------

def r3_daylight_waste(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    findings = []
    for room, rs in snapshot.get("rooms", {}).items():
        lux = rs.get("lux", 0)
        if lux <= DAYLIGHT_LUX_THRESHOLD:
            continue
        for name, load in _loads_in_room(snapshot, room).items():
            if name != "lights" or load.get("state") != "on":
                continue
            # Charge only since the last sensor update — conservative.
            dur = max(rs.get("ts", snapshot["now"]) - rs.get("lux_high_since", rs.get("ts", snapshot["now"])), 60)
            findings.append(_attach(Finding(
                id=f"r3-{room}-daylight",
                rule_name="daylight_waste",
                severity="warning",
                room=room,
                load_key=f"{room}/lights",
                seconds_wasted=dur,
                headline=f"Daylight is bright enough — {room} lights are redundant",
                evidence=[
                    f"Ambient light reported as {lux} lux, above the {DAYLIGHT_LUX_THRESHOLD} lux daylight threshold",
                    f"Lights still ON at {load.get('watts', 0):.0f} W"
                    + (" (measured by the smart plug)" if load.get("metered") else " (modelled)"),
                    # Say where the number came from rather than assert a sensor
                    # we may not have. lux_src is stamped by the MCU firmware
                    # ("ltr381" = real light sensor, "knob_sim" = declared
                    # simulation); absent means the phone simulator supplied it.
                    f"Lux is a threshold input, not a calibrated measurement "
                    f"(source: {rs.get('lux_src', 'simulator')})",
                ],
                suggested_actions=["Turn off the lights", "Open the blinds instead"],
            ), now_dt))
    return findings


# --------------------------------------------------------------------------
# R4 — HVAC fighting an open window (heuristic)
# --------------------------------------------------------------------------

def r4_hvac_with_window_open(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    findings = []
    for room, rs in snapshot.get("rooms", {}).items():
        loads = _loads_in_room(snapshot, room)
        ac = loads.get("ac")
        if not ac or ac.get("state") != "on":
            continue

        runtime_s = snapshot["now"] - ac.get("on_since", snapshot["now"])
        if runtime_s < HVAC_STALL_MIN_RUNTIME_S:
            continue

        temp_drop = rs.get("temp_drop_c", 0.0)
        humidity = rs.get("humidity", 0)
        if temp_drop >= HVAC_STALL_TEMP_C or humidity < HVAC_STALL_HUMIDITY_PCT:
            continue

        findings.append(_attach(Finding(
            id=f"r4-{room}-stall",
            rule_name="hvac_with_window_open",
            severity="serious",
            room=room,
            load_key=f"{room}/ac",
            seconds_wasted=runtime_s,
            headline=f"A/C running {runtime_s/60:.0f} min with no temperature drop — likely an open window",
            evidence=[
                f"A/C has run {runtime_s/60:.0f} minutes continuously",
                f"Room temperature fell only {temp_drop:.1f} C (expected at least {HVAC_STALL_TEMP_C} C)",
                f"Humidity is {humidity}%, above the {HVAC_STALL_HUMIDITY_PCT}% threshold, consistent with outside air",
                "HEURISTIC: we infer an open window, we do not sense it directly",
            ],
            suggested_actions=["Check for open windows or doors", "Then restart the A/C"],
        ), now_dt))
    return findings


# --------------------------------------------------------------------------
# R5 — phantom standby draw during a long absence
# --------------------------------------------------------------------------

def r5_phantom_standby(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    findings = []
    user = snapshot.get("user", {})
    if user.get("presence") != "away":
        return findings

    away_s = snapshot["now"] - user.get("ts", snapshot["now"])
    if away_s < PHANTOM_AWAY_S:
        return findings

    for room in snapshot.get("rooms", {}):
        for name, load in _loads_in_room(snapshot, room).items():
            if name != "standby" or load.get("state") != "on":
                continue
            findings.append(_attach(Finding(
                id=f"r5-{room}-standby",
                rule_name="phantom_standby",
                severity="warning",
                room=room,
                load_key=f"{room}/standby",
                seconds_wasted=away_s,
                headline=f"Phantom loads drawing power for {away_s/3600:.1f} h while you are out",
                evidence=[
                    f"User away {away_s/3600:.1f} hours, past the {PHANTOM_AWAY_S/3600:.0f} h threshold",
                    f"Standby draw measured at {load.get('watts', 0):.0f} W",
                    "Small per-hour cost, but it runs continuously — worth annualizing",
                ],
                suggested_actions=["Use a switched power strip", "Unplug idle chargers"],
            ), now_dt))
    return findings


# --------------------------------------------------------------------------
# R6 — heavy deferrable load during the on-peak window
# --------------------------------------------------------------------------

def r6_peak_hour_heavy_load(snapshot: Dict, now_dt: datetime) -> List[Finding]:
    from energy_model import (OFF_PEAK_USD_PER_KWH, ON_PEAK_END, ON_PEAK_START,
                              rate_at)

    findings = []
    rate, period = rate_at(now_dt)
    if period != "on_peak":
        return findings

    for room in snapshot.get("rooms", {}):
        for name, load in _loads_in_room(snapshot, room).items():
            if load.get("state") != "on":
                continue
            if _model_key(name) not in DEFERRABLE_LOADS:
                continue
            if load.get("watts", 0) < PEAK_DEFERRABLE_MIN_WATTS:
                continue

            runtime_s = max(snapshot["now"] - load.get("on_since", snapshot["now"]), 60)
            f = _attach(Finding(
                id=f"r6-{room}-{name}",
                rule_name="peak_hour_heavy_load",
                severity="warning",
                room=room,
                load_key=f"{room}/{name}",
                seconds_wasted=runtime_s,
                headline=f"{name.title()} is running during the {_fmt_hour(ON_PEAK_START)}-{_fmt_hour(ON_PEAK_END)} peak window",
                evidence=[
                    f"Current rate is ${rate:.2f}/kWh (on-peak) versus ${OFF_PEAK_USD_PER_KWH:.2f}/kWh off-peak",
                    f"{name.title()} drawing {load.get('watts', 0):.0f} W for {runtime_s/60:.0f} min",
                    f"On-peak window is {ON_PEAK_START.strftime('%H:%M')}-{ON_PEAK_END.strftime('%H:%M')} daily",
                ],
                suggested_actions=["Delay this cycle until 9 PM", "Use the delay-start timer"],
            ), now_dt)

            # Reframe: the waste is the RATE DELTA, not the whole energy cost.
            # The load itself is legitimate — only its timing is wasteful.
            delta = f.estimate.kwh * (rate - OFF_PEAK_USD_PER_KWH)
            f.estimate.usd = delta
            f.estimate.formula = (
                f"{f.estimate.watts:.0f} W x {runtime_s:.0f} s = {f.estimate.kwh:.4f} kWh; "
                f"rate delta ${rate:.2f} - ${OFF_PEAK_USD_PER_KWH:.2f} = ${rate - OFF_PEAK_USD_PER_KWH:.2f}/kWh; "
                f"{f.estimate.kwh:.4f} kWh x ${rate - OFF_PEAK_USD_PER_KWH:.2f} = ${delta:.3f} avoidable by shifting"
            )
            findings.append(f)
    return findings


# --------------------------------------------------------------------------
# R7 — comfort guardrail (a FILTER, not a detector)
# --------------------------------------------------------------------------

def r7_comfort_guardrail(findings: List[Finding], snapshot: Dict) -> List[Finding]:
    """TAG recommendations that would make the home uncomfortable.

    An assistant that tells you to switch off the A/C at 29 C is not helping. This
    rule is why the system can be trusted to act on its own advice.

    It used to `continue` past a vetoed finding, which threw away the most
    interesting thing the system does. A suppressed finding is now marked and
    returned, so callers can either drop it (`evaluate`, unchanged) or show it as
    a decision that was considered and rejected (`evaluate_all`). The veto itself
    is unchanged — suppressed advice is never narrated and never offered, and
    server.py enforces the same limit again as a pre-flight gate on /api/apply.
    """
    for f in findings:
        load_name = f.load_key.split("/")[-1]
        room_temp = snapshot.get("rooms", {}).get(f.room, {}).get("temp_c")
        if room_temp is None:
            continue

        if load_name == "ac" and room_temp > COMFORT_MAX_C:
            f.suppressed_by = "comfort_guardrail"
            f.suppressed_reason = (
                f"{f.room} is {room_temp:.1f} C, above the {COMFORT_MAX_C:.0f} C "
                f"comfort limit — cutting cooling here would make the room "
                f"uncomfortable, so this saving is not offered.")
        elif load_name == "heater" and room_temp < COMFORT_MIN_C:
            f.suppressed_by = "comfort_guardrail"
            f.suppressed_reason = (
                f"{f.room} is {room_temp:.1f} C, below the {COMFORT_MIN_C:.0f} C "
                f"comfort limit — cutting heat here would make the room "
                f"uncomfortable, so this saving is not offered.")
    return findings


DETECTORS = [
    r1_unoccupied_lights_on,
    r2_away_with_hvac_on,
    r3_daylight_waste,
    r4_hvac_with_window_open,
    r5_phantom_standby,
    r6_peak_hour_heavy_load,
]


def evaluate_all(snapshot: Dict, now_dt: Optional[datetime] = None):
    """Run every rule. Returns (offered, suppressed), each sorted by cost.

    `suppressed` are findings a guardrail vetoed. They are fully computed and
    fully costed — the system knows exactly what that saving would have been and
    declines to take it. Showing them is the difference between a system that
    looks like it found two things and one that visibly considered four.
    """
    if now_dt is None:
        now_dt = datetime.fromtimestamp(snapshot.get("now", 0))

    findings: List[Finding] = []
    for detector in DETECTORS:
        try:
            findings.extend(detector(snapshot, now_dt))
        except Exception as exc:  # never let one bad rule kill the loop
            print(f"[rules] {detector.__name__} failed: {exc}")

    findings = r7_comfort_guardrail(findings, snapshot)
    findings.sort(key=lambda f: f.usd, reverse=True)
    return ([f for f in findings if not f.suppressed],
            [f for f in findings if f.suppressed])


def evaluate(snapshot: Dict, now_dt: Optional[datetime] = None) -> List[Finding]:
    """Findings the system is willing to recommend, sorted by dollar value.

    Suppressed findings are NOT included — this is the actionable list, and every
    existing caller depends on that. Use `evaluate_all()` to see the vetoed ones.
    """
    offered, _ = evaluate_all(snapshot, now_dt)
    return offered


# --------------------------------------------------------------------------
# Self-test — one scenario per rule. This is the test suite.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import time as _time

    T = 1754240000  # fixed epoch so runs are reproducible
    EVENING = datetime(2026, 8, 3, 18, 30)   # on-peak
    MORNING = datetime(2026, 8, 3, 10, 0)    # off-peak

    def base(**kw):
        snap = {
            "rooms": {"living": {"occupancy": True, "lux": 120, "temp_c": 23.0,
                                 "humidity": 45, "last_occupied_ts": T, "ts": T,
                                 "temp_drop_c": 1.0}},
            "loads": {},
            "user": {"presence": "home", "distance_m": 0, "battery": 80, "ts": T},
            "now": T,
        }
        snap.update(kw)
        return snap

    scenarios = []

    # R1
    s = base()
    s["rooms"]["living"].update(occupancy=False, last_occupied_ts=T - 1500)
    s["loads"]["living/lights"] = {"state": "on", "watts": 240, "ts": T}
    scenarios.append(("R1 unoccupied lights", s, EVENING))

    # R2
    s = base()
    s["user"].update(presence="away", distance_m=2400, ts=T - 1800)
    s["loads"]["living/ac"] = {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800}
    scenarios.append(("R2 away with HVAC on", s, EVENING))

    # R3
    s = base()
    s["rooms"]["living"].update(lux=620, lux_high_since=T - 900)
    s["loads"]["living/lights"] = {"state": "on", "watts": 240, "ts": T}
    scenarios.append(("R3 daylight waste", s, MORNING))

    # R4
    s = base()
    s["rooms"]["living"].update(humidity=68, temp_drop_c=0.1)
    s["loads"]["living/ac"] = {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800}
    scenarios.append(("R4 A/C vs open window", s, MORNING))

    # R5
    s = base()
    s["user"].update(presence="away", distance_m=5000, ts=T - 10800)
    s["loads"]["living/standby"] = {"state": "on", "watts": 12, "ts": T}
    scenarios.append(("R5 phantom standby", s, MORNING))

    # R6
    s = base()
    s["loads"]["living/dryer"] = {"state": "on", "watts": 3000, "ts": T, "on_since": T - 1800}
    scenarios.append(("R6 peak-hour dryer", s, EVENING))

    # R7 — suppression: R2 would fire, but it is 29 C
    s = base()
    s["rooms"]["living"].update(temp_c=29.5)
    s["user"].update(presence="away", distance_m=2400, ts=T - 1800)
    s["loads"]["living/ac"] = {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800}
    scenarios.append(("R7 comfort guardrail SUPPRESSES R2", s, EVENING))

    # All-good control
    scenarios.append(("CONTROL all good (expect nothing)", base(), MORNING))

    print("\n" + "=" * 92)
    print("RULES ENGINE SELF-TEST")
    print("=" * 92)

    for name, snap, when in scenarios:
        fs, vetoed = evaluate_all(snap, when)
        print(f"\n--- {name}")
        if not fs and not vetoed:
            print("    (no findings)")
        for f in fs:
            print(f"    [{f.severity:8s}] {f.rule_name}")
            print(f"      {f.headline}")
            print(f"      ${f.usd:.3f}  |  {f.estimate.kwh:.4f} kWh  |  {f.estimate.co2_kg:.4f} kg CO2")
            for e in f.evidence:
                print(f"        - {e}")
            print(f"      actions: {', '.join(f.suggested_actions)}")
        for f in vetoed:
            # Printed so the self-test shows what was WITHHELD, not just what was
            # offered. A rule that silently removes things is hard to trust.
            print(f"    [SUPPRESSED] {f.rule_name}  (${f.usd:.3f} not offered)")
            print(f"      vetoed by {f.suppressed_by}: {f.suppressed_reason}")
    print("\n" + "=" * 92 + "\n")
