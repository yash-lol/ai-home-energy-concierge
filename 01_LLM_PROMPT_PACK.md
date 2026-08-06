# LLM Prompt Pack — your Claude substitute on site

You will have a Qualcomm-internal LLM instead of Claude. Assume it is **less capable, more literal, and worse at holding context** than what you are used to. These prompts are written to compensate.

> **QUAD is a different tool.** The organizers host a **QUAD MCP server** driven from Claude Code or another MCP client, with dedicated support all week. Use it for **profiling on real silicon**, not for code generation — see the QUAD section at the end. These prompts target the general-purpose LLM.

## The five rules of driving a weaker model

1. **One file per request.** Never "build the app." Ask for exactly one file with a stated purpose. Weaker models degrade sharply with scope.
2. **Paste the contract every time.** It will not remember the MQTT topics or the JSON schema from three turns ago. Re-paste the relevant block in every prompt. Repetition is cheap; a schema mismatch costs an hour.
3. **State the acceptance test up front.** "It is done when `python x.py` prints Y." This gives it a target and gives you a verdict.
4. **Demand complete, runnable files.** Forbid `...`, `# TODO`, and "rest of implementation here." Say so explicitly; weaker models elide constantly.
5. **You are the integrator.** It writes files; *you* decide the architecture and wire them together. Never let it invent structure — you already have the structure in `00_MASTER_PLAN.md`.

> If an answer comes back wrong, do not argue with it in follow-ups. Re-prompt from scratch with the failure mode named: *"Previous attempt used X, which fails because Y. Write it again using Z."* Weaker models spiral when you debate; they respond well to a clean restart.

**Most of the code in `code/` is already written and tested (38/38 smoke checks).** Use these prompts to extend it, not to regenerate it. Prompts 1–10 document how the existing files were specified; Prompts 12–15 cover the remaining work.

---

## PROMPT 0 — Session primer

Paste once at the start of every new session, before anything else.

```
You are helping build a hackathon project. Follow these rules exactly.

PROJECT: "AI Home Energy Concierge" — a multi-device system that detects wasted
home energy, explains it in natural language, and — with the user's approval —
PHYSICALLY switches the load off.

ARCHETYPE: IoT Sensor -> Actuator Physical AI. Closing the loop from sensing to
physical action is the defining requirement, not an optional extra.

DEVICES:
- Copilot+ PC, Snapdragon X Elite (Oryon CPU, Adreno GPU, Hexagon NPU 45 TOPS,
  32 GB), Windows on Arm. Central hub and orchestrator. Runs: MQTT broker,
  FastAPI server, rules engine, local LLM via Qualcomm GenieX.
- Arduino UNO Q. Dual brain: STM32U585 Cortex-M33 running Arduino sketches over
  Zephyr (sensors + servo/relay actuator) AND a Qualcomm Dragonwing QRB2210
  quad-core Cortex-A53 running Debian Linux (Python, MQTT, edge filtering).
  4 GB RAM. MPU<->MCU also has a built-in RPC library ("Arduino Bridge").
  Qwiic connector for Modulino nodes.
- Samsung Galaxy S25, Snapdragon 8 Elite, 12 GB. Progressive web app for
  presence context, notifications, and APPROVING actions.
- Qualcomm AI Cloud 100. Off-critical-path deep analysis.

LOCAL LLM: Qualcomm GenieX, OpenAI-compatible server on
http://127.0.0.1:18181/v1 (`geniex serve`). Runtimes: `qairt` (AI Hub bundle,
NPU only, fastest) or `llama_cpp` (GGUF, Q4_0 best for Hexagon, NPU/GPU/CPU).

LANGUAGE: Python 3.11 for hub and embedded Linux. Vanilla HTML/CSS/JS for
dashboard and PWA — no build step, no npm, no frameworks, no CDN.

MQTT TOPIC CONTRACT (never deviate, never rename, never add fields):
  home/sensors/<room>          {"occupancy":bool,"lux":int,"temp_c":float,"humidity":float,"ts":epoch}
  home/loads/<room>/<load>     {"state":"on"|"off","watts":float,"ts":epoch}
  home/context/user            {"presence":"home"|"away","distance_m":int,"battery":int,"ts":epoch}
  home/reco                    {"id":str,"severity":str,"title":str,"body":str,"kwh":float,"usd":float,"co2_kg":float,"actions":[str]}
  home/command/<room>/<load>   {"action":"on"|"off","reco_id":str,"approved_by":str,"ts":epoch}
  home/actuator/<room>/<load>  {"state":"on"|"off","source":str,"reco_id":str,"ok":bool,"ts":epoch}
  home/state                   full snapshot object

ARCHITECTURAL RULES — THE MOST IMPORTANT ONES:
1. Python computes ALL numbers deterministically. The LLM only explains numbers
   it is given. The LLM never performs arithmetic and never invents a figure.
2. The comfort guardrail (rule R7) GATES PHYSICAL ACTION. An action that would
   make the home uncomfortable is refused, even if the user asks for it.
3. Every tier has a tested fallback. Nothing may hard-fail.

OUTPUT RULES:
- Give me ONE complete runnable file per response.
- Never use "...", "# TODO", or "rest of code here". Every function fully written.
- Standard library first. Only these third-party packages are allowed:
  paho-mqtt, fastapi, uvicorn, pyserial, requests, pydantic, psutil.
- Include a `if __name__ == "__main__":` block that self-tests when relevant.
- Start with a one-paragraph plan, then the file, then the exact command to run it.

Acknowledge with "READY" and nothing else.
```

---

## PROMPT 1 — Energy model (build this FIRST)

This file is the credibility of the whole project. Get it right before anything else.

```
Write `hub/energy_model.py`.

PURPOSE: All deterministic energy arithmetic. No LLM, no I/O, no MQTT. Pure
functions only, fully unit-testable. Every other module imports from here.

REQUIREMENTS:

1. A LOADS dict of typical household load power draws in watts, each entry
   carrying: watts, a human label, and a `source` string naming where the figure
   came from (e.g. "US DOE typical", "measured nameplate"). We must be able to
   defend every number to a judge.
   Include at minimum: led_bulb_set, incandescent_set, ceiling_fan,
   window_ac, central_ac, space_heater, tv_65, desktop_pc, game_console,
   clothes_dryer, dishwasher, fridge, standby_phantom.

2. A TARIFF structure for San Diego SDG&E time-of-use:
   - off_peak_usd_per_kwh = 0.32
   - on_peak_usd_per_kwh  = 0.58
   - on-peak window 16:00-21:00 local, every day
   - function `rate_at(dt)` returning (rate, "on_peak"|"off_peak")

3. CO2: 0.25 kg CO2 per kWh (California grid average). Named constant with a
   comment citing it.

4. Functions, each with type hints and a docstring stating the formula:
   - `kwh(watts, seconds)` -> float
   - `cost(kwh, rate)` -> float
   - `co2_kg(kwh)` -> float
   - `waste_estimate(load_key, seconds_wasted, at_time)` -> a dataclass
     `WasteEstimate` with fields: kwh, usd, co2_kg, rate_used, period_label,
     watts, load_label, source, and a `formula` string that literally spells out
     the calculation performed, e.g.
     "60 W x 3600 s = 0.060 kWh; 0.060 kWh x $0.58/kWh (on_peak) = $0.035"
   - `annualize(usd_per_event, events_per_week)` -> float

5. The `formula` string is a hard requirement. The dashboard displays it so a
   judge can audit any number on screen.

6. Under `if __name__ == "__main__":` print a small table of five example waste
   estimates, one on-peak and one off-peak, so I can verify the math by hand.

It is done when `python hub/energy_model.py` prints a readable table and every
dollar figure can be recomputed by hand from the printed formula string.
```

---

## PROMPT 2 — Rules engine

```
Write `hub/rules.py`.

PURPOSE: Deterministic waste detection. Given a fused state snapshot, return
findings. NO LLM here — this is pure logic so it is testable and explainable.

INPUT — a dict shaped like:
{
  "rooms": {
    "living": {"occupancy": false, "lux": 420, "temp_c": 24.5, "humidity": 48,
               "last_occupied_ts": 1754200000, "ts": 1754200600}
  },
  "loads": {
    "living/lights": {"state":"on","watts":60,"ts":...},
    "living/ac":     {"state":"on","watts":1100,"ts":...}
  },
  "user": {"presence":"away","distance_m":2400,"battery":72,"ts":...},
  "now": 1754200600
}

OUTPUT — a list of Finding dataclasses:
  id, rule_name, severity ("critical"|"serious"|"warning"|"good"),
  room, load_key, seconds_wasted, headline (short, factual, no LLM flourish),
  evidence (list of plain-language strings, each a fact that triggered the rule),
  suggested_actions (list of short imperative strings)

RULES TO IMPLEMENT (each its own function, each independently testable):
 R1 unoccupied_lights_on   — room unoccupied > 10 min AND lights on
 R2 away_with_hvac_on      — user away AND AC/heater on
 R3 daylight_waste         — lux > 300 (bright daylight) AND lights on
 R4 hvac_with_window_open  — AC on AND humidity/temp pattern suggesting an open
                             window (temp not falling despite AC runtime). Keep
                             the heuristic simple, and make it clear in the
                             evidence that it is a heuristic.
 R5 phantom_standby        — devices drawing standby power with user away > 2 h
 R6 peak_hour_heavy_load   — high-watt deferrable load (dryer/dishwasher)
                             running during the 16:00-21:00 on-peak window
 R7 comfort_guardrail      — SUPPRESSES a cooling recommendation when the room
                             is already above 27 C or would become uncomfortable.
                             This rule REMOVES findings rather than adding them.

REQUIREMENTS:
- Every threshold is a module-level named constant with a comment. No magic numbers.
- Each finding calls energy_model.waste_estimate() and attaches the result.
  Import from hub.energy_model — do not recompute anything locally.
- Findings sort by usd descending so the most valuable is always first.
- R7 runs last, as a filter over the assembled list.
- Under __main__, run six hand-built scenarios (one per detection rule) and
  print which findings fire. This is my test suite.

It is done when `python hub/rules.py` shows each rule firing in its scenario and
nothing firing in an "all good" scenario.
```

---

## PROMPT 3 — LLM narration layer with a hard fallback

The fallback is the point. Read the last requirement carefully.

```
Write `hub/llm.py`.

PURPOSE: Turn a Finding (already carrying computed numbers) into friendly
natural language. The LLM NEVER computes or alters a number.

REQUIREMENTS:

1. Class `LLMClient` talking to an OpenAI-compatible chat completions endpoint.
   Config from env vars: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (key optional).
   Use `requests`. Timeout 8 seconds, hard.

2. Method `narrate(finding) -> Recommendation`.
   The prompt sent to the model must:
   - state the facts and the already-computed kwh/usd/co2 values
   - instruct: "Do not perform arithmetic. Use the numbers exactly as given."
   - require a JSON object back: {"title": str (max 60 chars),
     "body": str (2 sentences max, friendly, second person),
     "actions": [str] (max 3, imperative, each under 8 words)}

3. Validate the response: parse JSON, check required keys and length limits.
   Then OVERWRITE kwh/usd/co2_kg on the Recommendation from the Finding, never
   from the model output. If the model mentions a number that contradicts the
   computed one, the computed one wins.

4. CRITICAL — `template_narrate(finding) -> Recommendation`: a pure-Python
   deterministic fallback using f-strings, producing decent copy with NO LLM at
   all. `narrate()` must call this automatically on timeout, connection error,
   malformed JSON, or validation failure. Log which path was used.

   The demo must work with the LLM process killed. Test that explicitly.

5. `LLM_ENABLED` env flag to force template mode for rehearsals.

6. Under __main__, narrate three sample findings twice: once with the LLM, once
   with LLM_ENABLED=0, and print both so I can compare quality.

It is done when the file produces sensible recommendations with the LLM endpoint
completely unreachable.
```

---

## PROMPT 4 — Hub server

```
Write `hub/server.py`.

PURPOSE: The orchestrator. Subscribes to MQTT, fuses state, runs rules, calls
narration, serves the dashboard over WebSocket, exposes a small REST API.

REQUIREMENTS:
- paho-mqtt client, subscribes: home/sensors/#, home/loads/#, home/context/#
- Maintains an in-memory StateStore: latest reading per room/load/user, plus
  `last_occupied_ts` per room and a rolling 5-minute power history for sparklines.
- Evaluation loop every 5 seconds: build snapshot -> rules.evaluate() ->
  for each NEW finding (dedupe by rule+room+load, 10-minute cooldown so we do
  not spam) -> llm.narrate() -> publish to home/reco -> push over WebSocket.
- FastAPI endpoints:
    GET  /               serve dashboard/index.html
    GET  /phone          serve phone/index.html
    GET  /api/state      current snapshot as JSON
    GET  /api/recos      recent recommendations
    POST /api/presence   {"presence":"home"|"away"} — manual override for demo
    POST /api/deep_report trigger the cloud report
    WS   /ws             push state + recos
- Static files served from ../dashboard and ../phone.
- Bind 0.0.0.0 so the phone can reach it. Print the LAN URL and a QR-able
  address on startup.
- Graceful degradation: if the broker is down, keep serving the dashboard and
  log a warning. Never crash on a bad payload — wrap every handler in try/except
  and log-and-continue.

The dedupe cooldown matters: without it the demo floods with duplicates.

It is done when `python hub/server.py` starts, prints the LAN URL, and serves
the dashboard with live simulator data.
```

---

## PROMPT 5 — Simulator (your demo insurance)

```
Write `hub/simulator.py`.

PURPOSE: Publish realistic fake sensor/load/presence data to MQTT so the whole
pipeline can be developed and demoed without hardware. This is our stage
fallback, so it must be genuinely convincing.

REQUIREMENTS:
- CLI: --mode {random,demo} --broker host --port 1883 --speed N
- `random` mode: plausible drifting values, occasional occupancy changes.
- `demo` mode: a SCRIPTED 90-second narrative, printing a caption line as each
  beat begins so I can narrate on stage:
    t=0    user home, living room occupied, lights on, AC on, evening 18:30
           (on-peak) — baseline, no findings
    t=15   user leaves: presence -> away, distance climbs 5 -> 800 m
    t=25   occupancy goes false, lights STILL on, AC STILL on
           -> expect R1 + R2 to fire, on-peak rate, biggest dollar number
    t=45   simulated user acts on advice: lights off, AC off -> "good" finding,
           savings banked
    t=60   next morning 10:00, bright daylight lux 600, lights back on
           -> expect R3
    t=75   dryer starts at 17:00 on-peak -> expect R6 peak-shift advice
    t=90   loop or exit
- `--speed 2` halves all delays for rehearsal.
- Timestamps must be internally consistent (a simulated clock, not wall time),
  so on-peak/off-peak logic is exercised correctly.
- Print a clear banner: "SIMULATED SENSOR FEED" so we never accidentally imply
  fake data is real on stage.

It is done when `python hub/simulator.py --mode demo` drives the dashboard
through all five beats with the right recommendations appearing.
```

---

## PROMPT 6 — Arduino UNO Q, MCU side

```
Write `arduino/sketch/sketch.ino` for the STM32U585 MCU on an Arduino UNO Q.

PURPOSE: Sample sensors on a hard schedule and emit compact JSON lines.

HARDWARE:
- PIR motion sensor on D2 (digital)
- LDR / photoresistor divider on A0 (analog, 0-1023)
- DHT22 temp+humidity on D4  (if the DHT library is unavailable, fall back to
  the board's onboard sensor or a documented stub — say which you used)

REQUIREMENTS:
- Sample every 1000 ms, non-blocking, millis()-based. No delay() in the loop.
- Occupancy debounce: PIR must read HIGH once within a 30 s window to count as
  occupied; report `occupancy` as the debounced value, not the raw pin.
- Convert the LDR reading to approximate lux with a documented formula and a
  comment that it is an approximation, not a calibrated measurement.
- Emit exactly one JSON line per sample on Serial at 115200:
  {"occupancy":true,"lux":420,"temp_c":24.5,"humidity":48.0,"raw_pir":1}
- No pretty printing, no extra logging on Serial — the line must be parseable.
- Comment the pin map at the top of the file.

It is done when the serial monitor shows one clean parseable JSON line per second
and occupancy flips correctly when I wave at the sensor.
```

---

## PROMPT 7 — UNO Q Linux-side publisher (the differentiator)

```
Write `arduino/uno_q_publisher.py` to run on the Debian Linux side (Qualcomm
Dragonwing QRB2210) of an Arduino UNO Q.

PURPOSE: Read the MCU's JSON lines, aggregate locally, publish to MQTT over
Wi-Fi. This makes the UNO Q an independent network node rather than a USB
peripheral — an architectural point we make on stage.

REQUIREMENTS:
- pyserial read from the MCU. The port differs by image: try in order
  /dev/ttyACM0, /dev/ttyACM1, /dev/ttyUSB0, and any /dev/serial/by-id/* match.
  Log which one succeeded.
- Robust line parsing: ignore partial or malformed lines, never crash. Count and
  log the drop rate.
- EDGE INTELLIGENCE (this is the point — do it locally, not on the hub):
  - median-of-5 smoothing on lux and temp
  - occupancy state machine with a 30 s "grace" hold to avoid flapping
  - only publish when a value changes materially (lux +/-15%, temp +/-0.3 C,
    occupancy transition) OR every 10 s as a heartbeat. State why in a comment:
    it cuts broker traffic and shows real edge processing.
- Publish sensors to home/sensors/<ROOM> where ROOM comes from env var ROOM,
  default "living".
- Also publish simulated load state to home/loads/<ROOM>/lights and
  .../ac, toggled by a local file `/tmp/loads.json` so we can flip loads during
  the demo without rewiring anything.
- Auto-reconnect to MQTT with exponential backoff. Broker host from env
  MQTT_HOST.
- systemd-friendly: log to stdout, handle SIGTERM cleanly.
- Include a `--fake-serial` flag that generates data without any MCU attached,
  so this can be developed before the hardware works.

It is done when the UNO Q publishes to the PC broker over Wi-Fi with no USB
data connection, and `mosquitto_sub -t 'home/#' -v` on the PC shows the traffic.
```

Also ask for the USB fallback separately:

```
Write `arduino/bridge.py` — a minimal fallback that runs ON THE PC, reads the
UNO Q over USB serial, and republishes to MQTT. Same parsing and smoothing as
uno_q_publisher.py. Use this if the UNO Q Linux side is unavailable. Keep it
under 120 lines.
```

---

## PROMPT 8 — Phone PWA

```
Write `phone/index.html` — a single self-contained file. Inline CSS and JS, no
frameworks, no build step, no CDN dependencies (venue Wi-Fi may block them).

PURPOSE: Runs in Chrome on a Galaxy S25. Provides user-presence context and
displays recommendations.

FEATURES:
1. Big HOME / AWAY toggle. POSTs to /api/presence. Manual override is the
   demo-safe path and must always work.
2. Optional geolocation: watchPosition, compute distance from a "home" location
   captured by a "Set home here" button, auto-set presence when past 100 m.
   Must degrade silently to manual if permission is denied — geolocation over
   plain HTTP is often blocked, so NEVER make the demo depend on it.
3. WebSocket to /ws, renders incoming recommendations as cards, newest first:
   severity color strip, title, body, the dollar figure prominent, action chips.
4. Notification API for a buzz when a critical reco arrives, with a visible
   in-page banner fallback if permission is denied.
5. Mobile-first dark UI, large touch targets, readable at arm's length from a
   judge's viewing distance. Assume it will be shown on a projector or held up.
6. A connection status dot: green connected, red disconnected, with the hub URL
   shown so we can debug on stage.

Colors: dark surface #1a1a19, primary text #ffffff, secondary #c3c2b7,
critical #d03b3b, warning #fab219, good #0ca30c, accent blue #3987e5.

It is done when the phone loads it over the LAN, the toggle changes hub state,
and recommendation cards appear live.
```

---

## PROMPT 9 — Dashboard

The dashboard in `code/dashboard/index.html` is already written and validated. Use it as-is. Only if you need to extend it:

```
I have a working dashboard at dashboard/index.html. Add ONE feature without
restructuring anything else, and return the complete modified file:

<describe the single feature>

Design constraints that must not change:
- dark surface #1a1a19, page #0d0d0d, primary ink #ffffff, secondary #c3c2b7,
  muted #898781, gridline #2c2c2a, baseline #383835
- series colors in fixed order: #3987e5, #d95926, #199e70 — never add a fourth
  series color, never cycle them
- status: good #0ca30c, warning #fab219, serious #ec835a, critical #d03b3b,
  each always paired with an icon and a text label, never color alone
- thin marks, 2px lines, recessive grid, no dual axes ever
- every number on screen must be traceable to a formula string from energy_model
```

---

## PROMPT 10 — Cloud deep report

```
Write `hub/cloud_report.py`.

PURPOSE: Off-critical-path "deep analysis" using a larger model on Qualcomm AI
Cloud 100. Triggered by a button, never required for the live demo.

REQUIREMENTS:
- Aggregate the last N hours of state into a compact statistical digest computed
  IN PYTHON: total kwh, cost split on-peak vs off-peak, top waste events, hours
  of unoccupied-lights-on, occupancy pattern by hour of day. Keep the digest
  under 2000 characters — do not dump raw logs into the prompt.
- Send the digest to a larger model via an OpenAI-compatible endpoint. Env vars:
  CLOUD_BASE_URL, CLOUD_MODEL, CLOUD_API_KEY.
- Ask for JSON: {"summary": str, "patterns": [str],
  "weekly_plan": [{"action": str, "est_monthly_usd": float, "effort": "low"|"med"|"high"}],
  "top_retrofit": {"action": str, "cost_usd": float, "payback_months": float}}
- Validate the JSON. On any failure, return a deterministic Python-generated
  report from the same digest. The button must never show an error.
- Cache the last successful report to disk so we can show it even fully offline.
- Timeout 30 s, run in a background thread, never block the event loop.
- Under __main__, generate a report from a synthetic 24-hour digest.

It is done when the button returns a report with the cloud endpoint unset,
using the deterministic path.
```

---

## PROMPT 11 — README (do this Thursday, it is judged)

```
Write the README.md for our hackathon repo. It is directly judged on
"presentation and documentation," so it must be genuinely good.

STRUCTURE:
1. Title + one-sentence pitch
2. The problem, in three sentences, with a concrete example
3. Architecture — an ASCII diagram of the four tiers (UNO Q edge, X Elite hub,
   phone, AI Cloud 100) and a table of which intelligence runs where and WHY
4. What makes it different: three-tier AI placement; auditable arithmetic
   (LLM narrates, Python computes); cross-device context fusion that no single
   device could do alone
5. Hardware required, with a note that the simulator replaces all hardware
6. Setup — numbered, copy-pasteable, assuming a stranger on a clean machine:
   broker install, python deps, LLM runtime, env vars, UNO Q flash, phone URL.
   Include a "quickstart with no hardware" path that is 3 commands.
7. Usage — how to run the demo scenario, what to expect at each beat
8. The MQTT contract table
9. Repo layout tree
10. Limitations and honest next steps
11. Team and credits

TONE: plain, technical, confident. No marketing language, no emoji.
Assume the reader is an engineer who will actually try to run it.

I will paste our actual file list and any deviations for you to incorporate.
```

---

## Debugging prompts

When something breaks and you cannot ask me:

```
This code fails with the error below. Give me: (1) the single most likely cause
in one sentence, (2) the exact minimal fix as a diff, (3) one command that
verifies the fix. Do not rewrite the whole file. Do not suggest more than one fix.

CODE:
<paste only the relevant function, not the whole file>

ERROR:
<paste the full traceback>

WHAT I ALREADY TRIED:
<list, so it does not repeat you>
```

For hardware, which is where you will lose the most time:

```
Arduino UNO Q. Debugging <sensor/serial/wifi>. Symptom: <exact observed
behavior>. Expected: <what should happen>.

Give me an ordered checklist of diagnostic commands or measurements, cheapest
and most likely first. For each, tell me what result would confirm or eliminate
that cause. Stop at 6 steps.
```

---

---

## PROMPT 12 — Actuator firmware (already written; use to extend)

```
Extend `arduino/sketch/sketch.ino` for the Arduino UNO Q (STM32U585).

It already samples PIR (D2), LDR (A0) and DHT22 (D4) and emits one JSON line per
second at 115200 baud. Keep that behaviour byte-for-byte identical.

ADD a serial command reader that drives an actuator:
  host -> MCU:  CMD <load> <on|off>      e.g.  CMD lights off
  MCU -> host:  {"ack":"lights","state":"off","ok":true}

HARDWARE:
  D9  micro-servo signal  — presses a physical rocker light switch
  D7  relay / LED         — reflects requested state; fallback if no servo
  D8  buzzer              — short chirp confirming the command landed

REQUIREMENTS:
- Non-blocking reader: poll Serial every loop pass so actuation feels instant.
  Never use delay() in the telemetry path.
- Servo: move to the press angle, hold ~500 ms, RETURN TO REST. Never hold
  against the switch — a stalled servo browns out the rail.
- Buzz first, before the mechanical action, so we get confirmation even if the
  servo fails.
- Bound the command buffer; drop overlong lines rather than overflowing.
- Comment the pin map and warn that the servo needs its own 5 V supply.

It is done when typing `CMD lights off` in a serial monitor moves the servo and
prints exactly one ack line, and the 1 Hz telemetry stream is unaffected.
```

## PROMPT 13 — Command subscriber on the Linux side (already written)

```
Extend `arduino/uno_q_publisher.py` (runs on the UNO Q's Debian/Dragonwing side).

It already reads MCU telemetry, does median smoothing + occupancy FSM +
change-triggered publishing, and publishes home/sensors/<room>.

ADD an Actuator class that closes the loop:
- Subscribe to `home/command/#`. Ignore commands for other rooms.
- On a valid command, write `CMD <load> <action>\n` to the SAME serial handle
  the telemetry is read from (open it once in main and share it).
- Publish confirmation to `home/actuator/<ROOM>/<load>`:
  {"state":..., "source":..., "reco_id":..., "ok":bool, "ts":epoch}
- `source` must report honestly how it happened: "mcu_serial", "simulated"
  (no MCU attached), or "serial_error". NEVER claim a physical action that did
  not occur.
- Update /tmp/loads.json and republish the load state so hub power figures follow
  reality.
- The MCU echoes acks on the same link. They are valid JSON but NOT telemetry —
  detect the "ack" key and skip them rather than counting them as malformed.

CRITICAL BUG TO AVOID: register subscriptions inside the on_connect callback, not
once at startup. connect_async() has not completed when __init__ returns, so an
early subscribe() is silently lost — and it is also lost on every reconnect. Keep
a list of topics and re-subscribe in on_connect.

It is done when `mosquitto_pub -t home/command/living/ac -m '{"action":"off"}'`
makes the servo move and a confirmation appears on home/actuator/living/ac.
```

## PROMPT 14 — The apply endpoint with a safety gate (already written)

```
Add `POST /api/apply` to `hub/server.py`.

PURPOSE: the user approves a recommendation; we command the physical actuator.

BODY: {"reco_id": str, "action": "on"|"off", "approved_by": str}

SEQUENCE:
1. Look up the recommendation by id. 404 if unknown.
2. Map the rule name to a load ("away_with_hvac_on" -> ac,
   "unoccupied_lights_on" -> lights, "peak_hour_heavy_load" -> dryer, etc).
3. PRE-FLIGHT SAFETY GATE — enforce the comfort guardrail as a real gate, not
   advice. Refuse with HTTP 409 and a human-readable `reason` when:
     - turning the A/C off while the room is above rules.COMFORT_MAX_C (27 C)
     - turning the heater off while the room is below rules.COMFORT_MIN_C (16 C)
   Turning something ON is always allowed.
4. Publish `home/command/<room>/<load>`.
5. Update local load state so the UI responds even with no board attached.
6. Book the saving as REALIZED (a separate total from "avoidable"), dedup by
   reco id, and stop re-narrating that finding.
7. Broadcast new state over the WebSocket.

Return {"ok":true,"command":...,"load_key":...,"published":bool,"realized_usd":float}.

It is done when applying at 23 C succeeds and returns realized_usd, and applying
an A/C-off at 29.5 C returns HTTP 409 with the reason naming the temperature.
```

## PROMPT 15 — Benchmark harness (already written; use to extend)

```
Extend `hub/benchmark.py`.

PURPOSE: Technical Implementation is 40 of 100 points and is scored on "resource
utilization, optimization, latency and performance, and energy efficiency" —
i.e. on MEASUREMENTS. It already reports: energy-model latency, rules-engine
latency, narration latency (LLM vs deterministic control), end-to-end
sensor->recommendation, peak RSS, and the % of broker messages avoided by edge
filtering.

ADD <the metric you need>, following the existing conventions:
- report p50 and p95, never a single mean
- if a measurement cannot be taken, mark the row SKIPPED with the reason.
  NEVER silently substitute the fallback path's timing for the LLM's.
- keep --markdown and --json output modes working
- seed any randomness so the published figure is reproducible

It is done when `python hub/benchmark.py --markdown` emits a table that pastes
straight into README.md.
```

---

## QUAD — use it for measurement, not codegen

QUAD is a hosted MCP server on real Qualcomm silicon, driven from Claude Code or another MCP client. **Go to the dedicated QUAD support sessions / office hours (Tue & Thu 1:30–3:30, Fri 9–12).**

```powershell
.\install.ps1
quad-client install --transport sse-http `
  --sse-url https://quad.infra.foundries.io/mcp `
  --sse-auth-token-env QUAD_MCP_TOKEN
quad-client connect-test sse-http --sse-url https://quad.infra.foundries.io/mcp --auth-token "$env:QUAD_MCP_TOKEN"
quad-client detect
quad-client doctor
```

**The organizers' prescribed prompt for our project, verbatim from the project sheet:**

```
Build the "AI Home Energy Concierge" project with QUAD.
Archetype: IoT Sensor -> Actuator Physical AI (Close the loop from sensing to
physical action — servos, lenses, robotic arms, haptics — driven by the Arduino
UNO Q + X-Elite brain.)
Target devices: AI PC, Mobile, Arduino UNO Q
Use case: Fuses occupancy/light/temp sensing + user context; a local LLM
explains and recommends energy savings.

Drive the QUAD MCP tools in order: hardware_detect, convert_model,
generate_code. Follow the archetype workflow:
hardware_detect(platform=linux/robotics) -> the UNO Q / RB-class target ->
convert_model -> SNPE .dlc for the QCS2210 DSP (<3 W) -> quad robotics
build/deploy -> cross-compile + push to the board -> generate_code -> sensor-read
+ inference + actuator-drive sketch. When a step requires calibration data or a
real device, stop and ask me. If a step is blocked by the primary gap (G6), emit
the mock output and flag the blocker.
```

**What to actually run, and why:**

| Skill / tool | Verdict on their sheet | What we do |
|---|---|---|
| `/quad-detect` · `hardware_detect` | Automated | Run it — free, confirms the target |
| `convert_model` | Human (needs calibration data) | Only if converting a model; GenieX AI Hub bundles are pre-compiled |
| **`/quad-profile` · `profile_workload`** | Human | **Run it — the 40-point evidence: NPU latency, power, utilization** |
| **`/quad-orchestrate`** | Human | **Run it — CPU vs GPU vs NPU comparison** |
| `/quad-codegen` · `generate_code` | **BLOCKED by G6** | **Skip it** — see below |

**On gap G6.** Their sheet marks `generate_code` as blocked: *"Arduino UNO Q sensor/actuator + GPIO codegen (App Lab / Modulino / MPU-MCU bridge) — not started (Phase L)."* The suggested workaround is QUAD's mock path.

**We do not need it.** The sketch, the MPU-side publisher, and the actuator path are hand-written and tested. Say so on stage: *we routed around a known platform gap rather than waiting on it or shipping mock output.* That reads as engineering judgment. Do **not** present mock output as if it ran.

Paste the `/quad-profile` report into `README.md` beside the `benchmark.py` table.

---

## Day-1 discovery task — the App Lab Python API

The exact **App Lab / Arduino Bridge Python API** (module names, RPC call signatures, what "Bricks" are, the project layout) **could not be verified off-site.** Find it on the real board:

- `pip list` / `pip show` on the Dragonwing Linux side
- read the **App Lab example projects** — they show the sketch-side registration and the Python-side call as a matched pair
- `github.com/qualcomm/edge-ai-labs-arduino/tree/main/rpc` — the official RPC example

**Nobody on the team should invent this API.** If it resists, plain serial — which our tested code already uses — is the fallback and costs nothing but a bullet point.

Prompt to use once you have the real docs in front of you:

```
Here is the actual App Lab / Arduino Bridge documentation from the board:

<paste the real text, or the example project source>

Rewrite `arduino/uno_q_publisher.py` to use this RPC mechanism instead of raw
pyserial, keeping ALL existing behaviour identical: median smoothing, occupancy
FSM, change-triggered publishing with a 10 s heartbeat, the home/command/#
subscriber, and honest `source` reporting in the confirmation.

Do not invent any API name. If the pasted documentation does not cover something
you need, stop and tell me exactly what is missing.
```

---

## Time-boxing rule

**If any single component eats more than 90 minutes, switch to its fallback and move on.** Every component in this plan has one:

| Component | Fallback |
|---|---|
| UNO Q Linux MQTT | `arduino/bridge.py` over USB |
| Real sensors | `simulator.py` |
| Phone geolocation | manual HOME/AWAY toggle |
| Local LLM (GenieX) | `template_narrate()` |
| AI Cloud 100 | deterministic report path |
| Servo actuator | relay, then LED + buzzer indicator |
| QUAD profiling | `hub/benchmark.py` numbers alone |
| Live demo | Thursday's video (**capture the actuation shot**) |

A demo with three fallbacks engaged and a clear story still scores well. A half-finished component scores zero and costs you the README.
