# 09 — AI enhancement plan (for the host-PC Claude session)

**Status:** ready to implement. **Self-rated 9.5/10** feasibility-weighted; rationale at the end.

**Who this is for:** the Claude Code session running on the QUAD/Copilot+ host laptop, which has
the UNO Q board, the Kasa devices, and GenieX. This document is self-contained — you should not
need any other context to implement it.

**Read first:** `07_QUAD_SESSION_LOG.md` §0 (resume point) and §14 (what was actually built).
Note that §12 and §14.8 of that log list "team names/emails still placeholders" and "LICENSE
decision open" — **both are DONE** (MIT license with all four names; team table populated). That
log text is stale; do not act on it. The one genuine leftover is venue credentials still present
in `04_ORGANIZER_REQUIREMENTS.md` §E — see `CLEANUP_REMINDER.md`.

---

## 0. The problem this plan solves

The project's AI is currently **decorative and removable**. `hub/llm.py` receives a `Finding` in
which the rules engine has *already decided everything* — severity, dollar value, actions — and
the LLM's only job is to reword it. `template_narrate()` produces the same recommendations
deterministically. Kill GenieX and you lose prose style, nothing else.

That is a fine honesty story ("the LLM narrates, Python computes") but it caps the score and
fails the obvious judge question: **"what is the AI actually deciding?"**

Three real weaknesses sit next to each other:

| Weakness | Consequence |
|---|---|
| All 7 rule thresholds are hardcoded constants | cannot detect anything a fixed threshold can't express |
| The LLM sees one finding at a time | no cross-finding reasoning, no plan, no tradeoffs |
| Nothing verifies the LLM's output numerically | the "never invents a number" claim rests on prompt instruction + field overwrite only |

This plan turns each into a scored asset.

### Proof the first weakness is real (verified, not theoretical)

```
3 AM · room occupied · 24 °C · A/C running 1 h · user home · off-peak
  → current 7 rules produce: []   (nothing)
```

No threshold trips: occupancy is true, temp is in band, it's off-peak. But it is wildly
anomalous for a household pattern and it is real waste. **This exact scenario is the
justification for P0-A and a great live demo beat.**

---

## 1. Verdict on the "split AI across UNO Q + X Elite" idea

**The instinct is right, but not with an LLM.** State it this way to a judge:

- The Dragonwing QRB2210 is a **quad-core Cortex-A53** and per `07_QUAD_SESSION_LOG.md` §2.3 has
  **no on-device AI acceleration stack at all** — no fastrpc/DSP skel, no SNPE, no onnxruntime,
  GPU is `rusticl` (Mesa software, not real Adreno OpenCL). Bare Debian, CPU-only. A 4B LLM there
  is physically wrong. **Do not attempt it.**
- But **"AI at the edge" does not have to mean an LLM.** A small *trained classifier* — logistic
  regression — runs in **microseconds** on that A53 in pure Python with **zero new dependencies**
  (the board's `~/energy-venv` has no numpy and doesn't need it).

So the split becomes genuinely defensible:

| Tier | Hardware | Model | Job | Measured budget |
|---|---|---|---|---|
| **1. Edge** | Dragonwing A53 (CPU) | logistic regression, ~10 features | anomaly scoring on **every sample** | **µs** |
| **2. Hub** | X Elite Hexagon NPU (45 TOPS) | Qwen3-4B W4A16 via GenieX | **plan synthesis** across all findings, once per change | ~3.1 s |
| **3. Hub, on demand** | same NPU, same model | natural-language **Q&A** over home state | ~3.1 s, user-initiated |

This is exactly the "orchestrate across the SoC under a power/latency budget" story QUAD itself
sells, and it feeds the **40-point** criterion, which rewards *efficiency and deliberate
placement* — **not** model size. A big slow model *hurts* that criterion unless you justify each
placement. Now you can.

---

## 2. Hard constraints — respect these

1. **GenieX narration is measured at 3110 ms p50 / 3273 ms p95** (`code/README.md` performance
   table). Nothing per-sample can call it. Budget accordingly.
2. **The simulator and approve flow are being actively repaired in parallel.** Therefore:
   - **DO NOT TOUCH** `code/simulator/index.html`, the existing `/api/apply` logic in
     `code/hub/server.py`, or the `Actuator`/`KasaBank` classes in
     `code/arduino/uno_q_publisher.py`.
   - New capability goes in **new files**. Edits to existing files are **additive, few-line, and
     feature-flagged**, listed explicitly per task below.
3. **`python smoke_test.py` must stay at 32/32** after every task. That is the gate.
4. **Everything is feature-flagged off by default** (`AI_ANOMALY=0`, `AI_PLAN=0`, `AI_ASK=0`) so a
   half-finished task can never break the demo.
5. **Honesty bar:** the anomaly model trains on *simulated* data and must say so wherever it
   surfaces. Never present a learned score as a measurement.

---

## 3. Priority order with stop-lines

Work strictly top to bottom. **After each task the project is strictly better and demo-ready**,
so running out of time still leaves a clean state.

| # | Task | Effort | Stop-line meaning |
|---|---|---|---|
| **P0-A** | Edge anomaly detector (learned, on the board) | ~2.5 h | *Stop here: you have a real second AI tier + a µs-vs-s latency contrast.* |
| **P0-B** | LLM plan synthesis (replaces per-finding narration) | ~2.5 h | *Stop here: the LLM is load-bearing — killing it visibly degrades output.* |
| **P1-C** | Numeric provenance verifier | ~1 h | *Stop here: a novel, demonstrable AI-safety mechanism.* |
| **P1-D** | Benchmark all three tiers | ~1.5 h | *Stop here: the 40-pt slide is evidence, not adjectives.* |
| **P2-E** | Natural-language Q&A page | ~2 h | The Team's-Choice magnet for the gallery walk. |
| **P3-F** | Deck + README + demo script | ~1 h | Required for the 15-pt criterion regardless. |

**If you only finish P0-A and P0-B, the AI gap is closed.** The rest is amplification.

---

## P0-A — Edge anomaly detector

### Files

- **NEW** `code/hub/anomaly.py` (~180 lines) — pure Python, **no numpy/sklearn**, so it imports
  identically on the A53 board and the X Elite hub.
- **NEW** `code/tools/train_anomaly.py` (~150 lines) — offline trainer, hub only. May use
  numpy/sklearn if present; its *output* is plain Python literals.
- **NEW** `code/hub/anomaly_model.py` — generated by the trainer; coefficient literals + a
  provenance header.
- **EDIT (additive, ~12 lines)** `code/hub/rules.py` — append learned findings after R1–R6,
  **before** R7.
- **EDIT (additive, ~6 lines)** `code/arduino/uno_q_publisher.py` — score locally, add one field
  to the sensor payload. Guarded by `AI_ANOMALY=1`. **Do not restructure anything there.**

### `anomaly.py` interface

```python
FEATURE_NAMES = [
    "hour_sin", "hour_cos",      # cyclical time-of-day
    "occupancy",                 # 0/1
    "lux_norm",                  # lux / 1000, clamped 0..1
    "temp_norm",                 # (temp_c - 16) / 16, clamped 0..1
    "watts_norm",                # total_watts / 4000, clamped 0..1
    "lights_on", "hvac_on",      # 0/1
    "presence_away",             # 0/1
]

def featurize(snapshot: dict, now_dt: datetime) -> list[float]:
    """Snapshot -> fixed-length feature vector. Pure arithmetic, no deps."""

def score(features: list[float]) -> tuple[float, str]:
    """Return (anomaly_score 0..1, top_contributing_feature_name).

    Logistic regression: p = sigmoid(w . x + b). Top contributor is the feature with
    the largest |w_i * x_i| — this is what makes the score EXPLAINABLE, which both the
    planner and the UI consume.
    """

ANOMALY_THRESHOLD = 0.70   # named constant, documented, tunable
```

Include a `if __name__ == "__main__":` self-test printing scores for 5 hand-built snapshots,
including the 3 AM HVAC case above (should score high) and a normal evening (should score low).

### Trainer (`tools/train_anomaly.py`)

- Generate **~14 simulated days** of plausible household rhythm: morning daylight, workday-away,
  evening peak, overnight standby, with ±10–15 % jitter on timing and wattage.
- Label `normal`(0) for rhythm-consistent samples; inject `anomalous`(1): HVAC at 3 AM, lights at
  full daylight while away, dryer at 2 AM, all loads on with nobody home for hours.
- Fit logistic regression. Emit weights into `anomaly_model.py`.
- **Print train/holdout accuracy and a confusion matrix** — you will quote these on stage, so they
  must be real numbers you can defend.
- Emit a header comment into `anomaly_model.py` recording: training date, number of simulated
  days, holdout accuracy, and **an explicit statement that the training data is synthetic.**

### Wiring into `rules.py` (additive)

```python
# After R1-R6 collection, BEFORE r7_comfort_guardrail:
if os.environ.get("AI_ANOMALY", "0") == "1":
    try:
        import anomaly
        feats = anomaly.featurize(snapshot, now_dt)
        s, top = anomaly.score(feats)
        if s >= anomaly.ANOMALY_THRESHOLD:
            findings.append(_make_learned_finding(snapshot, now_dt, s, top))
    except Exception as exc:
        print(f"[rules] anomaly detector unavailable: {exc}")
```

**Critical for auditability:** tag the new finding `detector="learned"`; every R1–R6 finding gets
`detector="rule"`. **The UI must show this distinction.** That preserves the "rules are
deterministic and auditable" claim *and* makes the AI contribution visible.

Learned-finding evidence lines must read honestly:

```
Learned detector: this pattern scores 0.87 anomalous for this home
Strongest signal: hvac_on at an hour where this home is normally idle
Model: logistic regression, 9 features, trained on 14 SIMULATED days (holdout acc 0.94)
NOTE: a learned score, not a measurement — no fixed threshold expresses this
```

### Gate

```bash
python hub/anomaly.py                 # self-test prints scores for 5 snapshots
python tools/train_anomaly.py         # prints accuracy + emits anomaly_model.py
AI_ANOMALY=1 python hub/rules.py      # the 3 AM HVAC scenario now yields a learned finding
python smoke_test.py                  # MUST still be 32/32
```

Then measure it — this number is the tier story:

```bash
python -c "
import time, statistics, datetime, sys; sys.path.insert(0,'hub')
import anomaly
snap={'rooms':{'living':{'occupancy':True,'lux':100,'temp_c':24.0,'humidity':50}},
      'loads':{'living/ac':{'state':'on','watts':1100}},
      'user':{'presence':'home'},'now':0}
dt=datetime.datetime(2026,8,6,3,0)
ts=[]
for _ in range(2000):
    t0=time.perf_counter(); anomaly.score(anomaly.featurize(snap,dt)); ts.append((time.perf_counter()-t0)*1e6)
print(f'edge inference p50 {statistics.median(ts):.1f} us')
"
```

Expect **single-digit to low-tens of microseconds** — the headline contrast against 3110 ms.

---

## P0-B — LLM plan synthesis

### What changes

**Today:** N findings → N LLM calls, each rewording one finding. Cost N × 3.1 s.
**After:** N findings → **one** call returning a *ranked plan with reasoning*. Cost 3.1 s total,
**and only when the finding set changes** — strictly *cheaper* than today while doing more.

The model now decides what the rules engine cannot:
- **ordering by actual usefulness**, not just dollar value (rules sort by `usd` alone)
- **which findings interact** ("the dryer is legitimate — defer it; the A/C is pure waste — kill it now")
- **what to do first given the anomaly signal**
- **why the guardrail refused**, in plain language, at the most memorable demo moment

### Files

- **NEW** `code/hub/planner.py` (~230 lines)
- **EDIT (additive, ~20 lines)** `code/hub/server.py` — in the eval loop, when `AI_PLAN=1` and the
  finding-id set differs from last cycle, call the planner once and attach the plan to the
  broadcast state. **Leave the existing per-finding narration path intact as the fallback.**

### Prompt contract

```
You are the reasoning layer of a home energy system. You are given a complete list of
detected findings, each with PRE-COMPUTED numbers, plus a learned anomaly signal.

Your job is to produce a PLAN: order the findings by what the homeowner should actually do
first, and explain the reasoning for the ordering.

CRITICAL RULES:
- You must NOT perform arithmetic and must NOT invent, restate-differently, or round any
  number. Use figures exactly as given, or omit them.
- Ranking is your decision. Numbers are not.
- If a finding is legitimate use whose only problem is timing, say so — recommend deferring
  it, not stopping it.
- If the comfort guardrail suppressed something, explain the tradeoff in one sentence.

Reply with a single JSON object:
{
  "situation": "<one sentence, what is going on in this home right now>",
  "plan": [
    {"finding_id": "<id from the input>",
     "rank": <int, 1 = do first>,
     "why_this_order": "<one short sentence>",
     "action": "<imperative, under 10 words>"}
  ],
  "deferred": [{"finding_id": "<id>", "reason": "<why it can wait>"}],
  "anomaly_note": "<one sentence about the learned signal, or empty string>"
}
```

### Required guards (non-negotiable)

1. **Every `finding_id` in the response must exist in the input.** Drop unknown ids and log it.
2. **All numeric fields on every Recommendation come from the `Finding`, never the model** —
   reuse the existing `_validate()` overwrite discipline in `llm.py`.
3. **Deterministic fallback** = today's behaviour: findings sorted by `usd` desc with
   `template_narrate()` text. On any failure (timeout, malformed JSON, unknown id) fall back and
   set `planned_by="template"` so the UI labels it — mirroring the existing `narrated_by` badge.
4. **Only re-plan when the finding-id set changes.** Cache by `frozenset(finding ids)`. This keeps
   cost at one call per *change*, not per cycle.

### Gate

```bash
python hub/planner.py                            # self-test: 3 findings -> ranked plan, both paths
AI_PLAN=1 LLM_ENABLED=0 python hub/planner.py    # deterministic fallback produces a sane plan
python smoke_test.py                             # 32/32
```

Then prove the load-bearing claim: run once with GenieX up, once with it down, and **keep both
outputs side by side for the deck.** The visible difference *is* the argument.

---

## P1-C — Numeric provenance verifier

Small, cheap, and the most novel item here. Extends the project's existing philosophy into new
territory and demos in seconds.

**Idea:** after any LLM response, extract **every number** the model emitted and verify each
appears in the deterministic source it was given. Flag or strip anything that doesn't.

### Files

- **NEW** `code/hub/provenance.py` (~110 lines)

```python
def extract_numbers(text: str) -> list[str]:
    """Every numeric literal in the text, incl. $1.28, 2.2 kWh, 0.55, 89%."""

def verify(text: str, allowed: dict) -> tuple[bool, list[str]]:
    """Return (ok, unverified). `allowed` is the flat dict of every figure the model was
    given. Tolerant of formatting (1.28 == $1.28 == 1.280). Whitelist small integers
    0-24 (hours, counts) and rule ids R1..R7."""
```

Call it from `planner.py` and (P2) the Q&A path. On failure: keep the text but mark
`provenance="unverified"` and surface a small UI warning, **or** fall back to deterministic text.
**Log every violation — you want the count.**

**Why judges credit it:** every other team's answer to "how do you know the model didn't make that
up?" is "we told it not to." Yours is **"we check, mechanically, and here is the violation count
from this run."** A mechanism, not a promise.

### Gate

```bash
python hub/provenance.py    # self-test MUST catch a planted hallucinated number
python smoke_test.py        # 32/32
```

Include a deliberately-hallucinating fixture so you can demo it working.

---

## P1-D — Benchmark all three tiers

Extend `code/hub/benchmark.py` following its existing conventions (p50/p95, `--markdown`,
`--json`, **SKIPPED-with-reason** when a path is unavailable — never silently substitute one
path's timing for another's).

Add rows:

| Metric | Expect |
|---|---|
| Edge anomaly inference, per sample | **µs** |
| Rules engine, 7 rules | 0.027 ms (already measured) |
| Plan synthesis, NPU, one call per change | ~3.1 s |
| Plan synthesis, deterministic fallback | µs |
| Q&A round-trip, NPU (if P2 done) | ~3.1 s |
| Provenance verification per response | µs |
| Peak RSS | ~34 MB |
| Broker messages avoided by edge filtering | 88.7 % |

Also add the **fixed reproducible batch** from `npu_simulated_data_proposal.html` — genuinely good
and cheap: ~25 pre-built findings through both AI paths for a real percentile distribution instead
of a 3-sample average. **Seed any randomness** so the published figure reproduces.

**This table is the 40-point slide.** A µs → ms → s progression across three tiers, each with a
stated reason for its placement, is the strongest available evidence for "resource utilization,
optimization, latency and performance."

---

## P2-E — Natural-language Q&A (Team's Choice magnet)

Per the orientation transcript there is a **pre-screening round → 6 finalists**, plus a **gallery
walk-around where other teams watch demos and vote Team's Choice.** For a wandering engineer,
*"type a question, watch it answered on-device"* is far more memorable than a plan list, and it is
the clearest proof real AI is present.

### Files

- **NEW** `code/hub/ask.py` (~140 lines)
- **NEW** `code/ask/index.html` (~180 lines) — **a separate page. Do NOT touch
  `simulator/index.html`,** which is being repaired.
- **EDIT (additive, ~10 lines)** `server.py`: `POST /api/ask` + `GET /ask`, both behind `AI_ASK=1`.

### Design

- **Reuse `cloud_report.build_digest()`** — the compact state digest already exists; do not rewrite it.
- Prompt: digest + question, with the rule that the model may cite **only** numbers in the digest.
- **Run the P1-C provenance verifier on every answer.** Show a `verified` / `unverified` badge.
  That is the demo moment.
- Suggested-question chips so a visitor needn't think: *"Why is my bill high?"*, *"What should I do
  first?"*, *"What if I shift the dryer to 9 PM?"*, *"Is anything unusual right now?"* — that last
  one exercises the edge anomaly tier, a nice symmetry.
- **Verify on the host whether GenieX supports streaming** (`stream: true` on
  `/v1/chat/completions`). If yes, stream tokens so a 3 s answer *feels* fast. If not, show a
  progress state. **Do not assume either way — test it.**

### Gate

```bash
AI_ASK=1 python hub/ask.py     # self-test: 4 canned questions, LLM and fallback paths
python smoke_test.py           # 32/32
```

---

## P3-F — Documentation and deck

1. **`code/README.md`** — new "Where the AI actually runs" section with the three-tier table and
   measured latency per tier. Update the performance table. State the anomaly model's synthetic
   training data plainly, and update the "Limitations, honestly" section.
2. **`presentation_template.html`** — add the edge-AI badge to the UNO Q box and the plan/Q&A lines
   to the X Elite box, then **`python build_presentation.py`** to regenerate `presentation.html`.
   **Never hand-edit `presentation.html`.**
3. **`02_DEMO_SCRIPT.md`** — rework for the **7-minute** slot (1 min setup + **5 min demo** + 1 min
   breakdown) and add a gallery-mode loop.
4. **Rehearse these new Q&A answers:**
   - *"What does the AI actually decide?"* → ranking and reasoning across findings, plus a learned
     anomaly score no fixed threshold can express. Numbers stay in Python.
   - *"Isn't the anomaly model trained on fake data?"* → yes, 14 simulated days, holdout accuracy
     X.XX, stated in the UI. In deployment it retrains on real history. We would rather label it
     than imply otherwise.
   - *"Why not run the LLM on the UNO Q?"* → A53, no NPU stack there at all; a 4B model is
     physically wrong for that budget. So the *learned classifier* runs there in microseconds and
     the LLM runs on the 45-TOPS NPU. That split is the design.

---

## 4. Logistics from the orientation transcript (act on these)

- **Submit the GitHub repo link NOW and keep pushing.** They explicitly said you may submit early
  and keep updating; only the *survey* waits for Friday. Removes the biggest deadline risk.
- **Personal GitHub + personal email** — reconfirmed repeatedly (repo, scaler, GenieX signups).
- **7 minutes total**, not 5+2. Rehearse to 5:00 of actual demo.
- **Pre-screening → 6 finalists**, then gallery viewing. The demo must survive being run **many
  times back-to-back** — favour a short looping video plus one interactive moment (the Q&A page).
- **They recommend a recorded video over a live run** in a 5-minute slot. Have both.
- **The feedback form has its own top prize**, and they asked for *concise bulleted* feedback,
  explicitly "not AI slop." Assign one person, 15 minutes, real observations.
- **QUAD token limits reset every few hours**; if you hit one, ask on Discord immediately.
- **Only one person can use the QUAD laptop at a time** — plan shifts.

---

## 5. Verification — whole-project gate

After every task:

```bash
cd code
python smoke_test.py                          # 32/32, non-negotiable
python hub/benchmark.py --markdown            # table for the README
AI_ANOMALY=1 AI_PLAN=1 python hub/server.py   # both tiers live
```

Full rehearsal with all flags on:

1. Simulator → away + A/C on → **rule** finding, ranked by the planner with a reason.
2. Set 3 AM + moderate temp + A/C on → **learned** finding, tagged `learned`, with score and top
   contributing feature.
3. Approve → the real Kasa bulb goes dark → saving books as realized.
4. Knob past 27 °C → Approve → **HTTP 409 refusal**, planner explains the tradeoff.
5. `/ask` → *"Is anything unusual right now?"* → NPU answer with a **verified** badge.
6. Kill GenieX → everything still works, visibly labeled `template` → restart it.

**If step 6 is not clean, fix that before adding anything else.**

---

## 6. Self-rating: 9.5/10

| Dimension | Score | Why |
|---|---|---|
| Genuine, defensible AI | 9.5 | three distinct AI jobs, each load-bearing — remove any one and a capability is lost; plus a novel provenance mechanism |
| Feasibility in one day, while debugging | 9.0 | strict priority order with stop-lines, new files not hot-file edits, feature-flagged, smoke-test gated |
| 40-pt Technical Implementation | 9.5 | µs → ms → s measured three-tier table with a stated reason per placement |
| 25-pt Innovation | 9.5 | provenance verifier, learned detector catching what thresholds can't, interactive on-device Q&A |
| Honesty standard | 9.5 | synthetic training data declared, learned vs rule findings tagged separately, numbers mechanically verified |

**Deliberately excluded, and why**

- **Adaptive thresholds learned from real history** — the strongest remaining idea, thematically
  perfect (same human-approval pattern as actuation: *"the model proposes a new threshold, you
  approve it"*). Cut because the hub persists **no history** (5 min in-memory only). Persistence +
  learning + UI in one day, while debugging, is how you get three broken things. **Present it as
  roadmap** — it is a strong answer to "what's next."
- **A smaller/faster model for snappier Q&A** — another multi-GB pull and an unknown on demo day.
- **Anything requiring `/quad-profile`** — still blocked by two server-side infra failures
  (`07_QUAD_SESSION_LOG.md` §10). GenieX's real measured numbers already stand as NPU evidence.

**What would make it 10/10:** persisted history enabling genuinely adaptive thresholds with human
approval, plus a second *measured* device tier (a real Modulino Thermo replacing the Knob's
declared simulation). Both are hours-to-days beyond the remaining budget — which is exactly why
they are the roadmap slide rather than tonight's work.
