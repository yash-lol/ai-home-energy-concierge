"""Tier 2: plan synthesis on the NPU.

WHAT CHANGED AND WHY IT IS CHEAPER
    Before: N findings -> N LLM calls, each rewording one finding in isolation.
    After:  N findings -> ONE call returning a ranked plan with reasoning, and
            only when the finding set actually changes.

    So this does strictly more while costing strictly less: cross-finding
    reasoning that per-finding narration cannot express, at one call per change
    instead of N calls per cycle.

WHAT THE MODEL DECIDES, AND WHAT IT MUST NOT
    Decides : the ORDER to act in, which findings interact, what can wait and
              why, and how to explain a guardrail refusal in plain language.
    Must not: arithmetic. Every number on every Recommendation is copied from
              the deterministic Finding, never from the model. The model may
              reference a figure it was given; it may not compute, restate
              differently, or round one.

    That split is the whole honesty story: ranking is a judgement call and the
    model is good at it; the dollars are arithmetic and Python owns them.

LATENCY
    Measured on this machine (GenieX / Qwen3-4B W4A16, Hexagon NPU): latency
    tracks OUTPUT length steeply — 135 chars 2.6 s, 1060 chars 11.4 s, and a
    600-token reply 135 s. The prompt below is therefore aggressively terse by
    design: short fields, capped plan length, low max_tokens. Verbosity here is
    not a style preference, it is seconds on stage.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

import llm as llm_mod

AI_PLAN = os.environ.get("AI_PLAN", "0") == "1"

# Ranking more than this in one call is not useful to a homeowner and costs
# output tokens, which is what costs seconds.
MAX_PLAN_ITEMS = int(os.environ.get("PLAN_MAX_ITEMS", "4"))
PLAN_MAX_TOKENS = int(os.environ.get("PLAN_MAX_TOKENS", "320"))
PLAN_TIMEOUT_S = int(os.environ.get("PLAN_TIMEOUT_S", "30"))

# The planner may run a DIFFERENT, smaller model than narration.
#
# WHY THIS KNOB EXISTS
#     Plan synthesis is measured at ~11.5 s and is by far the slowest thing in
#     the system — the rules engine is 30 µs. Latency here tracks OUTPUT LENGTH,
#     and the plan is the longest output we ask for (ranked items, reasons,
#     deferrals), so it pays the steepest price for model size.
#
#     Ranking three findings and writing one short sentence each is a much
#     easier job than narration: the numbers are given, the schema is fixed, and
#     anything malformed is caught. GenieX publishes Qwen3.5-0.8B and 2B
#     alongside the 4B, so the planner can be right-sized independently.
#
# WHY IT IS SAFE TO TRY
#     A weaker model fails in exactly the two ways this file already handles:
#     unusable JSON (-> _validate drops it, then deterministic_plan) and a bad
#     number (-> the P1-C provenance verifier flags it). It cannot produce a
#     wrong dollar figure on screen, because it never owns one.
#
# Defaults to the narration model, so behaviour is unchanged until set.
#     PLAN_MODEL=ai-hub-models/Qwen3.5-0.8B  python hub/server.py
PLAN_MODEL = os.environ.get("PLAN_MODEL", "") or None
PLAN_BASE_URL = os.environ.get("PLAN_BASE_URL", "") or None


SYSTEM_PROMPT = """You are the reasoning layer of a home energy system.

You are given detected findings, each with PRE-COMPUTED numbers, plus a learned
anomaly signal. Produce a PLAN: order the findings by what the homeowner should
actually do first, and say why.

CRITICAL RULES:
- Do NOT do arithmetic. Do NOT invent, restate differently, or round any number.
  Use figures exactly as given, or omit them.
- Ranking is your decision. Numbers are not.
- If a finding is legitimate use whose only problem is timing, say so: recommend
  deferring it, not stopping it.
- If the comfort guardrail suppressed something, explain the tradeoff in one
  sentence.

Reply with ONE JSON object, no markdown, no commentary:
{"situation":"<one sentence>",
 "plan":[{"finding_id":"<id from input>","rank":<1 = do first>,
          "why_this_order":"<one short sentence>","action":"<under 10 words>"}],
 "deferred":[{"finding_id":"<id>","reason":"<why it can wait>"}],
 "anomaly_note":"<one sentence about the learned signal, or empty string>"}

BE BRIEF. Every string under 15 words. Output length is what makes this slow."""


@dataclass
class Plan:
    """A ranked plan over the current finding set."""
    situation: str = ""
    items: List[Dict] = field(default_factory=list)     # ranked, ids validated
    deferred: List[Dict] = field(default_factory=list)
    anomaly_note: str = ""
    planned_by: str = "template"                        # "llm" | "template"
    latency_s: float = 0.0
    dropped_ids: List[str] = field(default_factory=list)
    provenance: str = "unchecked"                       # set by P1-C verifier
    # WHICH model produced this. Empty on the deterministic path. Without it a
    # before/after latency comparison is unfalsifiable: a plan that silently
    # fell back to the template is indistinguishable from a fast small model,
    # and "we made the planner 5x faster" would be reporting a broken endpoint
    # as a win. This project has already been bitten once by exactly that —
    # narration fell back to templates every call while the README quoted an
    # NPU figure.
    model: str = ""

    def to_dict(self) -> Dict:
        return {
            "situation": self.situation,
            "plan": self.items,
            "deferred": self.deferred,
            "anomaly_note": self.anomaly_note,
            "planned_by": self.planned_by,
            "latency_s": round(self.latency_s, 3),
            "model": self.model,
            "dropped_ids": self.dropped_ids,
            "provenance": self.provenance,
        }


def _finding_line(f) -> str:
    """One compact line per finding. Terse on purpose — prompt length costs less
    than output length, but neither is free."""
    bits = [f"id={f.id}", f"sev={f.severity}", f"load={f.load_key}"]
    if getattr(f, "estimate", None):
        bits.append(f"usd={f.usd:.3f}")
        bits.append(f"kwh={f.estimate.kwh:.4f}")
    if getattr(f, "detector", "rule") == "learned":
        bits.append(f"detector=learned score={getattr(f, 'anomaly_score', '?')}")
    bits.append(f"what={f.headline}")
    return "  ".join(bits)


def _user_prompt(findings: List, suppressed: Optional[List] = None) -> str:
    lines = [_finding_line(f) for f in findings[:MAX_PLAN_ITEMS]]
    out = "FINDINGS:\n" + "\n".join(lines)
    if suppressed:
        out += ("\n\nSUPPRESSED BY THE COMFORT GUARDRAIL (do not rank these; "
                "explain the tradeoff in `situation` if relevant):\n"
                + "\n".join(f"  {s}" for s in suppressed))
    return out


def allowed_numbers(findings: List) -> Dict[str, str]:
    """Every figure the model was given, for the P1-C provenance verifier."""
    allowed: Dict[str, str] = {}
    for f in findings:
        est = getattr(f, "estimate", None)
        if est:
            allowed[f"{f.id}.usd"] = f"{f.usd:.3f}"
            allowed[f"{f.id}.kwh"] = f"{est.kwh:.4f}"
            allowed[f"{f.id}.co2"] = f"{est.co2_kg:.4f}"
            allowed[f"{f.id}.watts"] = f"{est.watts:.1f}"
            allowed[f"{f.id}.rate"] = f"{est.rate_used:.4f}"
        if getattr(f, "anomaly_score", None) is not None:
            allowed[f"{f.id}.score"] = f"{f.anomaly_score}"
    return allowed


def deterministic_plan(findings: List, suppressed: Optional[List] = None) -> Plan:
    """Today's behaviour, and the fallback: rank by dollar value descending.

    This must always produce something sane, because it is what the demo runs on
    when GenieX is dead. It is not a stub.
    """
    ordered = sorted(findings, key=lambda f: f.usd, reverse=True)[:MAX_PLAN_ITEMS]
    items = []
    for i, f in enumerate(ordered, start=1):
        items.append({
            "finding_id": f.id,
            "rank": i,
            "why_this_order": ("largest measured saving"
                               if i == 1 else "ranked by measured saving"),
            "action": (f.suggested_actions[0] if f.suggested_actions
                       else f"Turn off the {f.load_key.split('/')[-1]}"),
        })
    learned = [f for f in findings if getattr(f, "detector", "rule") == "learned"]
    note = ""
    if learned:
        note = (f"Learned detector flagged {learned[0].load_key} "
                f"(score {learned[0].anomaly_score}).")
    return Plan(
        situation=(f"{len(findings)} finding(s) detected; ranked by measured cost."
                   if findings else "Nothing wasteful detected right now."),
        items=items,
        deferred=[],
        anomaly_note=note,
        planned_by="template",
    )


def _extract_json(raw: str) -> Dict:
    """Tolerate a model that wraps JSON in prose or fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, depth = None, 0
    for i, ch in enumerate(raw):
        if ch == "{":
            if start is None:
                start = i
            depth += 1
        elif ch == "}" and start is not None:
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i + 1])
    raise ValueError("no JSON object in model reply")


def _validate(parsed: Dict, findings: List) -> Tuple[List[Dict], List[Dict], List[str]]:
    """Keep only plan entries whose finding_id actually exists.

    A model that invents an id would otherwise put a card on screen for a
    finding that does not exist — the plan calls this out as non-negotiable, and
    it is the single most likely way this feature could lie.
    """
    known = {f.id for f in findings}
    items, dropped = [], []

    for entry in (parsed.get("plan") or []):
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("finding_id", ""))
        if fid not in known:
            dropped.append(fid)
            continue
        try:
            rank = int(entry.get("rank", len(items) + 1))
        except (TypeError, ValueError):
            rank = len(items) + 1
        items.append({
            "finding_id": fid,
            "rank": rank,
            "why_this_order": str(entry.get("why_this_order", ""))[:160],
            "action": str(entry.get("action", ""))[:80],
        })

    items.sort(key=lambda e: e["rank"])
    for i, e in enumerate(items, start=1):
        e["rank"] = i

    deferred = []
    for entry in (parsed.get("deferred") or []):
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("finding_id", ""))
        if fid not in known:
            dropped.append(fid)
            continue
        deferred.append({"finding_id": fid,
                         "reason": str(entry.get("reason", ""))[:160]})

    return items, deferred, dropped


class Planner:
    """One LLM call per CHANGE of the finding set, cached by frozenset of ids."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 timeout: int = PLAN_TIMEOUT_S):
        self.base_url = (base_url or llm_mod.LLM_BASE_URL).rstrip("/")
        self.model = model or llm_mod.LLM_MODEL
        self.timeout = timeout
        self._cache_key: Optional[frozenset] = None
        self._cache: Optional[Plan] = None
        self.violations = 0          # provenance failures seen, for the deck

    def _chat(self, system: str, user: str) -> str:
        if requests is None:
            raise RuntimeError("requests not installed")
        headers = {"Content-Type": "application/json"}
        if llm_mod.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {llm_mod.LLM_API_KEY}"
        resp = requests.post(
            f"{self.base_url}/chat/completions", headers=headers,
            json={"model": self.model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": 0.3, "max_tokens": PLAN_MAX_TOKENS},
            timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def plan(self, findings: List, suppressed: Optional[List] = None,
             use_cache: bool = True) -> Plan:
        """Ranked plan for this finding set. Falls back deterministically."""
        if not findings:
            return deterministic_plan(findings, suppressed)

        # The suppressed set is part of the key, not just the offered one. The
        # prompt asks the model to explain a guardrail tradeoff, so a plan built
        # when nothing was vetoed is stale the moment something is — same
        # findings, different correct answer. Keying on findings alone would
        # serve that stale plan for as long as the finding set held steady,
        # which across a demo beat is exactly when the veto appears.
        key = (frozenset(f.id for f in findings), frozenset(suppressed or ()))
        if use_cache and self._cache is not None and key == self._cache_key:
            return self._cache

        plan = self._plan_uncached(findings, suppressed)
        self._cache_key, self._cache = key, plan
        return plan

    def _plan_uncached(self, findings: List, suppressed: Optional[List]) -> Plan:
        if not llm_mod.LLM_ENABLED:
            return deterministic_plan(findings, suppressed)

        t0 = time.time()
        try:
            raw = self._chat(SYSTEM_PROMPT, _user_prompt(findings, suppressed))
            parsed = _extract_json(raw)
            items, deferred, dropped = _validate(parsed, findings)
            if not items:
                raise ValueError("no usable plan entries survived validation")

            plan = Plan(
                situation=str(parsed.get("situation", ""))[:200],
                items=items,
                deferred=deferred,
                anomaly_note=str(parsed.get("anomaly_note", ""))[:200],
                planned_by="llm",
                latency_s=time.time() - t0,
                dropped_ids=dropped,
                model=self.model,
            )
            if dropped:
                print(f"[planner] dropped unknown finding ids: {dropped}")

            # P1-C: verify the model did not emit a number it was never given.
            try:
                import provenance
                ok, unverified = provenance.verify(
                    " ".join([plan.situation, plan.anomaly_note]
                             + [i["why_this_order"] + " " + i["action"]
                                for i in plan.items]),
                    allowed_numbers(findings))
                plan.provenance = "verified" if ok else "unverified"
                if not ok:
                    self.violations += 1
                    print(f"[planner] PROVENANCE FAIL — numbers not in source: "
                          f"{unverified}")
            except ImportError:
                pass

            return plan

        except Exception as exc:
            print(f"[planner] falling back to deterministic plan "
                  f"({type(exc).__name__}: {exc})")
            fb = deterministic_plan(findings, suppressed)
            fb.latency_s = time.time() - t0
            return fb


PLANNER = Planner(base_url=PLAN_BASE_URL, model=PLAN_MODEL)


# --------------------------------------------------------------------------
# Self-test:  python hub/planner.py        (LLM path, if GenieX is up)
#             LLM_ENABLED=0 python hub/planner.py   (deterministic fallback)
# --------------------------------------------------------------------------

def _fixtures():
    from datetime import datetime
    from rules import Finding
    from energy_model import waste_estimate
    now = datetime(2026, 8, 6, 18, 30)

    def mk(fid, rule, sev, load, key, secs, headline, actions, **kw):
        f = Finding(id=fid, rule_name=rule, severity=sev, room="living",
                    load_key=load, seconds_wasted=secs, headline=headline,
                    evidence=[], suggested_actions=actions, **kw)
        f.estimate = waste_estimate(key, secs, now)
        return f

    return [
        mk("r2-living-ac", "away_with_hvac_on", "critical", "living/ac",
           "window_ac", 1800, "A/C cooling an empty home", ["Turn off the ac"]),
        mk("r6-living-dryer", "peak_hour_heavy_load", "warning", "living/dryer",
           "clothes_dryer", 1500, "Dryer running in the peak window",
           ["Delay this cycle until 9 PM"]),
        mk("r1-living-lights", "unoccupied_lights_on", "serious", "living/lights",
           "incandescent_set", 1500, "Lights on in an empty room",
           ["Turn off the lights"]),
    ]


if __name__ == "__main__":
    findings = _fixtures()

    print("=" * 78)
    print("PLAN SYNTHESIS — self-test")
    print(f"LLM_ENABLED={llm_mod.LLM_ENABLED}  endpoint={llm_mod.LLM_BASE_URL}")
    print("=" * 78)

    for label, use_llm in (("LLM PATH", True), ("DETERMINISTIC FALLBACK", False)):
        saved = llm_mod.LLM_ENABLED
        llm_mod.LLM_ENABLED = saved and use_llm
        p = Planner().plan(findings, use_cache=False)
        llm_mod.LLM_ENABLED = saved

        print(f"\n--- {label} ---")
        print(f"  planned_by : {p.planned_by}   latency {p.latency_s:.2f}s"
              f"   provenance {p.provenance}")
        print(f"  situation  : {p.situation}")
        for it in p.items:
            print(f"    {it['rank']}. {it['finding_id']:18} {it['action']}")
            print(f"       why: {it['why_this_order']}")
        for d in p.deferred:
            print(f"    defer {d['finding_id']}: {d['reason']}")
        if p.anomaly_note:
            print(f"  anomaly    : {p.anomaly_note}")
        if p.dropped_ids:
            print(f"  DROPPED unknown ids: {p.dropped_ids}")

    print("\n" + "=" * 78)
    print("Both paths produced a usable plan." )
    print("=" * 78)
