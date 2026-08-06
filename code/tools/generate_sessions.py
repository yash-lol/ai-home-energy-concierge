#!/usr/bin/env python3
"""Generate a synthetic household corpus — declared synthetic, end to end.

WHY A GENERATOR AT ALL
----------------------
Labels are the scarce resource. Ticks accrue for free at 674 KB/hour, but an
approval only exists when a human clicks, so an unattended overnight run yields
thousands of rows and no training signal. This produces a corpus of the size a
model needs without pretending anyone was standing there clicking.

WHAT IS AND IS NOT INVENTED HERE
--------------------------------
Invented: the household. Occupancy schedule, presence, appliance use, the
daylight curve, the temperature cycle.

NOT invented: the findings or their arithmetic. Every situation is fed through
the real `rules.evaluate_all()` and narrated by the real `template_narrate()`,
so a row's rule, cost, formula and evidence are exactly what the live system
would have produced from that state. The generator fabricates *inputs*, never
outputs.

THE PART THAT MAKES THIS WORTH TRAINING ON
------------------------------------------
A generator that labelled rows using the rules would teach a model to
re-derive R1-R8, which is circular: it would score well and know nothing. So the
simulated occupant has PREFERENCES THE RULES DO NOT ENCODE, and the model's job
is to discover them:

  * they act on HVAC waste, and largely ignore lighting advice while at home
  * they act far more readily when out of the house
  * they never act between 23:00 and 07:00 — they are asleep
  * they ignore phantom-standby advice entirely; it is too small to care about
  * they respond to size: the bigger the figure, the likelier the tap
  * about one decision in eight goes the other way, because people are noisy

None of that is visible to the rules engine, which only knows what is wasteful —
not what this household will actually do about it. That gap is precisely the
"decides what is worth your attention" mandate in the tier table.

PROVENANCE
----------
Every sensor value is stamped `*_src="synthetic"` and every file is named
`session-synthetic-*.jsonl`, so `hub/recorder.py`'s provenance summary classifies
these rows as `contains_synthetic` and `tools/build_dataset.py` reports the share
automatically. A model trained on this must be described as trained on
simulation. That is fine. Describing it otherwise is not.

Run:
    python tools/generate_sessions.py --days 14
    python tools/generate_sessions.py --days 30 --seed 7 --tick-min 5
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))

import rules                                    # noqa: E402
from energy_model import rate_at                # noqa: E402
from llm import template_narrate                # noqa: E402
from recorder import SessionRecorder            # noqa: E402

SESSION_DIR = ROOT / "data" / "sessions"
ROOM = "living"

SYNTHETIC_SRC = {"temp_src": "synthetic", "hum_src": "synthetic",
                 "lux_src": "synthetic", "occ_src": "synthetic"}

# Mirrors hub/server.py _load_from_rule(). Kept here as a constant rather than
# inlined so the two stay easy to compare when a rule is added.
LOAD_FOR_RULE = {
    "unoccupied_lights_on": "lights",
    "daylight_waste": "lights",
    "away_with_hvac_on": "ac",
    "hvac_with_window_open": "ac",
    "phantom_standby": "standby",
    "peak_hour_heavy_load": "dryer",
    "peak_window_imminent": "dryer",
}


# --------------------------------------------------------------------------
# The household — invented, and stamped as such
# --------------------------------------------------------------------------

class Household:
    """A plausible weekday/weekend occupancy and appliance pattern."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        # Per-household habits, drawn once so a corpus has a personality rather
        # than being uniform noise.
        self.leave_h = rng.gauss(8.5, 0.4)
        self.return_h = rng.gauss(18.0, 0.6)
        self.bedtime_h = rng.gauss(23.0, 0.5)
        self.wake_h = rng.gauss(7.0, 0.5)
        self.forgets_lights_p = rng.uniform(0.25, 0.55)
        self.runs_dryer_p = rng.uniform(0.3, 0.6)
        # How often, while awake and in the house, they are actually in THIS
        # room rather than the kitchen or the bedroom.
        self.in_room_p = rng.uniform(0.55, 0.8)

    def at_home(self, dt: datetime) -> bool:
        """Geofence: is anyone in the HOUSE. This is what `user.presence` means.

        Deliberately not the same question as `occupancy`, which is about this
        one room — see the note on `asleep()`.
        """
        h = dt.hour + dt.minute / 60.0
        weekend = dt.weekday() >= 5
        if weekend:
            return not (11.0 < h < 15.0 and self.rng.random() < 0.4)
        return not (self.leave_h < h < self.return_h)

    def asleep(self, dt: datetime) -> bool:
        """In bed — home, but not in the living room.

        This is what separates presence from occupancy. An earlier version of
        this generator set `presence = "home" if occupancy else "away"`, which
        made the two perfectly anti-correlated: r = -1.000 across 18,704 ticks.
        Every model trained on the corpus then had nine features carrying eight
        bits, with one effect split across two weights that swamped everything
        else — and "A/C running at 3 AM" could not be learned as unusual,
        because `occupancy=1` argued it was a perfectly normal occupied room.

        A house where someone is asleep upstairs is home AND has an empty living
        room. That is both the truthful model and the one that makes the demo's
        motivating case actually anomalous.
        """
        h = dt.hour + dt.minute / 60.0
        if self.bedtime_h >= 24.0:
            return h < self.wake_h
        return h >= self.bedtime_h or h < self.wake_h

    def lux(self, dt: datetime) -> int:
        """Daylight curve, peaking near 13:00, with weather knocked off it."""
        h = dt.hour + dt.minute / 60.0
        if h < 6.5 or h > 20.0:
            return self.rng.randint(0, 25)
        day = math.sin((h - 6.5) / 13.5 * math.pi)
        cloud = self.rng.uniform(0.35, 1.0)
        return max(0, int(1500 * day * cloud + self.rng.gauss(0, 40)))

    def day_heat(self, dt: datetime) -> float:
        """A per-day heat offset, so the fortnight contains genuinely hot days.

        Without this the temperature never crosses R7's 27 C limit and the corpus
        contains zero guardrail events — which would leave the most important
        behaviour in the system entirely unrepresented in the data.
        """
        r = random.Random(f"{self.rng.randint(0, 1)}-{dt.date()}")
        return r.choice([0.0, 0.0, 0.0, 1.5, 3.0, 4.5])

    def temp(self, dt: datetime, ac_on: bool) -> float:
        h = dt.hour + dt.minute / 60.0
        base = 22.5 + 5.0 * math.sin((h - 9.0) / 24.0 * 2 * math.pi)
        base += self.day_heat(dt)
        if ac_on:
            base -= 2.2
        return round(base + self.rng.gauss(0, 0.4), 1)

    def humidity(self, dt: datetime) -> float:
        h = dt.hour + dt.minute / 60.0
        return round(max(20.0, min(85.0,
                     52.0 - 12.0 * math.sin((h - 9.0) / 24.0 * 2 * math.pi)
                     + self.rng.gauss(0, 3.5))), 1)


def approval_probability(rec, dt: datetime, presence: str) -> float:
    """The occupant's hidden preferences — what the model has to discover.

    Deliberately NOT a function of the rules' own logic. It is a function of the
    person: what they care about, when they are awake, how much money moves them.
    """
    h = dt.hour
    p = 0.30

    if 23 <= h or h < 7:
        p -= 0.50                                   # asleep
    if presence == "away":
        p += 0.25                                   # trusts it more when out
    if rec.rule_name in ("away_with_hvac_on", "hvac_with_window_open"):
        p += 0.30                                   # cares about HVAC
    if rec.rule_name in ("unoccupied_lights_on", "daylight_waste"):
        p -= 0.30 if presence == "home" else 0.05   # lights matter less at home
    if rec.rule_name == "phantom_standby":
        p -= 0.45                                   # beneath their notice
    if rec.usd >= 0.30:
        p += 0.30                                   # size moves them
    elif rec.usd < 0.05:
        p -= 0.20
    if rec.severity == "critical":
        p += 0.15
    if rec.kind == "anticipated":
        p += 0.10                                   # likes being warned early

    return max(0.02, min(0.95, p))


# --------------------------------------------------------------------------

def generate(days: int, tick_min: int, seed: int, out_dir: Path) -> Path:
    rng = random.Random(seed)
    home = Household(rng)

    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=days)
    sim_now = start

    rec = SessionRecorder(directory=out_dir)
    # Rename to advertise the corpus as synthetic in the filename itself, not
    # only inside the rows. Someone listing the directory should not have to
    # open a file to know what it is.
    synth_path = out_dir / f"session-synthetic-{rec.run_id}.jsonl"
    try:
        rec.path.unlink()
    except OSError:
        pass
    rec.path = synth_path
    rec.clock = lambda: sim_now.timestamp()
    rec.seq = 0
    rec._write({"type": "session_start", "run_id": rec.run_id,
                "origin": "synthetic",
                "generator": "tools/generate_sessions.py",
                "note": "SIMULATED HOUSEHOLD. Sensor values invented; findings "
                        "and arithmetic produced by the real rules engine.",
                "seed": seed, "days": days, "tick_minutes": tick_min})

    # Mutable state, mirroring what StateStore maintains on the live hub.
    last_occupied = sim_now.timestamp()
    lux_high_since = sim_now.timestamp()
    presence_ts = sim_now.timestamp()
    prev_temp = 22.5
    loads = {
        f"{ROOM}/lights": {"state": "off", "watts": 240.0, "on_since": 0.0},
        f"{ROOM}/ac": {"state": "off", "watts": 1100.0, "on_since": 0.0},
        f"{ROOM}/dryer": {"state": "off", "watts": 3000.0, "on_since": 0.0},
        f"{ROOM}/standby": {"state": "on", "watts": 12.0, "on_since": sim_now.timestamp()},
    }
    prev_presence = "home"
    in_room = True               # sticky room presence, see the tick loop
    dryer_until = None
    pending = []                 # decisions the occupant has not made yet
    seen_ids = set()
    stats = {"ticks": 0, "findings": 0, "applies": 0, "thumbs_up": 0,
             "thumbs_down": 0, "vetoes": 0, "refusals": 0}

    total_ticks = days * 24 * 60 // tick_min
    for _ in range(total_ticks):
        now = sim_now.timestamp()

        # presence = the geofence (in the house). occupancy = in THIS room.
        # They are different questions and the corpus has to say so, or the two
        # features collapse into one — see Household.asleep().
        at_home = home.at_home(sim_now)
        asleep = home.asleep(sim_now)
        if not at_home or asleep:
            occupied = False
        elif rng.random() < 0.9:
            occupied = in_room          # rooms are sticky; people do not teleport
        else:
            occupied = rng.random() < home.in_room_p
        in_room = occupied

        presence = "home" if at_home else "away"
        departed = (presence == "away" and prev_presence == "home")
        if presence != prev_presence:
            presence_ts = now
            prev_presence = presence
        if occupied:
            last_occupied = now

        # --- appliances react to the household, not to the rules -----------
        lux = home.lux(sim_now)
        lights_on = loads[f"{ROOM}/lights"]["state"] == "on"

        # Lighting is a state machine, not a function of the current lux. An
        # earlier version simply set lights = (dark and occupied), which meant
        # they were never on in daylight and never on in an empty room — so R1
        # and R3, the two rules about lights, could not fire even once in a
        # fortnight. The waste this system exists to catch comes precisely from
        # lights being on when they should not be.
        if occupied:
            if not lights_on and lux < 250:
                lights_on = True
            elif lights_on and lux > 400 and rng.random() < 0.06:
                lights_on = False        # they eventually notice the daylight
        elif asleep and at_home:
            # Going to bed. Without this the corpus had the lights on for
            # 100.0% of every hour from 19:00 to 07:00 — nobody sleeps with the
            # living-room lights on every night, and a "normal" class that says
            # otherwise teaches the detector that a lit empty room at 3 AM is
            # ordinary. They usually turn them off; sometimes they forget, and
            # that residue is real R1 waste rather than a modelling accident.
            if lights_on and rng.random() < 0.85:
                lights_on = False
        elif departed:
            # Walking out and leaving them on is the whole reason R1 exists.
            lights_on = lights_on and rng.random() < home.forgets_lights_p
        # Otherwise they stay however they were left.
        want_lights = lights_on

        temp_no_ac = home.temp(sim_now, ac_on=False)
        want_ac = temp_no_ac > 25.5 or (
            loads[f"{ROOM}/ac"]["state"] == "on" and temp_no_ac > 24.0)

        if dryer_until and sim_now >= dryer_until:
            dryer_until = None
        if dryer_until is None and rng.random() < home.runs_dryer_p / (24 * 60 / tick_min):
            dryer_until = sim_now + timedelta(minutes=rng.choice([45, 60, 75]))
        want_dryer = dryer_until is not None

        for key, want in ((f"{ROOM}/lights", want_lights),
                          (f"{ROOM}/ac", want_ac),
                          (f"{ROOM}/dryer", want_dryer)):
            state = "on" if want else "off"
            if loads[key]["state"] != state:
                loads[key]["state"] = state
                if state == "on":
                    loads[key]["on_since"] = now
            loads[key]["ts"] = now

        temp = home.temp(sim_now, ac_on=loads[f"{ROOM}/ac"]["state"] == "on")
        humidity = home.humidity(sim_now)
        if lux <= rules.DAYLIGHT_LUX_THRESHOLD:
            lux_high_since = now

        room = {"occupancy": occupied, "lux": lux, "temp_c": temp,
                "humidity": humidity, "ts": now,
                "last_occupied_ts": last_occupied,
                "lux_high_since": lux_high_since,
                "temp_drop_c": round(prev_temp - temp, 2), **SYNTHETIC_SRC}
        prev_temp = temp

        snapshot = {
            "rooms": {ROOM: room},
            "loads": {k: dict(v) for k, v in loads.items()},
            "user": {"presence": presence,
                     "distance_m": 0 if presence == "home" else rng.randint(800, 6000),
                     "battery": rng.randint(35, 100), "ts": presence_ts},
            "now": now,
        }

        # --- the REAL rules engine, on invented inputs ----------------------
        offered, vetoed = rules.evaluate_all(snapshot, sim_now)

        rate, period = rate_at(sim_now)
        rec.tick({**snapshot,
                  "total_watts": sum(l["watts"] for l in loads.values()
                                     if l["state"] == "on"),
                  "tariff": {"rate": rate, "period": period,
                             "clock": sim_now.strftime("%H:%M")},
                  "mqtt_connected": True,
                  "recos": [{"id": f.id} for f in offered]})
        stats["ticks"] += 1

        # A finding id is STABLE — `r2-living-ac` is the same string every time
        # the A/C is left on, on any day. So "have I seen this id" is the wrong
        # question; the right one is "is this a new occurrence". An id that
        # stopped firing and came back is a second, independent decision, and
        # collapsing the two would turn a fortnight into about seven rows.
        firing = {f.id for f in offered} | {f.id for f in vetoed}
        for stale in [i for i in seen_ids if i not in firing]:
            seen_ids.discard(stale)          # episode over; it may recur fresh

        for f in vetoed:
            if f.id not in seen_ids:
                rec.veto(f.id, f.load_key, f.suppressed_reason, f.suppressed_by, f.usd)
                stats["vetoes"] += 1
                seen_ids.add(f.id)

        for f in offered:
            if f.id in seen_ids:
                continue
            seen_ids.add(f.id)
            r = template_narrate(f)
            rec.finding(r)
            stats["findings"] += 1
            # People do not answer instantly. The delay matters: the guardrail
            # can change its mind in between, which is how a REFUSAL (as opposed
            # to a veto) actually arises in the live system.
            pending.append({"rec": r, "at": sim_now + timedelta(
                minutes=rng.choice([tick_min, tick_min * 2, tick_min * 3]))})

        # --- the occupant decides ------------------------------------------
        still = []
        for item in pending:
            if sim_now < item["at"]:
                still.append(item)
                continue
            r = item["rec"]
            load = r.rule_name in ("away_with_hvac_on", "hvac_with_window_open")
            p = approval_probability(r, sim_now, presence)
            acts = rng.random() < p

            if acts:
                # Re-check the guardrail at ACTION time, exactly as /api/apply
                # does. If the room has become too hot since the card appeared,
                # the request is refused — a real preference signal that a veto
                # is not, because here the human did ask.
                if load and temp > rules.COMFORT_MAX_C:
                    rec.refusal(r.id, f"{ROOM}/ac", "off",
                                f"Refused: {ROOM} is {temp:.1f} C, above the "
                                f"{rules.COMFORT_MAX_C:.0f} C comfort limit",
                                "comfort_guardrail")
                    stats["refusals"] += 1
                else:
                    key = f"{ROOM}/{LOAD_FOR_RULE.get(r.rule_name, 'lights')}"
                    rec.apply(r.id, key, "off", "synthetic-occupant",
                              round(r.usd, 4), True)
                    rec.actuation(key, {"state": "off", "source": "kasa",
                                        "ok": True, "reco_id": r.id})
                    stats["applies"] += 1
                    if key in loads:
                        loads[key]["state"] = "off"
                    if rng.random() < 0.35:
                        rec.feedback(r.id, True, "", "synthetic-occupant")
                        stats["thumbs_up"] += 1
            elif rng.random() < 0.25:
                # An explicit "not useful" — a deliberate negative, much stronger
                # than the silence of an ignored card.
                rec.feedback(r.id, False, "", "synthetic-occupant")
                stats["thumbs_down"] += 1
        pending = still

        sim_now += timedelta(minutes=tick_min)

    return rec.path, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--tick-min", type=int, default=5,
                    help="simulated minutes per tick (5 keeps files sane)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(SESSION_DIR))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 78)
    print("  GENERATE SYNTHETIC HOUSEHOLD CORPUS")
    print("=" * 78)
    print(f"\n  {args.days} days at one tick per {args.tick_min} simulated minutes, "
          f"seed {args.seed}")
    print("  Sensor values are INVENTED and stamped *_src=\"synthetic\".")
    print("  Findings and arithmetic come from the real rules engine.\n")

    path, stats = generate(args.days, args.tick_min, args.seed, out)

    print(f"  wrote {path.name}  ({path.stat().st_size/1024:.0f} KB)")
    for k, v in stats.items():
        print(f"    {k:<12} {v}")
    print("\n  Next:  python tools/build_dataset.py")
    print("=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
