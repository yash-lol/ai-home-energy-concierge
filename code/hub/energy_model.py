"""Deterministic energy arithmetic for the AI Home Energy Concierge.

This module is the credibility of the entire project. Every number shown on the
dashboard, spoken by the LLM, or claimed on stage originates here.

DESIGN RULE: the LLM never computes. It receives numbers from this module and is
allowed only to phrase them. Each estimate carries a `formula` string that spells
out the calculation so a judge can audit any figure on screen by hand.

No I/O, no MQTT, no LLM. Pure functions only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, Tuple

# --------------------------------------------------------------------------
# Load power draws
# --------------------------------------------------------------------------
# Every entry carries a `source` so we can defend the figure if challenged.
# Figures are typical steady-state draw, not startup surge.

LOADS: Dict[str, Dict] = {
    "led_bulb_set":     {"watts": 36.0,   "label": "LED bulb set (4 x 9 W)",     "source": "US DOE typical LED, 9 W each"},
    "incandescent_set": {"watts": 240.0,  "label": "Incandescent set (4 x 60 W)", "source": "nameplate 60 W each"},
    "ceiling_fan":      {"watts": 70.0,   "label": "Ceiling fan (medium)",        "source": "US DOE typical range 50-80 W"},
    "window_ac":        {"watts": 1100.0, "label": "Window air conditioner",      "source": "Energy Star typical 10k BTU unit"},
    "central_ac":       {"watts": 3500.0, "label": "Central A/C compressor",      "source": "US DOE typical 3-ton residential"},
    "space_heater":     {"watts": 1500.0, "label": "Space heater",                "source": "nameplate, standard US 120 V unit"},
    "tv_65":            {"watts": 120.0,  "label": "65-inch LED TV",              "source": "Energy Star typical 2024 panel"},
    "desktop_pc":       {"watts": 200.0,  "label": "Desktop PC + monitor",        "source": "measured typical idle-to-load average"},
    "game_console":     {"watts": 160.0,  "label": "Game console (active)",       "source": "manufacturer active-play figure"},
    "clothes_dryer":    {"watts": 3000.0, "label": "Clothes dryer (electric)",    "source": "US DOE typical electric resistance dryer"},
    "dishwasher":       {"watts": 1200.0, "label": "Dishwasher (heated cycle)",   "source": "Energy Star typical with heated dry"},
    "fridge":           {"watts": 150.0,  "label": "Refrigerator (compressor on)", "source": "Energy Star typical mid-size"},
    "standby_phantom":  {"watts": 12.0,   "label": "Standby / phantom load",      "source": "LBNL standby power survey, per-device average"},
}

# --------------------------------------------------------------------------
# Tariff — San Diego SDG&E time-of-use
# --------------------------------------------------------------------------
# Rates approximate SDG&E TOU-DR1 residential. On-peak 4 PM - 9 PM daily.
# Local touch: the demo is in San Diego, and peak-shifting advice is only
# meaningful against a real local tariff structure.

OFF_PEAK_USD_PER_KWH = 0.32
ON_PEAK_USD_PER_KWH = 0.58
ON_PEAK_START = time(16, 0)
ON_PEAK_END = time(21, 0)

# California grid average carbon intensity.
# Source: CA Energy Commission / EPA eGRID CAMX region, ~0.25 kg CO2 per kWh.
CO2_KG_PER_KWH = 0.25


def rate_at(dt: datetime) -> Tuple[float, str]:
    """Return (usd_per_kwh, period_label) for a given local datetime.

    On-peak is 16:00-21:00 inclusive of start, exclusive of end.
    """
    if ON_PEAK_START <= dt.time() < ON_PEAK_END:
        return ON_PEAK_USD_PER_KWH, "on_peak"
    return OFF_PEAK_USD_PER_KWH, "off_peak"


def seconds_to_peak(dt: datetime) -> float:
    """Seconds until the on-peak window opens. 0.0 if it is already open.

    The tariff calendar is the one thing about the future this system knows for
    certain — no model, no prediction, just a published schedule. That is what
    makes warning BEFORE the boundary defensible arithmetic rather than a guess.
    """
    if ON_PEAK_START <= dt.time() < ON_PEAK_END:
        return 0.0
    start_today = dt.replace(hour=ON_PEAK_START.hour, minute=ON_PEAK_START.minute,
                             second=0, microsecond=0)
    if dt.time() < ON_PEAK_START:
        return (start_today - dt).total_seconds()
    # Past the close: the next opening is tomorrow.
    return (start_today - dt).total_seconds() + 24 * 3600


def rate_delta() -> float:
    """What the on-peak window costs you, per kWh, over running off-peak."""
    return ON_PEAK_USD_PER_KWH - OFF_PEAK_USD_PER_KWH


# --------------------------------------------------------------------------
# Core arithmetic
# --------------------------------------------------------------------------


def kwh(watts: float, seconds: float) -> float:
    """Energy in kilowatt-hours.

    Formula: kWh = watts x seconds / 3_600_000
    """
    return (watts * seconds) / 3_600_000.0


def cost(energy_kwh: float, usd_per_kwh: float) -> float:
    """Cost in dollars.

    Formula: usd = kWh x rate
    """
    return energy_kwh * usd_per_kwh


def co2_kg(energy_kwh: float) -> float:
    """Carbon in kilograms.

    Formula: kg = kWh x CO2_KG_PER_KWH
    """
    return energy_kwh * CO2_KG_PER_KWH


@dataclass
class WasteEstimate:
    """One auditable waste figure.

    `formula` is a hard requirement: the dashboard renders it verbatim so any
    number on screen can be recomputed by hand.
    """

    kwh: float
    usd: float
    co2_kg: float
    rate_used: float
    period_label: str
    watts: float
    load_label: str
    source: str
    seconds: float
    formula: str = field(default="")


def waste_estimate(load_key: str, seconds_wasted: float, at_time: datetime) -> WasteEstimate:
    """Compute an auditable waste estimate for a load left running.

    Formula chain:
      kWh = watts x seconds / 3_600_000
      usd = kWh x rate(at_time)
      kg  = kWh x 0.25
    """
    if load_key not in LOADS:
        raise KeyError(f"unknown load key {load_key!r}; add it to LOADS with a source")

    spec = LOADS[load_key]
    watts = spec["watts"]
    rate, period = rate_at(at_time)

    e = kwh(watts, seconds_wasted)
    usd = cost(e, rate)
    kg = co2_kg(e)

    formula = (
        f"{watts:.0f} W x {seconds_wasted:.0f} s = {e:.4f} kWh; "
        f"{e:.4f} kWh x ${rate:.2f}/kWh ({period}) = ${usd:.3f}; "
        f"{e:.4f} kWh x {CO2_KG_PER_KWH} kg/kWh = {kg:.4f} kg CO2"
    )

    return WasteEstimate(
        kwh=e,
        usd=usd,
        co2_kg=kg,
        rate_used=rate,
        period_label=period,
        watts=watts,
        load_label=spec["label"],
        source=spec["source"],
        seconds=seconds_wasted,
        formula=formula,
    )


def annualize(usd_per_event: float, events_per_week: float) -> float:
    """Project a per-event cost to an annual figure.

    Formula: usd_year = usd_event x events_per_week x 52
    """
    return usd_per_event * events_per_week * 52.0


# --------------------------------------------------------------------------
# Self-test — verify the math by hand against this output
# --------------------------------------------------------------------------

if __name__ == "__main__":
    on_peak = datetime(2026, 8, 3, 18, 30)   # 6:30 PM -> on-peak
    off_peak = datetime(2026, 8, 3, 10, 0)   # 10:00 AM -> off-peak

    scenarios = [
        ("incandescent_set", 3600, on_peak,  "Lights left on 1 h, evening peak"),
        ("incandescent_set", 3600, off_peak, "Lights left on 1 h, mid-morning"),
        ("window_ac",        7200, on_peak,  "A/C running 2 h while away, peak"),
        ("clothes_dryer",    2700, on_peak,  "Dryer 45 min during peak window"),
        ("standby_phantom",  28800, off_peak, "Phantom load 8 h overnight"),
    ]

    print("\n" + "=" * 96)
    print("ENERGY MODEL SELF-TEST  —  every figure below is hand-checkable")
    print("=" * 96)

    for key, secs, when, note in scenarios:
        est = waste_estimate(key, secs, when)
        print(f"\n{note}")
        print(f"  load     : {est.load_label}  ({est.watts:.0f} W)")
        print(f"  source   : {est.source}")
        print(f"  window   : {when.strftime('%H:%M')}  -> {est.period_label} @ ${est.rate_used:.2f}/kWh")
        print(f"  energy   : {est.kwh:.4f} kWh")
        print(f"  cost     : ${est.usd:.3f}")
        print(f"  carbon   : {est.co2_kg:.4f} kg CO2")
        print(f"  formula  : {est.formula}")

    print("\n" + "-" * 96)
    daily = waste_estimate("window_ac", 7200, on_peak)
    print(f"Annualized: A/C wasted 2 h, 4 nights a week  ->  "
          f"${annualize(daily.usd, 4):.2f} per year")
    print(f"  formula  : ${daily.usd:.3f} x 4 events/week x 52 weeks")
    print("-" * 96 + "\n")
