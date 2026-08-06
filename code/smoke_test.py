#!/usr/bin/env python3
"""Smoke test — verifies the whole hub without a broker, hardware, or an LLM.

Run this on Day 1 to prove the environment is sane, and again before the demo.
Exits non-zero on failure so it can gate a commit.

Run:  python smoke_test.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent / "hub"
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    print("\n" + "=" * 70)
    print("  AI HOME ENERGY CONCIERGE — SMOKE TEST")
    print("=" * 70)

    print("\n1. Dependencies")
    import importlib.util as iu
    for mod in ("fastapi", "uvicorn", "websockets", "paho.mqtt.client", "requests"):
        try:
            check(mod, iu.find_spec(mod) is not None)
        except Exception:
            check(mod, False, "not importable")

    print("\n2. Pure modules (no server needed)")
    sys.path.insert(0, str(HUB_DIR))
    try:
        import energy_model
        est = energy_model.waste_estimate(
            "window_ac", 7200, __import__("datetime").datetime(2026, 8, 3, 18, 30))
        check("energy_model arithmetic", abs(est.usd - 1.276) < 0.01, f"${est.usd:.3f} for 2h A/C on-peak")
        check("formula string present", "kWh" in est.formula and "=" in est.formula)
    except Exception as exc:
        check("energy_model", False, str(exc))

    try:
        import rules
        T = 1754240000
        snap = {
            "rooms": {"living": {"occupancy": False, "lux": 120, "temp_c": 23.0, "humidity": 45,
                                 "last_occupied_ts": T - 1500, "ts": T, "temp_drop_c": 1.0}},
            "loads": {"living/ac": {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800},
                      "living/lights": {"state": "on", "watts": 240, "ts": T}},
            "user": {"presence": "away", "distance_m": 2400, "battery": 70, "ts": T - 1800},
            "now": T,
        }
        fs = rules.evaluate(snap, __import__("datetime").datetime(2026, 8, 3, 18, 30))
        check("rules fire on a waste scenario", len(fs) >= 2, f"{len(fs)} findings")
        check("findings sorted by cost", all(fs[i].usd >= fs[i+1].usd for i in range(len(fs)-1)))

        hot = json.loads(json.dumps(snap))
        hot["rooms"]["living"]["temp_c"] = 29.5
        hot_dt = __import__("datetime").datetime(2026, 8, 3, 18, 30)
        hot_fs = rules.evaluate(hot, hot_dt)
        check("R7 comfort guardrail suppresses",
              not any(f.load_key.endswith("/ac") for f in hot_fs), "no A/C advice at 29.5C")

        # The veto must be VISIBLE, not merely effective — a suppressed finding
        # keeps its cost so the UI can show what the system declined to take.
        offered, vetoed = rules.evaluate_all(hot, hot_dt)
        ac_vetoed = [f for f in vetoed if f.load_key.endswith("/ac")]
        check("suppressed findings are returned for display", len(ac_vetoed) == 1,
              f"{len(vetoed)} vetoed, {len(offered)} offered")
        if ac_vetoed:
            v = ac_vetoed[0]
            check("suppressed finding keeps its cost and reason",
                  v.usd > 0 and "comfort" in v.suppressed_reason.lower(),
                  f"${v.usd:.3f} withheld")
        check("evaluate() still returns only actionable advice",
              all(not f.suppressed for f in offered) and len(offered) == len(hot_fs))
    except Exception as exc:
        check("rules", False, str(exc))

    try:
        import llm
        rec = llm.template_narrate(fs[0])
        check("template narration works with no LLM", bool(rec.title and rec.body))
        check("narration preserves computed numbers", abs(rec.usd - fs[0].usd) < 1e-9)
    except Exception as exc:
        check("llm template path", False, str(exc))

    print("\n2b. Actuation safety gate (Archetype E)")
    try:
        import server as _srv

        class _R:
            id, rule_name, room = "gate-test", "away_with_hvac_on", "living"
            usd = kwh = co2_kg = 1.0
            title = "t"

        dt = __import__("datetime").datetime(2026, 8, 3, 18, 30)
        hot = {"rooms": {"living": {"temp_c": 29.5}}}
        cool = {"rooms": {"living": {"temp_c": 23.0}}}
        cold = {"rooms": {"living": {"temp_c": 14.0}}}

        allowed_hot, reason = _srv._guardrail_allows(hot, _R(), "living/ac", "off", dt)
        check("guardrail REFUSES turning A/C off at 29.5C", not allowed_hot,
              (reason or "")[:60])
        allowed_cool, _ = _srv._guardrail_allows(cool, _R(), "living/ac", "off", dt)
        check("guardrail allows turning A/C off at 23.0C", allowed_cool)
        allowed_cold, _ = _srv._guardrail_allows(cold, _R(), "living/heater", "off", dt)
        check("guardrail REFUSES turning heater off at 14.0C", not allowed_cold)
        allowed_on, _ = _srv._guardrail_allows(hot, _R(), "living/ac", "on", dt)
        check("guardrail never blocks turning something ON", allowed_on)
        check("rule maps to the right load", _srv._load_from_rule(_R()) == "ac")
    except Exception as exc:
        check("actuation safety gate", False, str(exc))

    try:
        import cloud_report
        rep = cloud_report.generate_report({
            "total_watts": 1460, "tariff": {"rate": 0.58, "period": "on_peak"},
            "loads": {}, "recos": [{"rule_name": "away_with_hvac_on", "usd": 1.28,
                                    "kwh": 2.2, "co2_kg": 0.55}]})
        check("cloud report falls back deterministically",
              rep.get("generated_by") == "deterministic", rep.get("generated_by", "?"))
    except Exception as exc:
        check("cloud_report", False, str(exc))

    print("\n3. Live server")
    env = {**__import__("os").environ, "LLM_ENABLED": "0", "HTTP_PORT": str(PORT)}
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(HUB_DIR), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        up = False
        for _ in range(30):
            time.sleep(1)
            try:
                urllib.request.urlopen(BASE + "/api/state", timeout=2)
                up = True
                break
            except Exception:
                continue
        if not check("server starts and serves /api/state", up):
            return 1

        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            html = r.read().decode()
        check("dashboard served", "Energy Concierge" in html and "chart" in html)
        with urllib.request.urlopen(BASE + "/phone", timeout=5) as r:
            ph = r.read().decode()
        check("phone page served", "Energy Concierge" in ph)

        post("/api/sensor", {"room": "living", "occupancy": True, "lux": 120,
                             "temp_c": 23.4, "humidity": 46})
        post("/api/load", {"key": "living/ac", "state": "on", "watts": 1100})
        post("/api/load", {"key": "living/lights", "state": "on", "watts": 240})
        post("/api/presence", {"presence": "away", "distance_m": 2400})
        time.sleep(7)
        post("/api/sensor", {"room": "living", "occupancy": False, "lux": 110,
                             "temp_c": 23.6, "humidity": 47})
        time.sleep(8)

        state = get("/api/state")
        check("power history accumulating", len(state.get("power_history", [])) >= 2,
              f"{len(state.get('power_history', []))} samples")
        check("total watts computed", state.get("total_watts", 0) == 1340,
              f"{state.get('total_watts')} W")

        recos = get("/api/recos")
        check("recommendation generated end-to-end", len(recos) >= 1,
              recos[0]["title"] if recos else "none")
        if recos:
            check("recommendation carries an audit formula", bool(recos[0].get("formula")))
            check("narrated via template (LLM disabled)",
                  recos[0].get("narrated_by") == "template")

            # --- actuation loop, live over HTTP ---
            rid = recos[0]["id"]
            applied = post("/api/apply", {"reco_id": rid, "action": "off",
                                          "approved_by": "smoke_test"})
            check("/api/apply approves and commands", applied.get("ok") is True,
                  f"realized ${applied.get('realized_usd', 0):.4f}")

            after = get("/api/state")
            check("saving booked as realized", after.get("realized", {}).get("count", 0) >= 1,
                  f"${after.get('realized', {}).get('usd', 0):.4f}")
            check("finding marked applied", rid in (after.get("applied_ids") or []))
            lk = applied.get("load_key", "")
            check("load switched off by the command",
                  after.get("loads", {}).get(lk, {}).get("state") == "off", lk)

            try:
                post("/api/apply", {"reco_id": "does-not-exist"})
                check("unknown reco_id rejected", False, "expected a 404")
            except urllib.error.HTTPError as e:
                check("unknown reco_id rejected", e.code == 404, f"HTTP {e.code}")

        # --- the session recorder: audit trail and training substrate ---
        ds = get("/api/dataset")
        check("recorder is collecting", ds.get("enabled") is True and ds.get("rows", 0) > 0,
              f"{ds.get('rows')} rows, {ds.get('ticks')} ticks")
        if recos:
            fb = post("/api/feedback", {"reco_id": recos[0]["id"], "useful": True})
            check("feedback endpoint records a label",
                  fb.get("dataset", {}).get("feedback", 0) >= 1)
        try:
            post("/api/feedback", {"reco_id": "x"})
            check("feedback without a verdict rejected", False, "expected a 400")
        except urllib.error.HTTPError as e:
            check("feedback without a verdict rejected", e.code == 400, f"HTTP {e.code}")

        rep = post("/api/deep_report", {})
        check("deep report endpoint returns", bool(rep.get("summary")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 70)
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\n  FAILURES:")
        for _, name, detail in failed:
            print(f"    - {name} {detail}")
    print("=" * 70 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
