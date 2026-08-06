"""LLM narration layer.

Turns a Finding (which already carries computed numbers) into friendly natural
language. The LLM NEVER computes or alters a number — after every call, the
numeric fields are overwritten from the Finding. If the model's prose contradicts
the computed figure, the computed figure wins.

The deterministic `template_narrate()` fallback is not a nicety: the demo must
work with the LLM process killed. Set LLM_ENABLED=0 to force it.

RUNTIME: defaults target **Qualcomm GenieX**, which serves an OpenAI-compatible API
on port 18181 and can execute on the Hexagon NPU:

    geniex pull ai-hub-models/Qwen3-4B-Instruct-2507
    geniex serve                       # -> http://127.0.0.1:18181/v1

Any other OpenAI-compatible server works too — only LLM_BASE_URL changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import List, Optional

try:
    import requests
except ImportError:  # keep the module importable without the dep
    requests = None

# GenieX's default port. Override for any other OpenAI-compatible endpoint.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:18181/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "ai-hub-models/Qwen3-4B-Instruct-2507")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_ENABLED = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False")

# Measured on this machine's GenieX / Qwen3-4B W4A16 on the Hexagon NPU:
# latency tracks OUTPUT LENGTH, steeply.
#
#     135 chars ->  2.6 s        378 chars ->  5.5 s
#    1060 chars -> 11.4 s       ~600 tokens -> ~135 s
#
# The old settings (max_tokens=300, no brevity instruction) let the model ramble
# to ~1060 chars, so every narration took ~11.4 s and blew the 8 s timeout — it
# fell back to the template EVERY time, silently. The NPU was never actually
# narrating anything, while README quoted 3110 ms.
#
# So: cap the output and ask for brevity, which is what actually buys the
# latency, and give the timeout real headroom (~3x the p50) so a slow call
# completes instead of being abandoned mid-generation. The template fallback is
# unchanged and still catches a genuinely dead endpoint.
LLM_TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "20"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "160"))

MAX_TITLE_CHARS = 60
MAX_ACTIONS = 3

SEVERITY_ICON = {
    "critical": "!!",
    "serious": "!",
    "warning": "~",
    "good": "+",
}


@dataclass
class Recommendation:
    """What the user sees. Numbers here are authoritative, copied from the Finding."""

    id: str
    severity: str
    title: str
    body: str
    actions: List[str]
    kwh: float
    usd: float
    co2_kg: float
    room: str
    rule_name: str
    formula: str
    evidence: List[str] = field(default_factory=list)
    source: str = ""
    narrated_by: str = "template"   # "llm" or "template" — logged and shown in debug
    # "detected" = already spent, "anticipated" = still avoidable. Carried through
    # to the UI because a projected figure must never be shown as an incurred one.
    kind: str = "detected"
    # Carried through from the Finding so the UI can separate a deterministic
    # rule from a learned detection. Without this the distinction dies at the
    # narration boundary: the Finding knows, the Recommendation does not, and
    # every card on screen looks equally rule-derived. Preserving "every
    # recommendation traces to a named rule" depends on being able to show which
    # ones do not.
    detector: str = "rule"          # "rule" (R1-R6) or "learned"
    anomaly_score: Optional[float] = None
    # The load this concerns, carried from the Finding. server.py historically
    # re-derived it from rule_name via a lookup table; anything not in that table
    # silently became "lights". A learned finding is not in that table.
    load_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


SYSTEM_PROMPT = """You are a home energy assistant. You write short, friendly, \
practical advice for a homeowner.

CRITICAL RULE: You must NOT perform any arithmetic and must NOT invent any number. \
All energy, cost, and carbon figures are given to you already computed. Use them \
exactly as provided, or omit them. Never recalculate, never round differently, \
never estimate.

Reply with a single JSON object and nothing else:
{"title": "<max 60 chars>", "body": "<at most 2 sentences, second person, friendly>", \
"actions": ["<imperative, under 8 words>", ...]}

At most 3 actions. No markdown, no code fences, no commentary outside the JSON.

BE BRIEF. Keep the whole reply under 350 characters. Output length, not model \
size, is what makes this slow on the NPU that generates it, so every extra word \
costs real latency. Body: at most 2 short sentences. Do not repeat a number you \
were given more than once."""


def _user_prompt(finding) -> str:
    est = finding.estimate
    evidence = "\n".join(f"- {e}" for e in finding.evidence)
    # An anticipated finding has not happened yet. Telling the model "a waste
    # condition was detected" would have it write about money already spent, and
    # the whole value of warning early is that the money is still in your pocket.
    anticipated = getattr(finding, "kind", "detected") == "anticipated"
    opening = ("A waste condition is ABOUT TO HAPPEN in the home and can still be "
               "avoided. Nothing has been spent yet — write about preventing it, "
               "in the future tense. Never say it has already cost anything."
               if anticipated else "A waste condition was detected in the home.")
    return f"""{opening}

WHAT THE SENSORS AND RULES FOUND:
{evidence}

ROOM: {finding.room}
APPLIANCE: {est.load_label} ({est.watts:.0f} W)
DURATION: {finding.seconds_wasted/60:.0f} minutes{" (PROJECTED, not observed)" if anticipated else ""}
SEVERITY: {finding.severity}

PRE-COMPUTED FIGURES — use verbatim, do not recalculate:
- energy {"at stake" if anticipated else "wasted"}: {est.kwh:.3f} kWh
- cost {"still avoidable" if anticipated else "already incurred"}: ${est.usd:.2f}
- carbon: {est.co2_kg:.2f} kg CO2
- current tariff period: {est.period_label} at ${est.rate_used:.2f} per kWh

SUGGESTED ACTIONS (rephrase naturally, keep the same meaning):
{', '.join(finding.suggested_actions)}

Write the JSON object now."""


class LLMClient:
    """Minimal OpenAI-compatible chat client with a hard timeout."""

    def __init__(self, base_url: str = LLM_BASE_URL, model: str = LLM_MODEL,
                 api_key: str = LLM_API_KEY, timeout: int = LLM_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _chat(self, system: str, user: str) -> str:
        if requests is None:
            raise RuntimeError("requests not installed")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "max_tokens": LLM_MAX_TOKENS,
        }
        resp = requests.post(f"{self.base_url}/chat/completions", headers=headers,
                             json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def narrate(self, finding) -> Recommendation:
        """Narrate a finding, falling back to the template on ANY failure."""
        if not LLM_ENABLED:
            return template_narrate(finding)

        try:
            raw = self._chat(SYSTEM_PROMPT, _user_prompt(finding))
            parsed = _extract_json(raw)
            rec = _validate(parsed, finding)
            rec.narrated_by = "llm"
            return rec
        except Exception as exc:
            print(f"[llm] falling back to template ({type(exc).__name__}: {exc})")
            return template_narrate(finding)


def _extract_json(raw: str) -> dict:
    """Pull a JSON object out of a model response, tolerating code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    return json.loads(text[start:end + 1])


def _validate(parsed: dict, finding) -> Recommendation:
    """Enforce the schema, then overwrite every number from the Finding."""
    for key in ("title", "body", "actions"):
        if key not in parsed:
            raise ValueError(f"missing key {key!r}")

    title = str(parsed["title"]).strip()[:MAX_TITLE_CHARS]
    body = str(parsed["body"]).strip()
    actions = [str(a).strip() for a in parsed["actions"]][:MAX_ACTIONS]

    if not title or not body:
        raise ValueError("empty title or body")
    if not actions:
        actions = list(finding.suggested_actions)[:MAX_ACTIONS]

    est = finding.estimate
    # Numbers come from the Finding, never from the model.
    return Recommendation(
        id=finding.id,
        severity=finding.severity,
        title=title,
        body=body,
        actions=actions,
        kwh=est.kwh,
        usd=est.usd,
        co2_kg=est.co2_kg,
        room=finding.room,
        rule_name=finding.rule_name,
        formula=est.formula,
        evidence=list(finding.evidence),
        source=est.source,
        kind=getattr(finding, "kind", "detected"),
        detector=getattr(finding, "detector", "rule"),
        anomaly_score=getattr(finding, "anomaly_score", None),
        load_key=getattr(finding, "load_key", ""),
    )


def template_narrate(finding) -> Recommendation:
    """Deterministic narration — no LLM, no network, never fails.

    Quality matters here: this is what the judges may actually see if the local
    model is unavailable on demo day.
    """
    est = finding.estimate
    mins = finding.seconds_wasted / 60.0
    room = finding.room
    load = est.load_label.lower()
    peak_note = " during expensive peak hours" if est.period_label == "on_peak" else ""

    if finding.rule_name == "unoccupied_lights_on":
        title = f"Lights left on in the empty {room} room"
        body = (f"Your {room} room has been empty for {mins:.0f} minutes but the {load} "
                f"is still on{peak_note}. Switching it off now saves about ${est.usd:.2f}.")
    elif finding.rule_name == "away_with_hvac_on":
        title = f"Cooling an empty home"
        body = (f"You are away, but the {load} has been running for {mins:.0f} minutes"
                f"{peak_note}. That is roughly ${est.usd:.2f} and {est.co2_kg:.2f} kg of CO2 "
                f"for an empty room.")
    elif finding.rule_name == "daylight_waste":
        title = f"Daylight is doing the job already"
        body = (f"It is bright enough in the {room} room that the {load} is not adding much. "
                f"Turning it off saves about ${est.usd:.2f}.")
    elif finding.rule_name == "hvac_with_window_open":
        title = "Your A/C may be cooling the outdoors"
        body = (f"The {load} has run {mins:.0f} minutes without the room getting cooler, which "
                f"usually means a window or door is open. You have spent about ${est.usd:.2f} so far.")
    elif finding.rule_name == "phantom_standby":
        title = "Phantom power while you are out"
        body = (f"Idle devices have drawn {est.kwh:.2f} kWh over {mins/60:.1f} hours away. "
                f"It is only ${est.usd:.2f} now, but it never stops.")
    elif finding.rule_name == "peak_window_imminent":
        # Future tense throughout. This is the one card that describes money the
        # user still has, so it must never read like a bill.
        title = "Shift this before the peak rate starts"
        body = (f"The {load} is running and the expensive 4-9 PM window is about to "
                f"open. Delaying it until after 9 PM would avoid roughly "
                f"${est.usd:.2f} — nothing has been spent yet.")
    elif finding.rule_name == "peak_hour_heavy_load":
        title = "Shift this load out of peak hours"
        body = (f"The {load} is running during the 4-9 PM peak window, when power costs "
                f"${est.rate_used:.2f} per kWh. Delaying it until after 9 PM would save "
                f"about ${est.usd:.2f}.")
    else:
        title = finding.headline[:MAX_TITLE_CHARS]
        body = f"{finding.headline}. Estimated cost so far: ${est.usd:.2f}."

    return Recommendation(
        id=finding.id,
        severity=finding.severity,
        title=title[:MAX_TITLE_CHARS],
        body=body,
        actions=list(finding.suggested_actions)[:MAX_ACTIONS],
        kwh=est.kwh,
        usd=est.usd,
        co2_kg=est.co2_kg,
        room=finding.room,
        rule_name=finding.rule_name,
        formula=est.formula,
        evidence=list(finding.evidence),
        source=est.source,
        narrated_by="template",
        kind=getattr(finding, "kind", "detected"),
        detector=getattr(finding, "detector", "rule"),
        anomaly_score=getattr(finding, "anomaly_score", None),
        load_key=getattr(finding, "load_key", ""),
    )


# --------------------------------------------------------------------------
# Self-test — compare LLM vs template output side by side
# --------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime

    import rules

    T = 1754240000
    EVENING = datetime(2026, 8, 3, 18, 30)

    snap = {
        "rooms": {"living": {"occupancy": False, "lux": 120, "temp_c": 23.0, "humidity": 45,
                             "last_occupied_ts": T - 1500, "ts": T, "temp_drop_c": 1.0}},
        "loads": {
            "living/lights": {"state": "on", "watts": 240, "ts": T},
            "living/ac": {"state": "on", "watts": 1100, "ts": T, "on_since": T - 1800},
            "living/dryer": {"state": "on", "watts": 3000, "ts": T, "on_since": T - 1800},
        },
        "user": {"presence": "away", "distance_m": 2400, "battery": 72, "ts": T - 1800},
        "now": T,
    }

    findings = rules.evaluate(snap, EVENING)
    client = LLMClient()

    print("\n" + "=" * 92)
    print("NARRATION SELF-TEST — template path (always available)")
    print("=" * 92)
    for f in findings:
        rec = template_narrate(f)
        print(f"\n[{rec.severity}] {rec.title}")
        print(f"  {rec.body}")
        print(f"  actions: {', '.join(rec.actions)}")
        print(f"  ${rec.usd:.3f} | {rec.kwh:.4f} kWh | audit: {rec.formula[:70]}...")

    print("\n" + "=" * 92)
    print(f"NARRATION SELF-TEST — LLM path (endpoint {LLM_BASE_URL})")
    print("=" * 92)
    for f in findings:
        rec = client.narrate(f)
        print(f"\n[{rec.severity}] ({rec.narrated_by}) {rec.title}")
        print(f"  {rec.body}")
        print(f"  actions: {', '.join(rec.actions)}")
    print("\n" + "=" * 92 + "\n")
