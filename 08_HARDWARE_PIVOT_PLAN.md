# Hardware pivot plan — Modulino + Kasa + phone simulator

Supersedes the breadboard hardware assumptions in `06_UNO_Q_BRINGUP.md` Steps 2-5
and 9. Written 2026-08-05. Companion to `07_QUAD_SESSION_LOG.md` (which records
everything already built and verified).

**Appendix A at the bottom is a standalone brief** for designing a phone/web
simulation UI — it contains the full API + MQTT contract and every rule threshold,
so it can be handed to someone (or another session) with no other context.

---

## Context

The project was designed around breadboard hardware we don't have: PIR (D2),
photoresistor (A0), DHT22 (D4), and a micro-servo (D9) pressing a light switch.

Everything **above the serial line already works and is verified** — hub, MQTT
broker, rules engine, GenieX NPU narration, 38/38 smoke tests, and the
board→broker→hub loop proven with `--fake-serial`.

What we actually have:
- **Modulino Knob** (connected now via Qwiic), **Modulino Buttons** (tomorrow)
- **Kasa/TP-Link smart bulbs AND plugs** — locally controllable over LAN
- SwitchBot Curtain (optional stretch)
- A phone, and the project's own PWA already served at `/phone`

Goal: a **breadboard-free** build that still satisfies Archetype E ("close the loop
from sensing to physical action"), holding the honesty standard the project sets
for itself.

The Kasa devices make this *better* than the original plan — a real lamp visibly
going dark beats a servo nudging a switch, and it removes the riskiest technical
dependency (Risk V-2 below).

## Two findings that shape the design

1. **Qwiic is on the MCU, as `Wire1`** (I2C4, STM32U585 pins PD12/PD13) — confirmed
   by Arduino's UNO Q pin table *and* by `Arduino_Modulino`'s source, which
   special-cases `ARDUINO_UNO_Q` to default to `Wire1`. A sketch can read Modulinos.
   Still verify with the scanner (Phase 0) — it also reports actual addresses.
2. **`Serial` on UNO Q routes through the Bridge/RPC layer to the App Lab console.**
   It is *unconfirmed* that it enumerates as `/dev/ttyACM*` on Linux, or that
   `Serial.read()` receives bytes written *from* Linux. Pre-existing exposure (the
   current sketch has it too), but it would kill the `CMD` actuation path.
   **Moving actuation to Kasa makes the MCU print-only, so this stops being
   load-bearing.** Board platform must be **>= 0.55.0** for `Serial` at all.

## Architecture

```
Modulino Knob ──I2C/Wire1──> STM32 MCU ──Serial(print-only)──> uno_q_publisher.py
                                                                      │ MQTT
Phone PWA (sliders) ──HTTP POST /api/sensor──────────────> hub/server.py
                                                                      │
                                          rules R1-R8 + GenieX narration
                                                                      │ approve
                                                     home/command/<room>/<load>
                                                                      │
                                          uno_q_publisher.py ──python-kasa──>
                                                            REAL BULB / PLUG
```

**Signal ownership** — each source declared on the wire, never faked silently:

| Signal | Source | Honesty label |
|---|---|---|
| `temp_c` | **Modulino Knob** — rotation maps 16-32 °C | `temp_src: "knob_sim"` |
| `lux` | Phone PWA slider | declared simulation |
| `humidity` | Phone PWA slider | declared simulation |
| `occupancy` | Phone PWA toggle (+ Buttons later) | declared |
| `presence` | Phone PWA (existing geofence/manual) | already honest |
| **`lights` load** | **Kasa bulb** | real device state read back |
| **`ac` load** | **Kasa plug** | real device state read back |

**Why the Knob drives temperature, not lux:** `temp_c` is the input to **R7**, the
comfort guardrail that *refuses* to switch off the A/C above 27 °C — the most
persuasive demo moment. A physical dial turned on stage until the system audibly
refuses beats a slider. Knob press snaps between 22 °C and 29.5 °C (straddling
R7's threshold) for a one-click demo.

## Phase 0 — Quick win, today (~15 min, Knob only)

1. In **App Lab**, confirm UNO Q board platform **>= 0.55.0** (needed for `Serial`;
   also defines `ARDUINO_UNO_Q`, which makes Modulino pick `Wire1`).
2. Flash a new **I2C scanner sketch** (`code/arduino/scanner/scanner.ino`) scanning
   **both** `Wire` and `Wire1`, printing found addresses and whether
   `ARDUINO_UNO_Q` is defined. A banner is fine — it's a diagnostic, not telemetry.
   - **Gate:** at least one address on `Wire1`. Knob is `0x3A`/`0x3B` 7-bit (the
     library stores 8-bit `0x74`/`0x76` and halves them).
   - Both buses empty → cabling/power. Stop and fix before writing app code.
3. Flash a minimal Knob sketch: `knob.get()` → print. Turn knob, watch it move.
   **That's the positive result.**

## Phase 1 — MCU sketch: Modulino telemetry (print-only)

Rewrite `code/arduino/sketch/sketch.ino`. **Wire contract unchanged** —
`uno_q_publisher.py` and everything above it keep working untouched:
115200 baud, one compact JSON object per `\n` line, ~1 Hz, **no boot banner**,
keys `occupancy` (bool), `lux` (int), `temp_c` (float 1dp), `humidity` (float 1dp).

Changes:
- `Modulino.begin(Wire1)` explicitly (correct even if `ARDUINO_UNO_Q` is missing).
- **Knob → `temp_c`**, 16-32 °C over ~100 detents, encoder clamped and written back
  so the dial stays in sync. Knob press snaps 22 °C ↔ 29.5 °C.
- Every node **independently optional**, auto-detected via its `begin()` return.
  A missing node degrades one signal, never blocks boot.
- **Provenance keys added** (`temp_src`, `lux_src`, `occ_src`, `env_ok`, `nodes`)
  so a stub is never indistinguishable from a measurement. Fixes a real
  pre-existing bug: `USE_DHT 0` emitted 23.5 °C/45 % that looked exactly like real
  readings, silently disabling **R4 and the R7 safety gate**.
- **Drop `raw_pir`** (verified: no consumer reads it).
- **`POLL_MS = 20`** instead of "sample every loop pass" — right for a free
  `digitalRead`, wrong for a 100 kHz I2C transaction (saturates the bus, starves
  `Serial`).
- **No command handling needed** — actuation moves to Kasa. Keep the `CMD` reader
  behind `#define MCU_ACCEPTS_COMMANDS 0` so it can be re-enabled if V-2 passes.

Two hard constraints from the research — do not violate:
- **Never emit `null`** for the four contract keys. `float(raw.get("temp_c", 0.0))`
  on `None` raises `TypeError` *outside* the publisher's try/except — it kills the
  process. Fallbacks stay numeric, with a separate boolean saying whether to trust.
- `ModulinoThermo::getTemperature()` returns **`0`, not NaN**, when uninitialised —
  gate validity on `begin()`, not `isnan()`. (Relevant if a Thermo is added later.)

## Phase 2 — Kasa actuator (the real physical action)

Add `python-kasa`; replace the serial write in `uno_q_publisher.py`'s
`Actuator.execute()` (~lines 213-248).

- Discover devices once at startup; map `lights` → bulb, `ac` → plug. Config via
  env (`KASA_LIGHTS_HOST`, `KASA_AC_HOST`), discovery as fallback.
- On `home/command/<room>/<load>`: call the device, **read state back**, publish
  `home/actuator/...` with the real result.
- **Fixes an existing honesty gap:** today `ok=True` / `source="mcu_serial"` only
  means "bytes were flushed" — the MCU ack is parsed then discarded. Kasa returns
  genuine device state. New `source` values: `kasa` / `kasa_error` / `simulated`.
- Keep the `simulated` fallback intact so the demo degrades honestly.

**Where it runs:** the UNO Q's Linux side, so the UNO Q is genuinely the actuator
driver (best Archetype E story). Needs the board on Wi-Fi to reach Kasa. Fallback:
run the Kasa call in `hub/server.py` — still honest, slightly weaker narrative.

## Phase 3 — Phone PWA as the sensor simulator

Extend `code/phone/index.html` with a "Sensor Simulator" panel: sliders for `lux`
and `humidity`, toggle for `occupancy`, POSTing to the hub's **existing**
`POST /api/sensor` (already implemented — no server change needed).

**Deliberately sliders, not real phone sensors.** `getUserMedia`,
`AmbientLightSensor`, and iOS `DeviceMotion.requestPermission()` all require a
**secure context** — blocked over plain `http://<PC_IP>:8000`. The README already
notes geolocation hits this. Sliders work everywhere, zero permissions, and are
exactly the "simulation from a phone app" that was asked for. Real phone sensors
remain an optional later step behind a self-signed HTTPS cert on uvicorn.

**See Appendix A** for the full contract needed to design this UI.

## Phase 4 — Modulino Buttons (when they arrive)

Three buttons → presence HOME/AWAY, `lights` toggle, `ac` toggle. Debounce with
**two-poll confirmation**: `ModulinoButtons::update()` copies its read buffer into
`last_status[]` *even when the I2C read failed*, so a flaky node emits phantom
presses.

## Transport

Keep **both** — different purposes, not mutually exclusive:
- **ADB over USB-C** (`winget install Google.PlatformTools`) — control/debug shell
  immune to the Wi-Fi drift that broke this setup three times. `adb reverse
  tcp:1883 tcp:1883` tunnels MQTT over USB if Wi-Fi dies.
- **Wi-Fi** — required for the UNO Q to reach Kasa devices on the LAN.

## Files to change

| File | Change |
|---|---|
| `code/arduino/scanner/scanner.ino` | **new** — I2C scanner, Phase 0 |
| `code/arduino/sketch/sketch.ino` | rewrite for Modulino Knob, print-only, provenance keys |
| `code/arduino/uno_q_publisher.py` | `Actuator.execute()` → python-kasa; real state read-back |
| `code/phone/index.html` | add Sensor Simulator panel → `POST /api/sensor` |
| `code/requirements.txt` | add `python-kasa` |
| `code/README.md` | rewrite Hardware + Setup; update the "uncalibrated photoresistor" honesty text |
| `06_UNO_Q_BRINGUP.md` | replace breadboard Steps 2-5, 9 with Qwiic + Kasa flow |
| `07_QUAD_SESSION_LOG.md` | record the new hardware reality and decisions |

Unchanged and reused as-is: `hub/server.py` (its `/api/sensor`, `/api/apply`, R7
pre-flight gate and MQTT ingest already do what we need), `hub/rules.py`,
`hub/llm.py`, `hub/energy_model.py`, `smoke_test.py`.

## Verification

1. **Phase 0:** scanner shows a Knob address on `Wire1`; `ARDUINO_UNO_Q` defined.
2. **Phase 1:** `python3 uno_q_publisher.py` (no `--fake-serial`) logs
   `MCU serial found at /dev/ttyACM*` — **this settles Risk V-1**. Clean 1 Hz JSON;
   knob moves `temp_c`; hub `/api/state` shows `rooms.living` updating. If no
   `/dev/tty*` appears, fall back to `--fake-serial` + phone sliders, noted honestly.
3. **Phase 2:** `curl -X POST /api/apply -d '{"reco_id":"...","action":"off"}'` →
   **the real bulb physically goes dark**; `home/actuator/...` reports
   `source:"kasa"`, `ok:true` from a real read-back; dashboard shows "Applied —
   saving realized".
4. **R7 safety gate (the money demo):** turn the knob past 27 °C → tap Approve on
   the A/C → **HTTP 409, nothing switches**, amber refusal note on the card.
5. `python smoke_test.py` still **38/38** (uses no hardware).
6. Re-run `hub/benchmark.py` with GenieX for the README table.
7. **Record video of #3 and #4 the moment they work** — Archetype E insurance.

## Known risks

- **V-2 (now de-risked):** MCU may not receive serial *from* Linux. Kasa actuation
  routes around it. Only matters if `MCU_ACCEPTS_COMMANDS` is re-enabled.
- **V-1:** MCU `Serial` may not appear as `/dev/tty*` at all. Phase 1 settles it;
  fallback is phone-only sensing.
- **Kasa auth:** newer firmware uses KLAP, may need TP-Link cloud credentials passed
  locally. `python-kasa` supports it — discover early, not on demo day.
- **No real temperature sensor.** R7 runs on a declared knob value. Honest and
  labelled, but say so on stage. A **Modulino Thermo** (or **Light** node for real
  lux) is the highest-value hardware addition if either can be obtained.
- Unrelated and still blocked: `/quad-profile` blocked by two server-side infra
  failures on `quad.infra.foundries.io`; GenieX's measured NPU benchmark stands as
  the NPU evidence. See `07_QUAD_SESSION_LOG.md` §10.

---

# Appendix A — Brief for a simulation UI / app

**Standalone brief.** Everything needed to design a phone or web UI that feeds
simulated sensor data into this system. No other context required.

## What the system is

A home energy assistant. Sensors report room conditions → a deterministic rules
engine (R1-R8) detects wasted energy → an on-device LLM narrates the finding in
plain language → the user approves → a real device is physically switched off.

The simulation UI's job: **stand in for the physical sensors** so the rules can be
driven deliberately during a demo, and be *visibly honest* that it is a simulation.

## Hard constraints

- **Plain HTTP, not HTTPS.** The hub serves `http://<PC_IP>:8000`. So the UI
  **cannot** use `getUserMedia` (camera), `AmbientLightSensor`, or iOS
  `DeviceMotion.requestPermission()` — all require a secure context and will be
  blocked. Use manual controls (sliders/toggles/buttons). Geolocation is likewise
  usually blocked and already degrades to a manual HOME/AWAY toggle.
- Must work on a phone browser with **no install and no permissions**.
- No build step. The existing pages (`code/dashboard/index.html`,
  `code/phone/index.html`) are single self-contained HTML files with inline CSS/JS,
  served statically by FastAPI. Match that.
- Should be honest on its face — label it a simulator, don't dress it as sensors.

## HTTP API (FastAPI, base `http://<PC_IP>:8000`)

```http
POST /api/sensor        Content-Type: application/json
{"room":"living","occupancy":false,"lux":110,"temp_c":23.6,"humidity":47}

POST /api/load
{"key":"living/ac","state":"on","watts":1100}          # state: "on"|"off"

POST /api/presence
{"presence":"away","distance_m":2400}                  # presence: "home"|"away"

POST /api/apply
{"reco_id":"r2-living-ac","action":"off","approved_by":"phone"}
   -> 200 on success; 409 when the R7 comfort guardrail REFUSES (body has reason)

GET  /api/state         full snapshot (rooms, loads, user, recos, realized, tariff)
WS   /ws                live push of the same snapshot
```

Static pages: `GET /` (dashboard), `GET /phone` (phone PWA).

## MQTT topic contract (broker on port 1883, if publishing directly instead)

| Topic | Publisher | Payload |
|---|---|---|
| `home/sensors/<room>` | sensor source | `{"occupancy":bool,"lux":int,"temp_c":float,"humidity":float,"ts":epoch}` |
| `home/loads/<room>/<load>` | sensor source | `{"state":"on"\|"off","watts":float,"ts":epoch}` |
| `home/context/user` | phone | `{"presence":"home"\|"away","distance_m":int,"battery":int,"ts":epoch}` |
| `home/reco` | hub | `{"id","severity","title","body","kwh","usd","co2_kg","actions"}` |
| `home/command/<room>/<load>` | hub | `{"action":"on"\|"off","reco_id","approved_by","ts"}` |
| `home/actuator/<room>/<load>` | device | `{"state","source","reco_id","ok","ts"}` |
| `home/state` | hub | full snapshot |

HTTP is simpler for a browser UI and is the recommended path — the hub fuses both
into the same state.

## Signals to simulate, with the thresholds that matter

Rooms are named (`living` is the default). Loads are keyed `<room>/<load>`;
the code already anticipates **`lights`** and **`ac`**.

| Signal | Range | Threshold that matters | Which rule |
|---|---|---|---|
| `occupancy` | bool | empty for **> 10 min** + lights on → fires | **R1** |
| `lux` | 0-2000 | **> 300** = "daylight", + lights on → fires | **R3** |
| `temp_c` | 16-32 °C | **> 27.0** suppresses advice AND **refuses actuation**; **< 16.0** likewise | **R7** |
| `humidity` | 0-100 % | **>= 60** + A/C running 15 min + no temp drop → fires | **R4** |
| `presence` | home/away | away + HVAC on → fires; away **> 2 h** + standby draw → fires | **R2**, **R5** |
| clock | — | **16:00-21:00** peak window, heavy deferrable load → fires | **R6** |

**Useful presets to build in** (these make a demo one tap instead of a hunt):

| Preset | Sets | Expected result |
|---|---|---|
| "Empty room, lights on" | occupancy=false (hold 10 min), lights on | R1 serious |
| "Away with A/C on" | presence=away, ac on 1100 W | **R2 critical** — the flagship |
| "Bright daylight" | lux=640, lights on | R3 warning |
| "Too hot to cool" | temp_c=29.5 | **R7 refuses** — the money demo |
| "Comfortable" | temp_c=22.0 | R7 allows |
| "Peak hour heavy load" | dryer 3000 W at 17:00 | R6, rate-delta only |

## Design notes

- **Two-panel split works well:** "Simulated sensors" (the controls) and "What the
  system decided" (live recommendations from `/ws` or polling `/api/state`).
- The **R7 refusal** is the most important interaction to make visible — when
  `POST /api/apply` returns **409**, show the refusal reason prominently. That is
  the system declining its own advice for safety, and it is the strongest moment
  in the demo.
- Show the **audit formula** — every recommendation carries a `formula` string
  (e.g. `1100 W x 7200 s = 2.2000 kWh; ... x $0.58/kWh (on_peak) = $1.276`).
  Surfacing it supports the project's "the LLM narrates, Python computes" claim.
- Existing pages use a dark theme; match `code/dashboard/index.html` for
  consistency if the new UI is to sit alongside it.
- Label simulated values plainly. The project's stated standard is: never present a
  simulated reading as a measurement.
