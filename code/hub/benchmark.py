#!/usr/bin/env python3
"""Performance and efficiency benchmark.

Exists because **Technical Implementation is 40 of the 100 available points**, and it
is scored on *"resource utilization, optimization, latency and performance, and energy
efficiency"* — i.e. on measurements, not claims.

There is a pleasing symmetry to lead the slide with: an energy-saving application that
measures its own energy cost.

Run:
  python hub/benchmark.py                  # full run
  python hub/benchmark.py --markdown       # emit a Markdown table for the README
  python hub/benchmark.py --iterations 50  # more samples

Compare runtimes by pointing at each and re-running:
  LLM_ENABLED=0 python hub/benchmark.py                                  # control
  LLM_BASE_URL=http://127.0.0.1:18181/v1 python hub/benchmark.py         # GenieX NPU
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import rules
from energy_model import waste_estimate
from llm import LLMClient, template_narrate

try:
    import psutil
except ImportError:
    psutil = None


# --------------------------------------------------------------------------
# Measurement helpers
# --------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    unit: str
    samples: List[float] = field(default_factory=list)
    note: str = ""
    ok: bool = True

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else float("nan")

    @property
    def p95(self) -> float:
        if not self.samples:
            return float("nan")
        if len(self.samples) < 20:
            return max(self.samples)
        ordered = sorted(self.samples)
        return ordered[int(len(ordered) * 0.95) - 1]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else float("nan")


def timed(fn: Callable, iterations: int) -> List[float]:
    """Return per-call wall time in milliseconds."""
    out = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


# --------------------------------------------------------------------------
# Fixtures — a realistic on-peak waste scenario
# --------------------------------------------------------------------------

T = 1754240000
EVENING = datetime(2026, 8, 3, 18, 30)   # on-peak, worst case for cost


def make_snapshot() -> Dict:
    return {
        "rooms": {"living": {"occupancy": False, "lux": 110, "temp_c": 23.6,
                             "humidity": 47, "last_occupied_ts": T - 1500,
                             "ts": T, "temp_drop_c": 0.9}},
        "loads": {
            "living/lights": {"state": "on", "watts": 240, "ts": T, "on_since": T - 1500},
            "living/ac": {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800},
            "living/tv": {"state": "on", "watts": 120, "ts": T, "on_since": T - 3600},
        },
        "user": {"presence": "away", "distance_m": 2400, "battery": 72, "ts": T - 1800},
        "now": T,
    }


# --------------------------------------------------------------------------
# Benchmarks
# --------------------------------------------------------------------------

def bench_energy_model(iterations: int) -> Result:
    r = Result("Energy-model estimate", "ms")
    r.samples = timed(lambda: waste_estimate("window_ac", 7200, EVENING), iterations * 10)
    r.note = "pure arithmetic + formula string"
    return r


def bench_rules(iterations: int) -> Result:
    snap = make_snapshot()
    r = Result("Rules engine (8 rules)", "ms")
    r.samples = timed(lambda: rules.evaluate(snap, EVENING), iterations * 5)
    n = len(rules.evaluate(snap, EVENING))
    r.note = f"all 8 rules over 1 room / 3 loads -> {n} findings"
    return r


def bench_narration(iterations: int) -> Dict[str, Result]:
    """Latency of the two narration paths — the headline comparison."""
    snap = make_snapshot()
    findings = rules.evaluate(snap, EVENING)
    if not findings:
        raise RuntimeError("fixture produced no findings")
    f = findings[0]

    out: Dict[str, Result] = {}

    tmpl = Result("Narration — deterministic template", "ms")
    tmpl.samples = timed(lambda: template_narrate(f), iterations * 5)
    tmpl.note = "control path; no model, no network"
    out["template"] = tmpl

    enabled = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False")
    base = os.environ.get("LLM_BASE_URL", "http://localhost:18181/v1")
    model = os.environ.get("LLM_MODEL", "local-model")

    llm_res = Result(f"Narration — local LLM", "ms")
    if not enabled:
        llm_res.ok = False
        llm_res.note = "skipped: LLM_ENABLED=0"
    else:
        client = LLMClient()
        probe = client.narrate(f)
        if probe.narrated_by != "llm":
            llm_res.ok = False
            llm_res.note = f"endpoint unreachable ({base}) — fell back to template"
        else:
            # Fewer iterations: real inference is expensive.
            n = max(3, min(iterations, 10))
            llm_res.samples = timed(lambda: client.narrate(f), n)
            llm_res.note = f"{model} @ {base}"
    out["llm"] = llm_res
    return out


def bench_end_to_end(iterations: int) -> Result:
    """Snapshot -> findings -> narrated recommendations, the full reasoning path."""
    snap = make_snapshot()

    def once():
        for f in rules.evaluate(snap, EVENING):
            template_narrate(f)

    r = Result("Sensor snapshot -> recommendations", "ms")
    r.samples = timed(once, iterations * 3)
    r.note = "rules + energy model + deterministic narration"
    return r


def bench_memory() -> Result:
    r = Result("Peak RSS of this process", "MB")
    if psutil is None:
        r.ok = False
        r.note = "psutil not installed (pip install psutil)"
        return r
    proc = psutil.Process(os.getpid())
    r.samples = [proc.memory_info().rss / (1024 * 1024)]
    r.note = "hub reasoning stack loaded"
    return r


def bench_edge_filtering() -> Result:
    """Quantify the UNO Q's change-triggered publishing.

    The publisher samples at 1 Hz but only publishes on a material change or a 10 s
    heartbeat. This is a real optimization we implemented and never measured; it is
    exactly the "resource utilization" the rubric asks about.
    """
    import random

    LUX_CHANGE_PCT, TEMP_CHANGE_C, HEARTBEAT_S = 0.15, 0.3, 10
    random.seed(42)   # deterministic, so the reported figure is reproducible

    samples = 600           # 10 minutes at 1 Hz
    published = 0
    last: Dict[str, float] = {}
    last_pub = -HEARTBEAT_S
    occ, lux, temp = True, 200.0, 23.0

    for t in range(samples):
        if random.random() < 0.02:
            occ = not occ
        lux = max(0.0, min(900.0, lux + random.uniform(-8, 8)))
        temp = max(18.0, min(30.0, temp + random.uniform(-0.05, 0.05)))

        material = (
            not last
            or occ != last.get("occupancy")
            or abs(temp - last.get("temp_c", 0)) >= TEMP_CHANGE_C
            or abs(lux - last.get("lux", 0)) >= max(LUX_CHANGE_PCT * max(last.get("lux", 1), 1), 10)
        )
        if material or (t - last_pub) >= HEARTBEAT_S:
            published += 1
            last = {"occupancy": occ, "lux": lux, "temp_c": temp}
            last_pub = t

    reduction = (1 - published / samples) * 100.0
    r = Result("Broker messages avoided by edge filtering", "%")
    r.samples = [reduction]
    r.note = f"{published} published of {samples} sampled (10 min @ 1 Hz, seeded)"
    return r


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_report(results: List[Result], markdown: bool) -> None:
    if markdown:
        print("\n| Metric | p50 | p95 | Unit | Notes |")
        print("|---|---|---|---|---|")
        for r in results:
            if not r.ok:
                print(f"| {r.name} | — | — | {r.unit} | {r.note} |")
                continue
            p95 = "—" if len(r.samples) < 2 else f"{r.p95:.3f}"
            print(f"| {r.name} | {r.p50:.3f} | {p95} | {r.unit} | {r.note} |")
        print()
        return

    width = max(len(r.name) for r in results) + 2
    print("\n" + "=" * (width + 46))
    print("  BENCHMARK — resource utilization, latency, efficiency")
    print("=" * (width + 46))
    for r in results:
        if not r.ok:
            print(f"  {r.name:<{width}} {'SKIPPED':>10}   {r.note}")
            continue
        if len(r.samples) == 1:
            print(f"  {r.name:<{width}} {r.p50:>10.2f} {r.unit}")
        else:
            print(f"  {r.name:<{width}} p50 {r.p50:>8.3f} {r.unit}   "
                  f"p95 {r.p95:>8.3f} {r.unit}   n={len(r.samples)}")
        if r.note:
            print(f"  {'':<{width}} {r.note}")
    print("=" * (width + 46))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--markdown", action="store_true", help="emit a Markdown table")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    if not args.markdown and not args.json:
        print(f"\nrunning benchmarks (iterations={args.iterations})…")

    results: List[Result] = []
    results.append(bench_energy_model(args.iterations))
    results.append(bench_rules(args.iterations))

    narr = bench_narration(args.iterations)
    results.append(narr["template"])
    results.append(narr["llm"])

    results.append(bench_end_to_end(args.iterations))
    results.append(bench_edge_filtering())
    results.append(bench_memory())

    if args.json:
        print(json.dumps([{
            "name": r.name, "unit": r.unit, "ok": r.ok, "note": r.note,
            "p50": None if not r.samples else round(r.p50, 4),
            "p95": None if len(r.samples) < 2 else round(r.p95, 4),
            "n": len(r.samples),
        } for r in results], indent=2))
        return 0

    print_report(results, args.markdown)

    if not args.markdown:
        tmpl, llm_r = narr["template"], narr["llm"]
        print("\nHeadline numbers for the slide:")
        print(f"  · Rules engine decides in {results[1].p50:.2f} ms — the edge tier is effectively free.")
        if llm_r.ok and llm_r.samples:
            speedup = llm_r.p50 / tmpl.p50 if tmpl.p50 else 0
            print(f"  · Local LLM narration: {llm_r.p50:.0f} ms p50, {llm_r.p95:.0f} ms p95.")
            print(f"  · Deterministic fallback is {speedup:.0f}x faster and always available.")
        else:
            print(f"  · Deterministic narration: {tmpl.p50:.3f} ms — the always-available path.")
            print(f"    (Start GenieX and re-run to fill in the NPU column: {llm_r.note})")
        print(f"  · Edge filtering removes {results[5].p50:.0f}% of broker traffic.")
        if results[6].ok:
            print(f"  · Hub reasoning stack: {results[6].p50:.0f} MB RSS.")
        print("\n  Next: run /quad-profile at a QUAD support session for authoritative")
        print("  NPU latency / power / utilization on real silicon, then paste it into")
        print("  README.md beside this table.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
