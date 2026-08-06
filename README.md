# AI Home Energy Concierge — hackathon packet

Everything needed to execute the project on site without Claude. **Copy this whole
folder to a USB stick and push `code/` to your GitHub repo before Monday.**

Event: Snapdragon Multiverse 2026 · Mon Aug 3 – Fri Aug 7 · submit Fri **1:00 PM**, demo 1:30 PM

---

## Read in this order

| File | What it is | When |
|---|---|---|
| **`CLEANUP_REMINDER.md`** | **⚠ the planning docs are currently public — decide keep-or-remove, and redact the venue credentials** | **Before the judges see the repo** |
| **`04_ORGANIZER_REQUIREMENTS.md`** | compliance checklist from the organizer decks — tick every box | Read first |
| **`00_MASTER_PLAN.md`** | Architecture, the 5 de-risking decisions, day-by-day plan, roles, cut list | Before kickoff |
| **`01_LLM_PROMPT_PACK.md`** | Copy-paste prompts for the on-site LLM + the **QUAD** section | All week |
| **`02_DEMO_SCRIPT.md`** | Minute-by-minute 5-min demo, slides, Q&A prep | Thursday |
| **`03_SETUP_CHEATSHEET.md`** | Every command: GenieX, QUAD, broker, UNO Q, actuator, phone | Day 1 and demo morning |
| **`05_FILE_STRUCTURE_AND_RUN.md`** | **operator's manual** — every file explained, every run path, troubleshooting | When something breaks |
| **`06_UNO_Q_BRINGUP.md`** | **hands-on-hardware guide** — flash the sketch, bring up sensors, wire and test the actuator, close the loop, gated step by step | The moment the board is on the bench |
| **`presentation.html`** | **the deck** — open in any browser, arrow keys to navigate | Thursday / demo |
| **`code/`** | Working, tested backbone | Day 1 |

## Team

| Name | Email | Role |
|---|---|---|
| Gowtham Raj Baskaran | gbaskara@qti.qualcomm.com | Implementation — hub, rules engine, UNO Q firmware, Kasa actuation |
| Nanda Kishore Nagabhushana | nnagabhu@qti.qualcomm.com | Architecture & requirements · project proposal |
| Yash Joshi | yashjosh@qti.qualcomm.com | On-device AI — GenieX NPU narration, QUAD profiling, benchmarks |
| Ajay Reddy | areddy@qti.qualcomm.com | Product & demo — energy model, dashboard/simulator UX, presentation |

The four of us developed the concept and architecture together; the roles above are
where each of us carried the work. The same table appears in `code/README.md` — this
copy is here because names and emails are a hard eligibility requirement and the
repository root is the first page a reader lands on.

## Live repository

**https://github.com/gowtham612/ai-home-energy-concierge** — public, MIT licensed.
Verified: a fresh `git clone` passes 38/38 smoke checks with no manual fixes.

Submit that URL via the organizers' Microsoft Form by **Friday 12:00 PM** (target 10:30).

## The presentation

`presentation.html` is a single self-contained file — no server, no network, no
framework. The three screenshots are embedded as base64, so it works from a USB stick on
any machine.

| Key | Action |
|---|---|
| `→` / `space` / click | next slide |
| `←` | previous |
| `F` | fullscreen |
| `O` | slide overview, click to jump |
| `P` | print / export to PDF |
| `1`–`9` | jump to slide |

16 slides: problem · closed loop · architecture · three output screenshots · auditable AI ·
the guardrail · measured performance · graceful degradation · tooling · demo beats ·
limitations · summary.

To rebuild after replacing a screenshot: `python build_presentation.py`.
**Edit `presentation_template.html`, never `presentation.html`** — the latter is generated
and your changes would be overwritten on the next build.

### The demo video

We get **5 minutes**, so the demo is a **~45 s recording** rather than a live run. Slide 13
plays it.

- Drop the file in as **`demo.mp4`, in the same folder as `presentation.html`.**
- It is referenced relatively, *not* base64-inlined — inlining would take the deck from
  ~400 KB to tens of MB. Copy both files to the USB stick.
- If the file is missing the slide still renders and says so, rather than showing a black
  rectangle in front of the judges.

A **5-minute run sheet** (which seven slides to show, in what order, with timings) is in
the comment block at the top of `presentation_template.html`.

## What the organizer documents changed

This packet was revised after two organizer documents arrived. The three things that
moved:

1. **Our archetype is E — IoT Sensor → Actuator Physical AI.** *"Close the loop from
   sensing to physical action."* The first draft explicitly excluded actuation; that was
   wrong and is now reversed. The system physically switches loads off, with human
   approval.
2. **The rubric is point-weighted, and Technical Implementation is 40 of 100** — scored on
   latency, resource use and energy efficiency. So we measure: `hub/benchmark.py` plus
   QUAD's `/quad-profile` on real silicon.
3. **Several hard submission requirements we would have failed:** an open-source license
   file, every member's name *and email* in the README, and **every team member must
   submit a feedback form** (gates prize eligibility). All now tracked.

Also newly known: **GenieX** gives us an NPU-backed OpenAI-compatible endpoint on
`:18181`, and QUAD's `generate_code` stage is **blocked by gap G6** — which does not
affect us, because our sensor and actuator code is hand-written and tested.

## What is already built and verified

The code in `code/` is not pseudocode. It was run end-to-end on Windows on Arm:

- **38/38 smoke test checks pass** (`python smoke_test.py`)
- All **7 rules** fire; the comfort guardrail suppresses advice **and refuses actuation**
- **Real MQTT** path verified: simulator → broker → hub → rules → narration → dashboard
- **The full actuation loop verified**: approve → command → UNO Q executes → confirmation
  → saving booked as realized (see `dashboard_actuated.png`)
- **Measured performance**: rules engine 0.027 ms p50, edge filtering removes **88.7%** of
  broker traffic, 32.9 MB peak RSS
- **Every fallback tested with its dependency killed**: no LLM, no cloud, no broker,
  no hardware, no servo

Five real bugs were found and fixed during verification:
1. `uvicorn` without `[standard]` → WebSocket 404, dashboard silently blank
2. Windows-invalid `strftime('%-I')` → R6 crashed
3. Clock-jump inflation → a $1.17 phantom finding outranking the critical one
4. **MQTT subscription registered outside `on_connect`** → silently lost, so the UNO Q
   never received commands. Subscriptions must be re-established on every connect.
5. Applied findings were re-narrated forever → now suppressed once acted on

Bug 1 is the one most likely to cost you an hour on site; bug 4 is the one most likely to
break the actuation demo. Both are flagged in the cheatsheet and the prompt pack.

## First 30 minutes on Day 1

```bash
pip install -r code/requirements.txt      # note: uvicorn[standard], not plain uvicorn
cd code && python smoke_test.py           # expect 38/38
python hub/benchmark.py                   # your baseline numbers
python hub/server.py                      # then open the printed LAN URL
```

Then: fill the **team table** in `code/README.md` (names *and* emails), confirm the
**submission deadline** at office hours (the decks say 12:00 PM and 1:00 PM — assume
12:00), freeze the MQTT contract with your team, and assign the five roles from
`00_MASTER_PLAN.md` §2.

## The four things that win this

1. **The loop closes, and safely.** Physical actuation with human approval, and R7 gating
   the actuator — the system refuses to switch off the A/C at 29 °C even when asked. That
   satisfies the archetype *and* gives you the best safety story in the room.
2. **Auditable arithmetic.** Python computes every number; the LLM only phrases it. Every
   figure expands to its formula. This answers the judges' hardest question before they
   ask, and matches QUAD's own "honest, not magic" framing.
3. **Measured, not claimed.** 40 points ride on latency and efficiency. Most teams will
   have adjectives; you will have a table and a QUAD profile.
4. **Graceful degradation you can demo.** Five fallback rungs, rehearsed. A demo with
   fallbacks engaged and an honest explanation beats a broken one.

## Non-negotiables

- **Thursday 5 PM is the real deadline.** Friday morning is buffer only.
- **Record the demo video Thursday — including the servo physically moving.** For
  Archetype E that shot is your insurance.
- **Submit by 10:30 AM Friday** (deadline 12:00 PM). Demo order is randomized and emailed
  Thursday morning — assume you are first.
- **Every team member submits the feedback form.** No exceptions; it gates the prize.
- 35 of 100 points are documentation and presentation. Budget Thursday for them; do not
  spend it adding features.
