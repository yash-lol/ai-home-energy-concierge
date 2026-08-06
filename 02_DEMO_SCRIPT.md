# Demo script — the 2-minute video

**This video IS the submission demo, not a backup for a live run.** That changes how you
shoot it: every beat can be retaken, nothing has to survive a live network, and dead time
gets cut rather than narrated.

Companion docs: `10_DEMO_PLAN.md` §8 (shot list rationale), §9 (hardware corrections you
must apply before filming), §10 (pre-shoot checklist that actually works).

**Previous version of this file described the retired breadboard/servo/Cloud-AI-100 build.
All of that is gone** — sensing is Modulino over Qwiic, actuation is real Kasa devices, and
the weekly report runs on the X Elite hub itself.

---

## The 120 seconds

| # | Shot | Time | Action on camera |
|---|---|---|---|
| 1 | Hook | 0:00–0:12 | Finger presses a board button → **real bulb goes dark**. No screen in frame. |
| 2 | Sense → act | 0:12–0:38 | Presence → away → finding appears → Approve → bulb dark, watts → 0.0, card flips to *realized*. |
| 3 | AI decides | 0:38–0:56 | Plan panel: two findings ranked, dryer **deferred** in the model's words. |
| 4 | The refusal | 0:56–1:20 | **Press the knob** → 29.5 °C → Approve the A/C → **HTTP 409**, nothing switches. |
| 5 | Caught | 1:20–1:42 | `/ask` → answer streams → **amber `unverified`** badge, invented figure listed. |
| 6 | Three tiers | 1:42–2:00 | µs → ms → s table. Close on the realized dollar figure. |

**Over budget? Cut shot 3.** Shots 4 and 5 already prove the model decides, with more
drama. Fold its line into shot 6 as a caption.

---

## Shot 1 — Hook (0:00–0:12)

- **Do:** press button A on the Modulino Buttons board. The bulb goes dark. Hold two beats.
  Then a slow pan across the bench: UNO Q, knob, bulb, plug.
- **Caption:** *"A button on the board. A real bulb. No cloud, no phone."*
- **Must be in frame:** the finger, the board, the bulb — all three, no browser.
- **Why first:** it earns "real hardware" before asking anyone to trust a screen. The LED
  on the button confirms the *device* switched, not just that a command was sent.

## Shot 2 — Sense → act (0:12–0:38)

- **Do:** on the simulator, toggle presence to **away**. Wait one 5 s eval cycle. A critical
  finding appears with a price. Tap **Approve**. The bulb goes dark; the dashboard wattage
  falls to 0.0 W; the card flips to *realized*.
- **Caption:** *"Detected, priced, approved by a human, physically switched."*
- **Retake if:** the wattage shown is 1.7 W — set the bulb to 100 % brightness first
  (`10_DEMO_PLAN.md` §9.1), it should read ~10 W falling to 0.0.
- **Do not** try to stage the bulb from the simulator. Metered loads are device-owned by
  design and the simulator will refuse (§9.5).

## Shot 3 — The AI decides (0:38–0:56)

- **Do:** have two findings live at once — the A/C wasting while away, and a dryer-class
  load inside the peak window. Show the plan panel ranking them.
- **The point:** the deterministic path would sort by dollar value and put the dryer first.
  The model **defers** the dryer as legitimate-but-mistimed and promotes the A/C. Read the
  model's own `why_this_order` text on screen, not a line you wrote.
- **Caption while it computes:** `plan synthesis · 11.5 s · one call per change`
- **Editing:** cut the wait, keep the caption. See the honesty rule below.

## Shot 4 — The refusal (0:56–1:20) — the money shot

- **Do:** **press** the knob (one click, 22 °C → 29.5 °C). Cut to the dashboard showing
  29.5 °C. Tap Approve on the A/C recommendation. **HTTP 409** — nothing switches, the
  refusal reason is on screen.
- **Caption:** *"It refuses to execute its own advice."*
- **Do not turn the dial** — that is ~60 detents of dead screen time.
- **Why it lands:** every other team's AI does what it is told. Yours declines, with a
  stated reason, and the guardrail sits in front of the actuator rather than in the prompt.

## Shot 5 — Caught (1:20–1:42) — the most novel beat

- **Do:** open `/ask`, type **"What if I shift the dryer to 9 PM?"**. Let the answer stream.
  Point the camera at the badge.
- **Aim for amber.** An `unverified` badge proves *the check works*; a green one only proves
  the model behaved that afternoon.
- **Measured hit rate: 6/6.** That question produced an `unverified` badge on 3/3 takes
  against the fixed fixture and 3/3 against live hub state. Expect it to land, but it is a
  model output, not a guarantee — budget two or three takes, not forty.
- **Do not substitute a different question without testing it.** Hit rate is
  state-dependent: *"What percentage of my bill is waste?"* also scored 3/3 on the fixture
  and then **0/3** on live state. The dryer question is the one that has been checked in
  both.
- **Guaranteed fallback:** `python hub/provenance.py` plants and catches a hallucinated
  number deterministically (7/7 cases, no model involved). Less slick, always works.
- **Caption:** *"$0.39 was given to it. $0.19 was not — it did the arithmetic anyway. We
  caught it mechanically, in 110 µs."*
- **If it comes up green two or three takes running,** do not keep rolling hoping for
  amber — switch to `python hub/provenance.py` on camera and narrate the same point. A
  green badge is still a true result; grinding for a failure you want to film is the
  wrong instinct on a project built around not overstating things.

## Shot 6 — Three tiers and close (1:42–2:00)

- **Do:** the µs → ms → s table, then land on the realized dollar figure.
- **Say:** 30.6 µs on the board's A53 · 3.3 s on the Hexagon NPU · 110 µs to check every
  number the model emitted.
- **Close on the number, not the architecture.**

---

## The one editing rule

You may cut dead time. You may **not** imply speed the system does not have.

- Cutting the 11.5 s plan call is fine — **show the number on screen while you do it.**
- Do **not** speed-ramp the bulb switching; it is genuinely ~2 s and should play real.
- Retaking until the model gives a good *ranking* is directing. Retaking until it gives a
  *number you liked* is fabrication — and shot 5 is literally about that line.

---

## Before you shoot

Full checklist in `10_DEMO_PLAN.md` §10. The four that will actually bite you:

1. **Bulb brightness → 100.** Otherwise shot 2 shows 1.7 W and lands on nobody.
2. **Start the hub with all three flags** — they default off:
   `AI_ANOMALY=1 AI_PLAN=1 AI_ASK=1 python hub/server.py`
3. **Check the CRLF trap:**
   `adb shell "grep -c $'\r' .../board.env"` — non-zero means the publisher is serving
   **invented** sensor data while looking perfectly healthy.
4. **Verify flags on the running hub, not your shell:** `curl /ask` → 200, and `"plan"`
   present in `/api/state`. A correct shell variable and a hub started without it look
   identical from outside.

---

## Q&A prep — the questions you will get

**"Where do the wattage numbers come from?"**
> Both, and the dashboard says which. The bulb and the A/C plug are metered devices — those
> watts are **measured** and read back from the device after every switch. Anything without
> a meter falls back to published DOE/Energy Star figures, and every entry carries its
> source string. On screen you'll see `real device · measured` next to `simulated ·
> modelled`. We'd rather label the difference than average it away.

**"What is the AI actually deciding?"**
> Three things, in three different places. A learned classifier on the board's CPU decides
> whether the current state is *unusual* — it catches cases no fixed threshold expresses,
> like the A/C running at 3 a.m. in an occupied, comfortable room, where all seven rules
> fire nothing. The NPU model decides the *order* to act in and what can wait. And it
> answers questions. What it never decides is a number — that stays in Python.

**"What if the LLM hallucinates a saving?"**
> Two independent defences. Structurally, every numeric field is overwritten from the
> deterministic source after the call, so prose can never move a figure. And mechanically,
> we extract every number the model emitted and check it appears in what it was given —
> `hub/provenance.py`, 110 µs per response. It caught the model doing forbidden arithmetic
> during development, unprompted: it was given $0.39, and wrote $0.19. Most teams answer
> this question with "we told it not to." We answer with a violation count.

**"Isn't the anomaly model trained on fake data?"**
> Yes — 14 simulated days, holdout accuracy 0.9714. And that number measures how separable a
> *synthetic* distribution is, not real-world accuracy. It says so in the model file, in the
> UI tooltip, and in the README, in the same breath as the number. In deployment it would
> retrain on real logged history. We'd rather label it than imply otherwise.

**"Why not run the LLM on the UNO Q?"**
> That board is a quad Cortex-A53 with no on-device AI stack at all — no DSP skel, no SNPE,
> its GPU is software rasterisation. A 4B model there is physically wrong. But AI at the
> edge doesn't have to mean an LLM: a trained classifier runs there in **30.6 µs**, in pure
> Python, with zero new dependencies. That split is the design, not a compromise.

**"Is it really running on the NPU?"**
> Yes — GenieX with the `qairt` runtime, which is NPU-only, on a pre-compiled AI Hub bundle.
> We measured the deterministic path as a control, so we can tell you what the model costs
> versus the arithmetic: 3.3 s against 0.014 ms.

**"Did you use QUAD?"**
> For profiling. We deliberately skipped `generate_code` — the project sheet flags it as
> blocked by gap G6, the UNO Q sensor/actuator codegen. Rather than wait or ship mock
> output, we wrote and tested that layer by hand.

**"Isn't approving each action tedious? Why not full automation?"**
> Deliberate. Watch R7 refuse to switch off the A/C at 29.5 °C — that's the system declining
> to execute its own advice, and the gate sits in front of the actuator, not in the prompt.
> Full autonomy is a small code change; earning the trust to enable it is the hard part.

**"That's a fan, not an air conditioner."**
> Correct, and the software has no idea. It reasons about a load by name and measured
> wattage, not brand. Swap the fan for a real 1.1 kW compressor and nothing in the code
> changes. We'd rather show you the substitution than hide it.

**"How do you know it's really an open window in R4?"**
> We don't, and we say so. It's a heuristic — A/C running 15+ min, temperature not falling,
> humidity above 60 %. The evidence list literally says "HEURISTIC: we infer an open window,
> we do not sense it directly."

**"Does it scale to a whole house?"**
> The MQTT contract is already per-room and per-load, and the rules iterate over rooms.
> Adding rooms is adding publishers. We demo one room because multiple rooms demo
> identically and we'd rather show depth.

**"How hard is it to install?"**
> Three commands with no hardware, and `smoke_test.py` runs 32 checks so you know
> immediately whether your environment is sound. Every tier has a tested fallback.

---

## Shooting discipline

- **Protect shots 1, 2 and 4.** Physical action and the refusal are the archetype and the
  differentiator. If you run out of time, cut shot 3, then the close, never these.
- **Label the simulation on screen**, not just in narration. Occupancy and lux are
  simulated; the code already says so in `lux_src` / `occ_src`. Use it.
- **Never imply a physical action that didn't happen.** If a Kasa device misses, reshoot —
  you have that luxury now. Do not cut around a failure to make it look successful.
- **Lead with the dollar figure**, not the architecture. It is what gets remembered.
- **For Team's Choice**, the two things engineers remember are the bulb switching from a
  button press and the amber `unverified` badge. Make sure both are unmissable.
- **Shoot it early.** The video is the submission; a missing `demo.mp4` costs more than any
  feature you could add in the same hour.
