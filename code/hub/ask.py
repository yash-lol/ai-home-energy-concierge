"""Tier 3: natural-language Q&A over the current home state, on the NPU.

WHY THIS EXISTS
    A plan list is something a judge reads. A question they typed themselves,
    answered on-device in a couple of seconds, is something they remember — and
    during the gallery walk it is the clearest available proof that a real model
    is running locally rather than a template pretending to be one.

THE HONESTY MECHANISM
    The model is given ONLY a digest computed in Python (cloud_report.build_digest)
    and may cite only numbers from it. Every answer then goes through the P1-C
    provenance verifier, and the page shows a verified / unverified badge. That
    badge is the demo moment: it is a mechanical check, not a promise.

LATENCY
    Measured here: first token 0.15 s, full answer ~2.5 s, because latency tracks
    OUTPUT length and the prompt demands brevity. Streaming is therefore worth
    real UX: the answer starts appearing essentially immediately.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Generator, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

import llm as llm_mod
from cloud_report import build_digest

AI_ASK = os.environ.get("AI_ASK", "0") == "1"

ASK_MAX_TOKENS = int(os.environ.get("ASK_MAX_TOKENS", "180"))
ASK_TIMEOUT_S = int(os.environ.get("ASK_TIMEOUT_S", "30"))

SUGGESTED_QUESTIONS = [
    "Why is my bill high?",
    "What should I do first?",
    "What if I shift the dryer to 9 PM?",
    "Is anything unusual right now?",     # exercises the tier-1 anomaly signal
]

SYSTEM_PROMPT = """You answer questions about a home's live energy state.

You are given a DIGEST computed in Python. Every number you may use is in it.

RULES:
- Do NOT do arithmetic. Do NOT invent, restate differently, or round any number.
  Cite figures exactly as they appear in the digest, or omit them.
- If the digest does not contain what was asked, say so plainly. Do not guess.
- Answer in at most 3 short sentences. Be direct and practical.
- Plain prose. No markdown, no JSON, no preamble."""


def _digest_lines(state: Dict) -> Tuple[str, Dict[str, str]]:
    """(prompt text, allowed-number map for the provenance verifier).

    The allowed map is built from the SAME figures put in the prompt, so the
    verifier and the model see exactly one source of truth. Building them apart
    would let the two drift and make the badge meaningless.
    """
    d = build_digest(state)
    allowed: Dict[str, str] = {}
    lines: List[str] = []

    # Keys are build_digest's own: total_wasted_usd / _kwh / _co2_kg, etc.
    for key, label, fmt in (("total_wasted_usd", "avoidable $", "{:.3f}"),
                            ("total_wasted_kwh", "kWh", "{:.4f}"),
                            ("total_wasted_co2_kg", "kg CO2", "{:.4f}")):
        if d.get(key) is not None:
            allowed[key] = f"{d[key]}"
    lines.append(f"TOTALS: avoidable ${d.get('total_wasted_usd', 0):.3f}, "
                 f"{d.get('total_wasted_kwh', 0):.4f} kWh, "
                 f"{d.get('total_wasted_co2_kg', 0):.4f} kg CO2")

    tariff = state.get("tariff") or {}
    tn = d.get("tariff_now") or {}
    rate = tn.get("rate", tariff.get("rate"))
    if rate is not None:
        allowed["tariff.rate"] = f"{rate}"
        lines.append(f"TARIFF NOW: ${rate}/kWh "
                     f"({tn.get('period', tariff.get('period',''))}) "
                     f"at {tariff.get('clock','')}")
    # Both rates, so "what if I move it to 9 PM?" can be answered from the digest
    # instead of the model reasoning that the off-peak figure "is not specified".
    for k in ("on_peak_rate", "off_peak_rate", "co2_kg_per_kwh"):
        if d.get(k) is not None:
            allowed[k] = f"{d[k]}"
            lines.append(f"{k.upper()}: {d[k]}")

    tw = d.get("current_watts", state.get("total_watts"))
    if tw is not None:
        allowed["total_watts"] = f"{tw}"
        lines.append(f"DRAWING NOW: {tw} W")

    for name, room in (state.get("rooms") or {}).items():
        bits = []
        for k, label in (("temp_c", "C"), ("humidity", "% RH"), ("lux", " lux")):
            if room.get(k) is not None:
                allowed[f"{name}.{k}"] = f"{room[k]}"
                bits.append(f"{room[k]}{label}")
        bits.append("occupied" if room.get("occupancy") else "empty")
        if room.get("temp_src"):
            bits.append(f"temp source={room['temp_src']}")
        lines.append(f"ROOM {name}: " + ", ".join(bits))

    for key, load in (state.get("loads") or {}).items():
        w = load.get("watts")
        if w is not None:
            allowed[f"{key}.watts"] = f"{w}"
        lines.append(f"LOAD {key}: {load.get('state')} at {w} W"
                     + (" (real metered device)" if load.get("metered")
                        else " (simulated)"))

    for r in (state.get("recos") or [])[:6]:
        rid = r.get("id", "?")
        if r.get("usd") is not None:
            allowed[f"{rid}.usd"] = f"{r['usd']}"
        if r.get("kwh") is not None:
            allowed[f"{rid}.kwh"] = f"{r['kwh']}"
        det = r.get("detector", "rule")
        if r.get("anomaly_score") is not None:
            allowed[f"{rid}.score"] = f"{r['anomaly_score']}"
        lines.append(f"FINDING {rid} [{r.get('severity','')}, detector={det}]: "
                     f"{r.get('title','')} — ${r.get('usd',0)}, {r.get('kwh',0)} kWh"
                     + (f", anomaly score {r['anomaly_score']}"
                        if r.get("anomaly_score") is not None else ""))

    plan = state.get("plan") or {}
    if plan.get("situation"):
        lines.append(f"PLAN SITUATION: {plan['situation']}")

    user = state.get("user") or {}
    if user.get("presence"):
        lines.append(f"USER: presence {user['presence']}")

    if not lines:
        lines.append("No findings and no live device state.")

    return "\n".join(lines), allowed


def deterministic_answer(question: str, state: Dict) -> str:
    """Answer from the digest with no model at all.

    The fallback must be genuinely useful, not an apology: with GenieX dead the
    demo still has to answer the question, just less fluently.
    """
    d = build_digest(state)
    total_usd = d.get("total_wasted_usd", 0.0)
    total_kwh = d.get("total_wasted_kwh", 0.0)
    recos = state.get("recos") or []
    q = (question or "").lower()

    if "unusual" in q or "anomal" in q or "strange" in q:
        learned = [r for r in recos if r.get("detector") == "learned"]
        if learned:
            r = learned[0]
            return (f"Yes — the learned detector flagged {r.get('title','a load')} "
                    f"(score {r.get('anomaly_score')}). It is a learned score on "
                    f"SIMULATED training data, not a measurement.")
        return "Nothing is scoring anomalous right now."

    if "first" in q or "should i do" in q:
        if recos:
            r = max(recos, key=lambda x: x.get("usd", 0))
            # Format explicitly: a raw float renders as $0.0004174503347608778,
            # which reads as a bug to anyone looking at the screen.
            return (f"Start with: {r.get('title','the largest finding')} "
                    f"— ${float(r.get('usd', 0) or 0):.3f} avoidable.")
        return "Nothing needs action right now."

    if "bill" in q or "high" in q or "why" in q:
        if recos:
            return (f"There is ${total_usd:.3f} of avoidable waste right now "
                    f"across {len(recos)} finding(s), {total_kwh:.4f} kWh.")
        return "No avoidable waste is detected right now."

    if recos:
        return (f"{len(recos)} finding(s) are active, "
                f"${total_usd:.3f} avoidable in total.")
    return "Nothing wasteful is detected right now."


class Asker:
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = (base_url or llm_mod.LLM_BASE_URL).rstrip("/")
        self.model = model or llm_mod.LLM_MODEL
        self.violations = 0

    def _payload(self, question: str, digest_text: str, stream: bool) -> Dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"DIGEST:\n{digest_text}\n\n"
                                            f"QUESTION: {question}"},
            ],
            "temperature": 0.3,
            "max_tokens": ASK_MAX_TOKENS,
            "stream": stream,
        }

    def _headers(self) -> Dict:
        h = {"Content-Type": "application/json"}
        if llm_mod.LLM_API_KEY:
            h["Authorization"] = f"Bearer {llm_mod.LLM_API_KEY}"
        return h

    def ask(self, question: str, state: Dict) -> Dict:
        """Non-streaming answer + provenance verdict."""
        digest_text, allowed = _digest_lines(state)
        t0 = time.time()

        if not llm_mod.LLM_ENABLED or requests is None:
            return self._wrap(deterministic_answer(question, state), allowed,
                              "template", time.time() - t0)
        try:
            r = requests.post(f"{self.base_url}/chat/completions",
                              headers=self._headers(),
                              json=self._payload(question, digest_text, False),
                              timeout=ASK_TIMEOUT_S)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            return self._wrap(text, allowed, "llm", time.time() - t0)
        except Exception as exc:
            print(f"[ask] falling back to deterministic ({type(exc).__name__}: {exc})")
            return self._wrap(deterministic_answer(question, state), allowed,
                              "template", time.time() - t0)

    def stream(self, question: str, state: Dict) -> Generator[Dict, None, None]:
        """Yield {'delta': str} chunks then a final {'done': True, ...} record.

        First token arrives in ~0.15 s here, so a ~2.5 s answer reads as instant.
        """
        digest_text, allowed = _digest_lines(state)
        t0 = time.time()

        if not llm_mod.LLM_ENABLED or requests is None:
            text = deterministic_answer(question, state)
            yield {"delta": text}
            yield self._wrap(text, allowed, "template", time.time() - t0, done=True)
            return

        acc = ""
        try:
            with requests.post(f"{self.base_url}/chat/completions",
                               headers=self._headers(),
                               json=self._payload(question, digest_text, True),
                               stream=True, timeout=ASK_TIMEOUT_S) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    raw = line.decode("utf-8", "ignore")
                    if raw.startswith("data:"):
                        raw = raw[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    delta = (d.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        acc += delta
                        yield {"delta": delta}
            if not acc.strip():
                raise ValueError("empty stream")
            yield self._wrap(acc.strip(), allowed, "llm", time.time() - t0, done=True)
        except Exception as exc:
            print(f"[ask] stream failed, falling back ({type(exc).__name__}: {exc})")
            text = deterministic_answer(question, state)
            yield {"delta": ("\n" if acc else "") + text}
            yield self._wrap(text, allowed, "template", time.time() - t0, done=True)

    def _wrap(self, text: str, allowed: Dict, answered_by: str,
              latency: float, done: bool = False) -> Dict:
        prov, unverified = "unchecked", []
        try:
            import provenance
            ok, unverified = provenance.verify(text, allowed)
            prov = "verified" if ok else "unverified"
            if not ok:
                self.violations += 1
                print(f"[ask] PROVENANCE FAIL — not in digest: {unverified}")
        except ImportError:
            pass
        out = {"answer": text, "answered_by": answered_by,
               "provenance": prov, "unverified": unverified,
               "latency_s": round(latency, 3), "figures_offered": len(allowed)}
        if done:
            out["done"] = True
        return out


ASKER = Asker()


# --------------------------------------------------------------------------
# Self-test:  AI_ASK=1 python hub/ask.py
#             LLM_ENABLED=0 python hub/ask.py     (fallback path)
# --------------------------------------------------------------------------

def _demo_state() -> Dict:
    return {
        "rooms": {"living": {"occupancy": False, "lux": 110, "temp_c": 23.5,
                             "humidity": 47, "temp_src": "knob_sim"}},
        "loads": {"living/ac": {"state": "on", "watts": 1100, "metered": True},
                  "living/dryer": {"state": "on", "watts": 2400, "metered": False}},
        "user": {"presence": "away"},
        "total_watts": 3500,
        "tariff": {"rate": 0.58, "period": "on_peak", "clock": "18:30"},
        "recos": [
            {"id": "r2-living-ac", "title": "A/C cooling an empty home",
             "severity": "critical", "usd": 0.319, "kwh": 0.55, "detector": "rule"},
            {"id": "learned-living-dryer", "title": "Unusual pattern for this home",
             "severity": "serious", "usd": 0.39, "kwh": 0.67,
             "detector": "learned", "anomaly_score": 0.81},
        ],
        "plan": {"situation": "A/C is cooling an empty home during peak hours."},
    }


if __name__ == "__main__":
    state = _demo_state()
    print("=" * 78)
    print("NATURAL-LANGUAGE Q&A — self-test")
    print(f"LLM_ENABLED={llm_mod.LLM_ENABLED}  endpoint={llm_mod.LLM_BASE_URL}")
    print("=" * 78)

    asker = Asker()
    for q in SUGGESTED_QUESTIONS:
        res = asker.ask(q, state)
        print(f"\nQ: {q}")
        print(f"   [{res['answered_by']}] {res['latency_s']}s  "
              f"provenance={res['provenance']}"
              + (f"  UNVERIFIED={res['unverified']}" if res["unverified"] else ""))
        print(f"   {res['answer']}")

    print(f"\nprovenance violations this run: {asker.violations}")
    print("=" * 78)
