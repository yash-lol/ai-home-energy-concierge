# 10 — Demo plan: real hardware + AI, staged as one convincing story

> ## ⚠ FORMAT CHANGED: this is a **2-minute recorded video**, not a live 5-minute slot.
>
> §0's reasoning and §3's beats still hold. **§4's timing table is void** — replaced by
> §8, a shot list built for a cut video. **§6's checklist does not work as written** —
> corrected in §10. Read §8 first, then §3 for the *why* behind each beat.
>
> **Video changes one thing fundamentally.** §3 Beat B spends 130 words teaching you to
> stage an 11.5 s pause gracefully on stage. In a recorded video you simply **cut it** —
> and you can retake until the model says something good, which de-risks the entire plan.
> The rule that replaces it: if you cut a wait, **caption the real number on screen**.
> Never let an edit imply latency the system does not have.
>
> **Three hardware facts §1 and §3 get wrong, verified against the live devices on
> 2026-08-06 — see §9. One of them silently breaks the payload of shot 2.**

**Read this alongside `02_DEMO_SCRIPT.md` (the timed script) — this file is the "why it's
convincing" layer and the exact hardware staging.** `02_DEMO_SCRIPT.md` still describes the
original breadboard/servo/Cloud-AI-100 plan and needs a rewrite once this staging is locked;
that rewrite is the last step below.

**Your teammate's `demo_idea.html`** (45-second video storyboard) is good and is used as
*reference*, not as the golden path — its five-act structure (before/after → reasoning chain →
audit zoom → refusal twist → closing number) is the right shape for a recorded backup, but it
predates the hardware pivot (Modulino/Kasa) and the three-tier AI (P0-A through P2-E), so its
specific beats need updating, not discarding.

---

## 0. What "convincing" means here, precisely

A judge has seen ten teams flip a smart plug by the time they get to you. What they have **not**
seen ten times:

1. **A live number that changes because a physical thing changed** — not a slide claiming it.
2. **An AI that visibly *decides* something**, not one that just talks nicely.
3. **An AI that gets refused permission** — the comfort guardrail. This is still your single best
   30 seconds and nothing below changes that.
4. **A number the AI got caught trying to fudge, and the system catching it.** Nobody else will
   have this. It is new since the plan landed (`hub/provenance.py`) and it is the cheapest,
   highest-leverage beat to add.

Everything below is built to hit those four with the exact hardware you have, seamlessly
mixed with the simulator so the mix itself is invisible to the audience.

---

## 1. Hardware inventory and exact role assignment

| Device | Role in the demo | Feeds |
|---|---|---|
| **Modulino Knob #1** | the only knob wired; **thermostat dial** | `temp_c` — real, physically turned on stage |
| **Modulino Knob #2** | **cold spare** — pre-configured, on the bench, not wired unless #1 fails | swap-in only |
| **Modulino Buttons** (3-button board) | manual override / recovery path | button0 = toggle lights, button1 = toggle AC/fan, button2 = force re-scan |
| **Smart bulb** (Kasa) | the `lights` load | real device, real wattage read-back |
| **Smart plug #1** (Kasa) | the `ac` load — **fan physically attached, relabeled "AC"** | real device, real wattage read-back |
| **Smart plug #2/#3** (Kasa, if you have them) | optional second room / phantom-standby prop (a phone charger or lamp) | see §5 stretch beat |
| **Phone / simulator page** | presence, occupancy, lux, humidity — the signals with no physical sensor | `/api/sensor`, `/api/presence` |
| **Dashboard** | the audience-facing screen; projector | `/` |
| **`/ask` page** | the interactive Q&A moment | `/ask` |

**Why the fan-as-HVAC works, and why you should say so out loud:** the rules engine and the
Kasa integration match on the **load name** (`ac`), not the device type — this is already flagged
as a real gotcha in `07_QUAD_SESSION_LOG.md` ("rules match on load NAME, not device"). A fan
plugged into the smart plug labeled `ac` in `board.env` behaves identically to the code as a real
air conditioner. **Do not disguise this** — it's a better story told straight: *"We're using a fan
as a stand-in for a compressor — the software has no idea, because it reasons about a load by name
and wattage, not brand. Swap the fan for a real 1.1 kW A/C and nothing here changes."* That is a
legitimate architecture claim, not a cheat, and saying it plainly earns more credit than hiding it.

**Why one Knob wired + one spare is right:** the firmware has a fixed I2C address per Knob
(0x3A/0x3B) and nothing multiplexes two on one Qwiic bus today. A cold spare, pre-paired and
tested, swaps in during a "the Qwiic cable came loose" recovery in under 10 seconds — genuinely
useful insurance, not wasted hardware.

---

## 2. The seamless mix — how real and simulated interleave without a seam

The mixing already exists in the code; you're staging it, not building it.

- **`temp_c` comes from the real Knob** whenever it's connected (`MCU_SIGNALS=temp_c` in
  `board.env`). Turn it, the dashboard's temperature number moves within ~1 second.
- **`lux`, `humidity`, `occupancy`** come from the **simulator/phone page** (`POST /api/sensor`).
  This is a declared simulation, and the honesty labelling is already in the code
  (`lux_src`, `hum_src`, `occ_src` fields) — **use it, don't hide it.**
- **Load state and wattage** come from the **real Kasa devices**, polled every `KASA_POLL_S`
  seconds and read back after every command — this is real, measured, not modelled.
- **Presence** comes from the **phone** (manual toggle or geofence).

**The one thing that makes the mix look intentional instead of improvised:** name it once, early,
in one sentence, and never apologise for it again:

> *"Temperature is a real dial in my hand. Occupancy and daylight are simulated from this phone —
> we don't have a PIR or a light sensor wired yet, and we say so on screen, not just to you.
> The lights and the fan are real smart devices, switching for real."*

Judges credit a system that **labels its own inputs** far more than one that fakes uniformity.
That sentence is worth more than pretending everything is real hardware.

---

## 3. The four beats, in the order that builds

Each beat maps to one thing from §0. Timings assume the 5-minute slot from `02_DEMO_SCRIPT.md`;
adjust down for the 45-second video using `demo_idea.html`'s five-act shape as the skeleton.

### Beat A — the physical proof (existing script, unchanged in spirit)

Home, fan (as "AC") running, occupied. Toggle presence to **away** on the phone. Within one
5-second eval cycle: a critical finding appears, priced. Approve it. **The bulb visibly goes
dark or the fan visibly stops**, wattage on the dashboard drops from a real measured number to
0.0 W, the finding moves from *avoidable* to **realized**.

This is unchanged from `02_DEMO_SCRIPT.md` Beat 4, just re-grounded in real Kasa devices instead
of the retired servo.

### Beat B — the AI *decides*, live (new — this is the gap the old script has)

The old script's Differentiator 1 was "the model narrates, Python computes" — true, but passive.
You now have `hub/planner.py` (P0-B) actually **ranking findings and explaining the order**, not
rewording one at a time. Stage it with **two simultaneous findings**, not one:

1. Set up two things at once: fan/"AC" running while away (critical, real waste) **and** the
   dryer-equivalent load running inside the on-peak window (legitimate, just badly timed).
2. Trigger the plan (dashboard shows the ranked list with `situation` and `why_this_order` text
   from `planner.py`).
3. **Read the model's own words**, not yours: *"defer the [dryer], it's legitimate — kill the
   [AC], it's pure waste right now."* That sentence is the model's decision, not a hand-written
   line. Point at the screen when you say it.

> *"Watch what it decides, not just what it says. Two things are wrong at once. It doesn't just
> list them by dollar value — it explains that one is a legitimate load that only needs to move in
> time, and the other should stop right now. That ranking is the model's job. The numbers next to
> it are Python's."*

**This call measures 11.5 s p50 — a real pause, not instant** (`code/README.md`'s "Where the AI
actually runs" table). Do not try to talk through it as if it were snappy; that reads as dead air
or a stall. **Stage the wait on purpose:**

> *"I'll trigger the plan now — this one call is doing real reasoning across everything at once,
> so give it a few seconds."* [pause, let it run] *"There — two findings, ranked, and it explains
> why."*

Naming the wait turns 11.5 s from a liability into more evidence: it's the one number in the
whole demo big enough to *feel*, and feeling it is what makes "an LLM call, not a lookup" true in
the room, not just on a slide. **It also only fires once per change of the finding set** — say
that too, it's the reason it's cheap in aggregate despite the per-call cost.

**Flag `AI_PLAN=1` must be exported before the demo.** It defaults OFF. Put this in the
pre-demo checklist (§6), not just in your memory.

### Beat C — the AI gets caught, on stage (new — the most novel beat available to you)

`hub/provenance.py` mechanically checks every number the LLM emits against the numbers it was
actually given. This is genuinely rare among hackathon AI demos — most teams *promise* their model
doesn't hallucinate; you can *show* the check running.

Two ways to stage it, in order of preference:

1. **If the `/ask` page shows a verified/unverified badge on every answer (per the P2-E plan)** —
   ask a real question (*"why is my bill high?"*), point at the badge: **verified**. Then say the
   sentence that lands it:

   > *"That badge isn't decoration. Every number the model just said was checked, mechanically,
   > against the numbers Python actually computed. If it had invented one, that badge would say
   > unverified — not because we told the model to behave, because we checked."*

2. **Fallback if the UI badge isn't wired by demo day:** run `python hub/provenance.py` in a
   terminal live (it has a self-test that plants a hallucinated number and shows it getting
   caught) — less slick, but the mechanism itself still lands and it's a guaranteed-to-work
   backup that has zero dependency on live inference timing.

**Confirmed: the badge exists** — `code/ask/index.html` renders `verified`/`unverified` tags and
`hub/server.py` wires the check into `/api/ask`'s response (this was checked directly in the
running code, not assumed). **Use path 1.** Keep path 2 only as an if-Wi-Fi-dies fallback.

### Beat D — the refusal (existing script, unchanged — do not touch this beat)

Turn the **real Knob** past 27 °C. Tap Approve on the AC/fan recommendation. **HTTP 409, nothing
switches, the reason is on screen.** This remains your single strongest 30 seconds and the
plan's own three self-ratings agree — leave `02_DEMO_SCRIPT.md`'s Beat 5 language almost verbatim,
it already frames this correctly. The only change: it's a real physical dial in your hand turning
in front of the judges, not a sensor-injection curl command off-screen. **Narrate the turning, not
just the result** — "watch the number climb as I turn this" — the physical motion is the point.

### Beat E — the edge tier, named but brief (new, keep to one sentence + one visual)

Do not build a live scenario around this — the 3 a.m. anomaly case is real but slow to stage live
(you'd need to fake the clock). Instead: one sentence over the architecture slide, pointing at the
UNO Q box:

> *"There's a fourth thing running that you're not watching: a tiny learned model on this board's
> own CPU, scoring every sample for patterns no fixed rule would catch — like the AC running at
> 3 a.m. in an occupied, comfortable room. It runs in **30 microseconds**, measured on this exact
> board — which is why it's on the CPU and not the NPU."*

**There is now a real on-screen prop for this, not just a verbal claim.** Every recommendation
card on the dashboard carries a small `rule` or `learned · 0.999` badge (hover shows: *"Edge
classifier on the UNO Q — a learned score, not a fixed threshold. Trained on simulated data."*).
If you can get one learned finding to surface during rehearsal, **point at that badge** instead of
only gesturing at the architecture slide — a green `rule` tag next to a blue `learned` tag on the
same screen is a stronger, more concrete visual than the sentence alone, and the honesty caveat is
already written into the tooltip rather than something you have to remember to say.

**If a judge asks how accurate it is:** say the number (holdout accuracy ~0.97) **and immediately
add what it actually measures** — how separable a synthetic training distribution is, not
real-world accuracy. `code/README.md` states this in the same breath as the number, on purpose;
match that discipline on stage. Claiming 97% real-world accuracy from one day of simulated
training data is the one honesty slip that would visibly contradict everything else the project
stands for — do not let enthusiasm produce it live.

This is where `hub/benchmark.py`'s three-tier table (P1-D, presented as µs → ms → s) earns its
slide — see `02_DEMO_SCRIPT.md`'s existing Measured Performance section, which already has the
right shape and just needs the edge-anomaly row added (already done in the deck per
`presentation_template.html`'s current state).

**One safety note worth knowing before you stage Beat E next to Beat D.** A real bug existed
where approving a *learned* A/C finding at 29.5 °C returned success instead of the R7 refusal —
the load lookup defaulted unmapped rule names to `"lights"`, so a learned finding about the A/C
would have silently switched the wrong device while the guardrail, keyed off that wrong device
name, saw no comfort risk. This has been found and fixed (`07_QUAD_SESSION_LOG.md`, "a learned
finding resolved to the wrong load and skipped the comfort gate") and is verified: approving a
learned A/C finding at 29.5 °C now correctly returns HTTP 409. **If you want to combine Beats D
and E into one moment** — turn the Knob hot, let the *learned* detector (not a fixed rule) flag
the A/C, then get refused — that is now a safe, verified combination and arguably a stronger
single beat than doing them separately. Worth trying in rehearsal.

---

## 4. Suggested beat order for the live 5-minute slot

Reordering `02_DEMO_SCRIPT.md`'s existing skeleton to fit the new beats without adding time.
**Beat B's timing is wider than a first pass suggests — it has to absorb a real 11.5 s model
call, not just talking time.** Budget for the wait explicitly rather than hoping to talk over it:

| Beat | What | Was in old script | Time |
|---|---|---|---|
| Problem | unchanged | Beat 1-2 | 0:00–0:40 |
| **A** — physical proof | fan/bulb, real Kasa | Beat 3-4, re-grounded | 0:40–1:30 |
| **B** — AI decides | NEW, planner ranking two findings — **includes the 11.5 s wait** | *(new, replaces old "model narrates" framing)* | 1:30–2:35 |
| **D** — refusal | real Knob, unchanged in spirit | Beat 5 | 2:35–3:05 |
| **C** — provenance catch | NEW, `/ask` badge (streamed, ~3.4 s) | *(new)* | 3:05–3:35 |
| **E** — edge tier, one line | NEW, over the architecture slide | *(folded into old Beat 6-ish)* | 3:35–3:50 |
| Measured performance | unchanged, +edge/plan/Q&A/provenance rows | Beat 7-adjacent | 3:50–4:25 |
| Close | unchanged | — | 4:25–4:45 |

This drops the old Cloud-AI-100 beat entirely (that device is gone per the architecture pivot —
the weekly deep report now runs on the X Elite hub itself) and drops the daylight/context-fusion
beat to make room — keep it only as a Q&A answer, it's a fine one but not worth live stage time
against two brand-new, more novel beats.

**If rehearsal shows Beat B genuinely doesn't fit in 65 seconds** (very possible — an unrehearsed
11.5 s pause always feels longer than it measures), the honest cut is to **pre-trigger the plan
30 seconds before you get to Beat B on stage**, off-camera, so it's already computed and you're
narrating a result instead of narrating a wait. Say so if you do: *"I triggered this a moment ago
so we're not standing here for it — here's what it decided."* That is still a live system, just
not a live latency demo; the µs→ms→s story already lives on the Measured Performance slide, so
Beat B's job is showing the *decision*, not re-proving the timing.

---

## 5. Stretch beat, only if time allows in rehearsal (optional)

If you have a genuine third Kasa plug free: wire it to a phone charger or small lamp as a
**phantom-standby prop** for R5. Walk away with it plugged in and idle; after the away-grace
period it surfaces as a small, separate finding, distinct from the big fan/"AC" finding. This
demonstrates the rules engine catching a *small*, boring waste alongside a *big*, obvious one —
which is a good answer to "does this only catch dramatic waste?" Cut this first if you're over
time; it adds breadth, not a new capability.

---

## 6. Pre-demo checklist — hardware + flags, do this every single rehearsal

```bash
# On the board (ADB), confirm the flags this demo depends on are exported before starting:
adb shell "cat /path/to/board.env | grep -E 'MCU_SIGNALS|KASA_LOADS'"
# Expect: MCU_SIGNALS=temp_c   and   KASA_LOADS=lights,ac

# On the hub, confirm the AI flags are ON — they default OFF, this is the #1 way to
# rehearse the wrong demo:
echo $AI_PLAN     # must be 1 for Beat B
echo $AI_ANOMALY  # must be 1 if you want the edge tier live (Beat E is a slide either way)
echo $AI_ASK      # must be 1 for Beat C path 1

python smoke_test.py   # 32/32, always, before anything else
```

- [ ] Knob #1 wired, turning it moves `temp_c` on the dashboard within ~1 s
- [ ] Knob #2 (spare) pre-paired, in your bag, address matches, not wired
- [ ] Fan physically plugged into the plug labeled `ac` in `board.env`; test it switches
- [ ] Bulb visibly in frame for the camera/judges — not behind you
- [ ] `AI_PLAN=1`, and (if using) `AI_ASK=1` exported on the hub **before** starting the server
- [ ] `/ask` page checked for a verified/unverified badge — confirms which Beat C path to script
- [ ] One full dry run with a timer, hardware and all, not slides-only
- [ ] Recorded backup video exists in case Wi-Fi or a Kasa device misbehaves live

---

## 7. What to do with `02_DEMO_SCRIPT.md`

That file is still accurate on **tone, timing discipline, and Beats A/D** — keep its "stage
discipline" section verbatim, it's good advice regardless of hardware. It needs a rewrite on:

- Architecture beat: drop the servo/PIR language, drop Cloud AI 100 as a separate device (already
  fixed in the deck itself, not yet in this script)
- Insert Beats B and C as new script text (use §3 above as the draft)
- Q&A prep: add "why a fan for the AC?" (answer is in §1) and "what does the provenance check
  actually verify?" (answer is Beat C's own explanation)

This is a mechanical follow-up once you've rehearsed §3 once and know it holds up at the actual
timings — do the rewrite after one live rehearsal, not before, so the script matches what you
actually say rather than what you planned to say.

---

# §8 — The 2-minute video: shot list

Six segments, shot independently and in any order, retaken freely. The whole value of a
recorded demo is that **no beat has to survive a live network**. Total 120 s.

| # | Shot | Length | What is on screen |
|---|---|---|---|
| 1 | **Hook — physical, no UI** | 0:00–0:12 | A finger presses a button on the UNO Q. The bulb across the room goes dark. Then the bench: board, knob, bulb, plug. |
| 2 | **Sense → physical act** | 0:12–0:38 | Presence → away. Finding appears, priced. Approve. **Bulb goes dark**, dashboard watts fall to 0.0, card flips to *realized*. |
| 3 | **The AI decides** | 0:38–0:56 | Plan panel: two findings ranked, the dryer **deferred** in the model's own words. Latency captioned (§8.1). |
| 4 | **The refusal** | 0:56–1:20 | **Press the knob** → 29.5 °C on screen → Approve the A/C → **HTTP 409**, nothing switches, reason visible. |
| 5 | **The AI gets caught** | 1:20–1:42 | `/ask` → answer streams → **`unverified`** badge listing the invented figure. |
| 6 | **Three tiers + close** | 1:42–2:00 | µs → ms → s. 30.6 µs on the board, 3.3 s on the NPU, 110 µs to check every number. |

**If it overruns, cut shot 3 first.** Its job is "the model decides" — which shot 4 and
shot 5 already demonstrate with more drama. Fold its one good line ("it deferred the dryer
as legitimate and killed the A/C as waste") into shot 6 as a caption.

## 8.1 The one editing rule

You may cut dead time. You may not imply speed the system does not have.

- Cutting the 11.5 s plan call is fine — **show `plan synthesis · 11.5 s · one call per
  change` on screen while it happens.** That number is an asset, not an embarrassment: it
  is the only latency in the demo big enough to *feel*, and feeling it is what makes "a
  real model, not a lookup" credible.
- Do **not** speed-ramp the bulb switching. That is genuinely ~2 s and should play real.
- Retaking until the model produces a good *ranking* is directing. Retaking until it
  produces a *number you liked* is fabrication. Shot 5 is literally about that line.

## 8.2 Shot 4 is the money shot — direct it properly

**Press the knob, do not turn it.** One press toggles 22 °C ↔ 29.5 °C (verified in
`sketch.ino::pollKnobButton`), crossing R7's 27 °C limit in a single click. Turning takes
~60 detents and is dead screen time.

Three cuts, ~8 s of action, no waiting: finger presses knob → dashboard reads 29.5 °C →
tap Approve → **409 refusal banner with its reason**.

## 8.3 The shot the plan misses — the physical button

§1 lists the Modulino Buttons only as a "manual override / recovery path". For a video
that badly undersells the most visceral asset available:

> **A finger presses a button on the board. A real bulb across the room goes dark. No
> browser anywhere in frame.**

Five seconds, no UI, no network to explain. It is the clearest possible statement of
sensor → actuator for Archetype E, and it earns the "this is real hardware" claim *before*
asking anyone to trust a screen. Open with it.

Button A = bulb, button B = the `ac` plug, button C = rescan. The LEDs show
**device-confirmed** state, not intent — if the LED lights, the bulb really switched. Say
that in one clause; it is a small, true, unusual detail.

---

# §9 — Corrections: verified hardware facts that change the shots

Checked against the live devices on 2026-08-06.

## 9.1 ⚠ The bulb is at 9 % brightness — it draws 1.7 W

```
KL120  on=False  watts=0.0  brightness=9
```

Shot 2's entire payload is a wattage number falling to zero. **1.7 W → 0.0 W is not a
number anyone will feel.** At full brightness this bulb draws ~10 W — an earlier session
logged 10.8 W → 0.0 W.

Set it before filming. It also makes the bulb going dark **visibly** darker on camera,
which is the actual shot:

```bash
adb shell "cd /home/arduino/ai-home-energy-concierge/code/arduino && set -a && . ./board.env && set +a && ~/energy-venv/bin/python3 -c 'import asyncio
from kasa import Discover, Module
async def m():
    d = await Discover.discover_single(\"192.168.86.49\", timeout=8)
    await d.update()
    await d.modules[Module.Light].set_brightness(100)
    print(\"brightness 100\")
asyncio.run(m())'"
```

## 9.2 ⚠ The `ac` load reads 0 W — nothing will drop

```
HS110  on=False  watts=0
```

The plug meters correctly, but the **space heater's own switch is off**, so it measures
nothing even when energised. §1 assumes a fan is attached; right now it is the heater.

Either **plug the fan into that outlet** (a table fan draws ~40–70 W — a good visible
number, safe to leave running), or **film the wattage drop on the bulb only** and use the
`ac` load purely for the refusal beat, which needs no wattage at all.

Do **not** switch the heater on for a 1.5 kW drop. A heater cycling on camera in an empty
room is the one shot that could read as unsafe, and it argues against your own thesis.

## 9.3 ⚠ Plug #3 (EP40) has no energy meter

```
EP40  watts=no energy meter
```

§5 proposes it as a phantom-standby prop. It works for on/off **state**, but its wattage
would be **modelled, not measured** — the dashboard will correctly label it
`simulated · modelled` beside the bulb's `real device · measured`. That contrast is honest
and fine, but do not narrate it as a third *measured* device. Cut the stretch beat from a
2-minute video regardless: breadth, not a new capability.

## 9.4 Only one Knob is on the bus

§1 assumes Knob #1 plus a cold-spare Knob #2. Telemetry reports `"nodes":"KB"` — one Knob,
one Buttons board. If a second Knob exists in a drawer the cold-spare advice stands; if
not, delete that row rather than leaving a checklist item nobody can tick.

## 9.5 The simulator can no longer fake the bulb — by design

Loads backed by a real Kasa device are marked `metered`, and the simulator now refuses to
POST state for them (it logs *"real metered device — simulator will not override it"*).
Before this, the simulator would show `on / 12 W` while the dashboard showed the true
`0 W`, because the publisher re-published real state within 5 s.

**For filming this means:** you cannot stage the bulb from the simulator. Switch it for
real — via Approve, via the Modulino button, or via the Kasa app. That is the correct
behaviour and a better story, but it will surprise you mid-shoot if you expect otherwise.

---

# §10 — Corrected pre-demo checklist

§6's commands do not work as written. These do.

```bash
# 1. Board config — real path, not a placeholder
adb shell "grep -E 'MCU_SIGNALS|KASA_LOADS|KASA_LIGHTS_HOST|KASA_AC_HOST' /home/arduino/ai-home-energy-concierge/code/arduino/board.env"
# Expect MCU_SIGNALS=temp_c, KASA_LOADS=lights,ac, 192.168.86.x hosts

# 2. CRLF check — the worst failure mode on this project (log §18.1)
adb shell "grep -c $'\r' /home/arduino/ai-home-energy-concierge/code/arduino/board.env"
# Non-zero => the publisher serves INVENTED sensor data while looking perfectly healthy

# 3. Publisher on the REAL knob, not synthetic
adb shell "grep -E 'MCU monitor connected|SYNTHETIC|MQTT connected' /tmp/publisher.log"

# 4. AI flags — check the RUNNING hub, not your shell.
#    `echo $AI_PLAN` tells you nothing: the server reads the flags at startup, so a
#    correct shell variable and a hub started without it look identical from outside.
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/ask     # 200 => AI_ASK on
curl -s http://localhost:8000/api/state | grep -c '"plan"'             # 1  => AI_PLAN on

# 5. Smoke test — STOP THE PUBLISHER FIRST (log §19.1)
adb shell "pkill -9 -f '[u]no_q_publisher.py'"
cd code && python smoke_test.py     # 32/32. With the publisher live you get a spurious
                                    # 23/25: its real 1.7 W overwrites the test fixtures.
# then relaunch the publisher
```

Start the hub with all three flags — they default **off**:

```bash
AI_ANOMALY=1 AI_PLAN=1 AI_ASK=1 python hub/server.py
```

- [ ] Bulb brightness set to 100 (§9.1) — else shot 2 shows 1.7 W
- [ ] Fan in the `ac` outlet, or shot 2 films the bulb only (§9.2)
- [ ] Knob **press** tested — one click gives 29.5 °C (§8.2)
- [ ] Modulino button tested — press switches the real bulb, LED confirms (§8.3)
- [ ] `/ask` returns 200 and a badge renders
- [ ] `"plan"` present in `/api/state`
- [ ] Whole video shot and cut with time to spare — it is the submission, not a backup

---

# §11 — Protect this one claim, and aim shot 5 at it

§3 Beat C is right that most teams *promise* their model does not hallucinate while you
can *show* the check. That claim got stronger since the plan was written: the verifier
caught the model doing forbidden arithmetic during development, **unprompted and
unanticipated**:

```
Q: "What if I shift the dryer to 9 PM?"
A: "...reducing cost from $0.39 to $0.19, saving $0.20."   ->  UNVERIFIED  [0.19]
```

`$0.39` was in the digest. `$0.19` was not — the model computed it.

**Ask exactly that question in shot 5.** It reliably tempts the model into arithmetic, and
a video lets you retake until it does.

An **amber `unverified` badge is worth more on camera than a green one.** A green badge
proves the model behaved; an amber one proves *the check works* — and only the second is
evidence about your system rather than about the model's mood that afternoon. Say the line
that lands it:

> "That badge isn't decoration. Every number the model just said was checked, mechanically,
> against what Python actually computed. It got caught. Not because we told it to behave —
> because we checked."

If it refuses to misbehave after several takes, `python hub/provenance.py` deterministically
plants and catches one (7/7 cases). Less slick, equally true.

---

# §12 — What I would cut, and why

Honest editorial view of the plan against a 120-second budget:

- **Cut §5's stretch beat.** No new capability, and §9.3 shows the third plug cannot be
  narrated as measured anyway.
- **Cut Beat E as a spoken beat.** The `learned · 0.999` badge does the work silently in
  shots 2–4; a sentence explaining the edge tier belongs in shot 6's table, not its own
  segment. The plan already half-concedes this ("one sentence + one visual").
- **Keep §1's fan-as-A/C paragraph verbatim.** Saying it straight is the right call and it
  is the best-argued page in the document.
- **Keep §2's "name the mix once" sentence.** In a 2-minute video it becomes a single
  caption rather than spoken narration, but it must still appear — the honesty labelling
  is genuinely in the code and it costs three seconds to earn credit for it.

The plan's instinct throughout — label the simulation, do not hide the fan, name the
latency — is the right one and is what makes the rest credible. None of my corrections
change that; they fix numbers and commands, not judgement.

---

# §13 — Adversarial review response (2026-08-06)

A devil's-advocate review of the AI work and the video pivot. Both actionable items are
fixed; recording the outcomes because the reasoning matters more than the verdict.

## 13.1 ✅ FIXED — the silent `"lights"` fallback was narrowed, not removed

**The finding, and it was correct.** Commit `fda9f8b` fixed the learned-A/C incident but
left `_load_from_rule` ending in `return "lights"` — the exact mechanism that made the
incident possible, waiting for the next thing that touches Recommendation construction.

**One correction to the review's reasoning, in the safer direction.** It judged the line
currently unreachable because `Finding.load_key` is required. Nearly right:
`Recommendation.load_key` **defaults to `""`**, so unreachability depends on every
construction site remembering to pass it. Two do today. A third — a new rule, a
deserialised object, a refactor — inherits the guess with nothing to flag it. The review
called it tech debt; it was slightly closer to live than that.

**Fixed in `493c389`:** returns `""`, and `/api/apply` refuses with **422** instead of
acting. Behaviour unchanged for the six mapped rules and anything carrying a `load_key`.

Verified:
```
unmapped rule + empty load_key -> ''   + logged refusal
learned finding with load_key  -> 'ac'
mapped rule (unchanged)        -> 'ac'
```

## 13.2 ✅ FIXED — "reliably tempts the model" was an unmeasured claim

**The finding was fair**: whether shot 5 gets an amber badge is a model output, not a
structural guarantee, and "retake until it does" reads badly if the model behaves.

Rather than soften the wording, it was **measured** — 3 trials per question, on the fixed
fixture and again on live hub state:

| Question | Fixture | Live | Caught |
|---|---|---|---|
| **"What if I shift the dryer to 9 PM?"** | **3/3** | **3/3** | `0.19` / `0.00032` |
| "What percentage of my bill is waste?" | 3/3 | **0/3** | `1.427`, `50` |
| "How much would I save over a year?" | 0/3 | — | (model correctly declines) |
| "What is the total cost of all findings combined?" | 0/3 | — | (declines) |

**Both conclusions are true at once.** The review's general caution is vindicated —
hit rate *is* state-dependent, and the second row proves it: 3/3 becomes 0/3 when the
state changes. But the specific question the docs recommend is robust, at 6/6 across two
different states.

`02_DEMO_SCRIPT.md` now states the measured rate, warns against substituting an untested
question, and says to stop grinding after two or three green takes and use the
deterministic self-test instead. Filming a failure you want badly enough to keep rolling
for is the wrong instinct on a project built around not overstating things.

## 13.3 The review's own near-miss, worth preserving

It first concluded the `$0.39 → $0.19` incident was invented for dramatic effect, having
searched only `provenance.py`'s self-test fixtures (which use different numbers). A wider
search found it dated in `07_QUAD_SESSION_LOG.md` §19.6 and reproduced verbatim in
`code/README.md`.

Worth keeping because it is the shape of the mistake a judge could make: a claim that
looks fabricated because the obvious place to check is not where the evidence lives. **If
asked about the provenance incident, point at the session log entry with its date, not at
the self-test** — the self-test proves the mechanism, the log proves the incident.

## 13.4 Confirmed, no action

- §8.1's editing rule ("cut the wait, caption the number") — the right discipline for a
  recorded demo.
- The 120 s timing math, with the 11.5 s planner call handled by cutting rather than
  narrating.
- §9's three hardware corrections are load-bearing, not padding: the bulb at 9 % and the
  A/C plug at 0 W would each have silently ruined a shot.
- §9.5 (simulator refuses metered loads), verified against `simulator/index.html`.
- §10's "check the running hub, not your shell".
- The knob-press preset straddling R7's 27 °C threshold, verified against firmware.

## 13.5 Standing item this review did not raise

`demo.mp4` still does not exist, and it is the submission. It outranks every remaining
item in this document.
