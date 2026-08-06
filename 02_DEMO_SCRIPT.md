# Demo script — 5 minutes, timed

Five minutes is much shorter than it feels. This script is timed to 4:45, leaving
15 seconds of slack. **Rehearse with a timer three times on Thursday.**

**Point-weighted criteria — this script is built to hit all four:**

| Criterion | Points | Where this script earns it |
|---|---|---|
| Technical Implementation | **40** | the measured-numbers slide + the NPU/edge-filtering figures |
| Use-Case & Innovation | 25 | context fusion + the closed loop |
| Deployment & Accessibility | 20 | "3 commands, 32 tests" line |
| Presentation & Documentation | 15 | timing, clarity, the audit panel |

**Your assigned archetype is E — IoT Sensor → Actuator Physical AI.** The physical
actuation is not a bonus; it is the thing being judged. **Do not run out of time before
the servo moves.** If you are behind at 3:00, skip the cloud report, not the actuation.

**Demo order is randomized and emailed Thursday morning.** Assume you are first.

---

## Slide deck — 6 slides, no more

| # | Slide | On screen | Seconds |
|---|---|---|---|
| 1 | Title + the problem | Project name, team, one sentence | 25 |
| 2 | Architecture | The four-tier diagram, actuator arrow highlighted | 40 |
| 3 | **LIVE DEMO** | Dashboard + phone side by side | 165 |
| 4 | **Measured performance** | The benchmark table + QUAD profile | 35 |
| 5 | Why this is different | Four claims | 30 |
| 6 | What's next | Honest limitations | 15 |

Put the dashboard on the projector, the phone on a document camera or held up, **and the
servo/lamp where the judges can see it move.** Rehearse the physical choreography —
fumbling costs 30 seconds.

**Nominate one speaker and one driver.** The driver never talks; the speaker never
touches the keyboard.

---

## The script

### [0:00–0:25] Problem

> "Homes waste energy because devices don't know what's happening around them. Your
> A/C doesn't know you left. Your lights don't know the sun is out. And your utility
> charges you 81% more between 4 and 9 PM than it does overnight.
>
> We built the AI Home Energy Concierge — three devices and a cloud accelerator that
> notice waste, explain what it's costing you in plain language, and then — with your
> approval — **physically switch it off.**"

### [0:25–1:05] Architecture

Point at each tier as you name it. **The "why" is what gets scored.**

> "Four tiers, and where each piece of intelligence lives is a deliberate choice.
>
> The **Arduino UNO Q** is a dual-brain board. The STM32 microcontroller, running over
> Zephyr, samples PIR, light and temperature in hard real time — and drives the servo.
> The Qualcomm Dragonwing running Debian does median smoothing, an occupancy state
> machine, and speaks MQTT over Wi-Fi. It's a network peer doing its own edge
> processing — not a sensor on a USB cable.
>
> The **Snapdragon X Elite Copilot+ PC** is the orchestrator: broker, state fusion,
> deterministic rules engine, and a local LLM running on the 45-TOPS NPU through
> **GenieX**. On-device, because latency and privacy both matter — your occupancy data
> never leaves the house.
>
> The **Galaxy S25** contributes the one signal nothing else can — are you actually
> home — and it's where you approve the action.
>
> And **Qualcomm AI Cloud 100** does the heavy weekly analysis, off the critical path."

### [1:05–3:50] LIVE DEMO — the core

**Beat 1 (20s) — baseline.** Dashboard showing everything running legitimately.

> "I'm home, watching TV. 1,460 watts. It's 6:30 PM, so we're on-peak at 58 cents a
> kilowatt-hour. Notice: **no recommendations.** Everything running is actually in use.
> A system that nags you constantly gets ignored."

**Beat 2 (20s) — the trigger.** Tap AWAY on the phone. Hold it up.

> "Now I leave. The phone crosses the geofence and tells the hub I'm away."

**Beat 3 (40s) — THE MONEY MOMENT.** Recommendation appears; the phone buzzes.

> "Motion times out. The lights and A/C are still running. Here's the recommendation —
> on the dashboard and pushed to my phone.
>
> 'Cooling an empty home.' A dollar twenty-eight so far, 2.2 kilowatt-hours, half a
> kilo of CO₂.
>
> And this is the part I want to be judged on." **Expand the audit disclosure.**
> "1,100 watts times 7,200 seconds is 2.2 kilowatt-hours, times 58 cents on-peak is a
> dollar twenty-eight. The evidence that triggered the rule. The source of the power
> figure.
>
> **The language model wrote the sentence. Python did the arithmetic.** The model never
> touches a number — that's an architectural rule, not a convention."

**Beat 4 (40s) — CLOSE THE LOOP. The archetype moment; do not rush it.**

> "Now watch what happens when I approve it."

Tap **Approve & turn it off** on the phone. **Pause. Let everyone watch the servo move
and the lamp go out.**

> "The hub published a command, the UNO Q drove the servo, the servo pressed a real
> switch, and the board sent back a confirmation. Sense, reason, recommend, human
> approves, **physical action**, confirm.
>
> And notice the dashboard: that number moved out of 'avoidable' and into **'realized.'**
> It's no longer 'you could save this' — it's 'you saved this.'"

**Beat 5 (30s) — THE SAFETY STORY. This is your best differentiator.**

> "But here's the thing I actually want to show you."

Raise the room temperature (sensor injection or the real sensor), then tap Approve again.

> "The room is now 29 degrees. I'm asking the system to switch off the air conditioning,
> and it **refuses.** That's rule R7, our comfort guardrail — and it isn't advisory, it's
> a pre-flight check on the actuator. The system will not carry out its own
> recommendation if doing so would make your home uncomfortable.
>
> **That's the difference between automation and judgment.**"

**Beat 6 (20s) — context fusion.**

> "One more. Next morning, I'm home, the room is occupied — but it's 640 lux of daylight
> with the lights on. A motion sensor sees an occupied room and does nothing. We flag it,
> because we fused light level with occupancy. And the dryer at 5 PM: we charge only the
> **rate delta**, because the load is legitimate — only its timing is wasteful."

**Beat 7 (15s) — the cloud tier.** Click "Generate weekly deep report."

> "For depth we burst to AI Cloud 100 — a larger model over a digest we compute locally.
> Ranked weekly plan, and the retrofit with the best payback: a $130 thermostat, 2.4
> months."

### [3:50–4:25] Measured performance — the 40-point slide

**Do not skip this.** It is the heaviest criterion and most teams will have nothing here.

> "Technical implementation is judged on latency, resource use and energy efficiency, so
> we measured instead of claiming.
>
> The whole reasoning tier — seven rules, the energy model, narration — runs in **35
> microseconds**. The edge tier is effectively free. All the latency that matters is LLM
> inference, which is exactly why it's the only part we put on the NPU.
>
> **Edge filtering removes 89% of broker traffic**: the UNO Q samples at 1 Hz but only
> publishes on a material change or a 10-second heartbeat. 68 messages instead of 600.
>
> The whole hub reasoning stack is **33 megabytes** of RSS.
>
> And there's a symmetry we like: **an energy-saving app that measures its own energy
> cost.** These numbers come from `benchmark.py` in the repo — you can reproduce them."

*(If you have the QUAD profile: "and these NPU latency and power figures are from
`/quad-profile`, measured on real silicon.")*

### [4:25–4:45] Why this is different + what's next

> "Four things. **Where the AI lives is the design** — rules at the edge in microseconds,
> a small model on the NPU for private narration, a large model in cloud for depth.
> **Auditable AI** — the model narrates, Python computes, every number expands to its
> formula. **Context fusion** — this recommendation is impossible on any single one of
> these devices. And **the loop closes safely** — physical action, human-approved, with a
> guardrail that can veto the machine.
>
> Honest limitations: load power is modelled, not metered — a clamp meter is next. Lux is
> uncalibrated, so we use it as a threshold, not a measurement. And actuation is
> human-approved by design; we'd want a lot more validation before removing the human.
>
> Repo, README, MIT license, 32 tests, and a three-command quickstart are up. Thanks."

---

## Q&A prep — the questions you will get

**"Where do the wattage numbers come from?"**
> Published typical figures — DOE and Energy Star — and every entry carries its source
> string, visible in the audit panel. We model load power rather than metering it; a smart
> plug is the next step. We'd rather show a defensible estimate than a fabricated
> measurement.

**"Is it really running on the NPU?"**
> Yes — GenieX with the `qairt` runtime, which is NPU-only, using a pre-compiled AI Hub
> bundle. We also measured the deterministic path as a control, so we can tell you what
> the model actually costs versus the arithmetic. *(If QUAD profiling is done: and these
> are the `/quad-profile` numbers from real silicon.)*

**"Did you use QUAD?"**
> For profiling, yes — that's where our NPU numbers come from. We deliberately skipped
> `generate_code`: your own project sheet flags it as blocked by gap **G6**, the UNO Q
> sensor/actuator and GPIO codegen. Rather than wait on it or ship mock output, we wrote
> and tested that layer by hand. A pending platform gap didn't become a project blocker.

**"What if the LLM hallucinates a saving?"**
> Structurally it can't. It receives pre-computed figures, and after the call we overwrite
> every numeric field from the deterministic source. If the prose contradicts the computed
> value, the computed value wins. And if the model is unreachable, a Python narrator takes
> over — that's what the "TEMPLATE" badge on the card means.

**"Isn't approving each action tedious? Why not full automation?"**
> Deliberate. Note R7 refusing to switch off the A/C at 29 degrees — that's the system
> declining to execute its own advice. Full autonomy is a small code change; earning the
> trust to enable it is the hard part, and we'd want much more validation first. The
> guardrail architecture is what would make that safe.

**"How do you know it's really an open window in R4?"**
> We don't, and we say so. It's a heuristic — A/C running 15+ minutes, temperature not
> falling, humidity above 60%. The evidence list literally says "HEURISTIC: we infer an
> open window, we do not sense it directly."

**"What's genuinely novel? Smart thermostats exist."**
> A thermostat sees one room and one load. Our recommendations require four independent
> context sources fused together, and the deliberate placement of intelligence across four
> tiers is the contribution — plus auditability, which no commercial product in this space
> offers.

**"Does it scale to a whole house?"**
> The MQTT contract is already per-room and per-load — `home/sensors/<room>`,
> `home/command/<room>/<load>` — and the rules iterate over rooms. Adding rooms is adding
> publishers. We demo one room because multiple rooms demo identically and we'd rather
> show depth.

**"How hard is it to install?"**
> Three commands with no hardware, and `smoke_test.py` runs 38 checks so you know
> immediately whether your environment is sound. Every tier has a tested fallback.

---

## Stage discipline

- **Protect the actuation beat.** It is the archetype. If you're behind at 3:00, cut the
  cloud report and the daylight rule, never the servo.
- **Say "simulated" out loud** if you're on the simulator. Judges respect it; getting
  caught implying otherwise is fatal.
- **Do not debug on stage.** Move to the next fallback rung mid-sentence: *"Venue Wi-Fi is
  fighting us — switching to our simulated sensor feed. Everything downstream is live."*
- **If the servo fails**, say plainly that the command reached the board and the actuator
  is the failed link — then show the video. Never imply a physical action happened when it
  didn't.
- **Do not read the slides.** They are backdrop.
- **Lead with the dollar figure**, not the architecture. Judges remember "$265 a year"
  far longer than "MQTT over Wi-Fi."
- **For Team's Choice** (other teams vote): the servo moving and the audit panel are what
  engineers remember. Make sure other teams see the demo.
