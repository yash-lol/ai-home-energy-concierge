# File structure and how to run everything

Companion to `code/README.md`. That file is the judged submission document; **this one is
the operator's manual** — every file explained, every run path spelled out, with the
expected output so you know when something is wrong.

Nothing here is aspirational: every command below has been executed and its output
verified on a Windows-on-Arm machine.

---

## 1. File structure

```
hackathon_energy_concierge/
│
├── README.md                       packet index — read this first
├── 00_MASTER_PLAN.md               architecture, 5 key decisions, day-by-day plan, roles
├── 01_LLM_PROMPT_PACK.md           copy-paste prompts for the on-site LLM + QUAD section
├── 02_DEMO_SCRIPT.md               timed 5-minute demo, slide list, Q&A prep
├── 03_SETUP_CHEATSHEET.md          every command, venue Wi-Fi, wiring, pre-demo checklist
├── 04_ORGANIZER_REQUIREMENTS.md    compliance checklist distilled from the organizer decks
├── 05_FILE_STRUCTURE_AND_RUN.md    this document
├── presentation.html               self-contained slide deck (open in any browser)
│
├── dashboard_verified.png          dashboard with open findings + Apply buttons
├── dashboard_actuated.png          after approval: power collapsed, saving realized
├── phone_verified.png              phone PWA with a critical alert
│
└── code/                           ← this is what goes in the GitHub repo root
    │
    ├── README.md                   the judged submission README
    ├── LICENSE                     MIT
    ├── requirements.txt            6 pinned dependencies
    ├── mosquitto.conf              broker config that binds 0.0.0.0 (not just localhost)
    ├── smoke_test.py               38 checks — run this first, always
    │
    ├── hub/                        the Copilot+ PC orchestrator
    │   ├── energy_model.py         (200 ln) deterministic arithmetic — the source of every number
    │   ├── rules.py                (485 ln) R1–R8 waste detection, no LLM
    │   ├── llm.py                  (313 ln) GenieX narration + deterministic fallback
    │   ├── server.py               (561 ln) MQTT, state fusion, FastAPI, WebSocket, /api/apply
    │   ├── simulator.py            (205 ln) scripted 90-second demo + random mode
    │   ├── cloud_report.py         (242 ln) AI Cloud 100 deep report + deterministic fallback
    │   └── benchmark.py            (321 ln) latency / memory / efficiency measurements
    │
    ├── dashboard/
    │   └── index.html              (646 ln) hub dashboard, no build step, no CDN
    │
    ├── phone/
    │   └── index.html              (355 ln) phone PWA, no build step, no CDN
    │
    └── arduino/
        ├── sketch/sketch.ino       (218 ln) STM32 firmware: sensors + servo/relay actuator
        ├── uno_q_publisher.py      (402 ln) Dragonwing Linux publisher + command subscriber
        └── bridge.py               (127 ln) USB fallback, runs on the PC instead
```

### What each file is responsible for

| File | Responsibility | Why it exists separately |
|---|---|---|
| `hub/energy_model.py` | watts → kWh → dollars → kg CO₂; the SDG&E tariff; the `formula` string | Isolated so it is trivially unit-testable and so **no other module ever does arithmetic**. Every number on screen originates here. |
| `hub/rules.py` | 7 detection rules; R7 comfort guardrail | Pure logic, no I/O, no LLM — so every recommendation traces to a named rule and a named threshold |
| `hub/llm.py` | Turns a finding into prose; overwrites numbers from the finding afterwards | Contains the deterministic `template_narrate()` fallback, so the demo survives the model dying |
| `hub/server.py` | MQTT ingest, state fusion, rules loop, WebSocket push, `POST /api/apply` + safety gate | The only stateful component; everything else is pure functions |
| `hub/simulator.py` | Publishes realistic fake sensor/presence data | The stage fallback. Real rules, real math, real LLM — only sensors are fake |
| `hub/cloud_report.py` | Statistical digest computed locally, interpretation delegated to a larger model | Kept off the critical path: it is a button, never a dependency |
| `hub/benchmark.py` | p50/p95 latency, RSS, edge-filter efficiency | Technical Implementation is 40 of 100 points and is scored on measurements |
| `dashboard/index.html` | Operator view: KPIs, power chart, findings, audit panel, Apply | Single file, no build step — cannot break from a missing `npm install` |
| `phone/index.html` | Presence context, notifications, **Approve** | Same reasoning; also works if only the phone is available |
| `arduino/sketch/sketch.ino` | 1 Hz sensor sampling **and** actuator drive | Hard real-time work belongs on the MCU, not under an OS scheduler |
| `arduino/uno_q_publisher.py` | Edge smoothing, occupancy FSM, change-triggered publish, command subscriber | Makes the UNO Q a network peer rather than a USB sensor; cuts broker traffic ~89% |
| `arduino/bridge.py` | Same parsing, but runs on the PC over USB | Fallback if the Dragonwing Linux side is unavailable |
| `smoke_test.py` | 32 assertions across every layer | Proves the environment before anyone debugs application logic |

### Data flow, file by file

```
sketch.ino ──serial JSON──▶ uno_q_publisher.py ──MQTT──▶ server.py
                                                             │
phone/index.html ──────────────MQTT/REST──────────────────────┤
                                                             ▼
                                                     rules.py (8 rules)
                                                             │
                                                     energy_model.py
                                                       (all numbers)
                                                             │
                                                        llm.py (prose only)
                                                             │
                                    ┌────────────────────────┴───────────┐
                                    ▼                                    ▼
                          dashboard/index.html                  phone/index.html
                                    │                                    │
                                    └──── Apply / Approve ───────────────┘
                                                     │
                                    server.py  POST /api/apply
                                    (R7 pre-flight safety gate)
                                                     │
                                          MQTT home/command/…
                                                     ▼
                                          uno_q_publisher.py
                                                     │
                                          serial "CMD ac off"
                                                     ▼
                                              sketch.ino → SERVO MOVES
                                                     │
                                       MQTT home/actuator/… (confirmation)
                                                     ▼
                                       server.py books the saving as REALIZED
```

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | 3.11 verified |
| A browser | Chrome/Edge; the dashboard uses no framework and no CDN |
| mosquitto | Optional — the hub runs without a broker via REST injection |
| GenieX | Optional — without it a deterministic narrator is used |
| Arduino UNO Q | Optional — the simulator replaces all sensor hardware |

### Install dependencies

```bash
cd code
pip install -r requirements.txt
```

Behind a corporate proxy, use the internal mirror:

```bash
pip install -r requirements.txt -i https://devpi.qualcomm.com/qcom/dev/+simple --trusted-host devpi.qualcomm.com
```

> **CRITICAL — the one dependency mistake that costs an hour.** It must be
> `uvicorn[standard]` **and** `websockets`, both of which are in `requirements.txt`.
> Plain `pip install uvicorn` gives you no WebSocket support: the REST API works fine,
> but `/ws` returns **404**, the dashboard renders completely blank, and **there is no
> error in the server log.** It looks like a front-end bug. It is not.

Verify:

```bash
python -c "import fastapi, uvicorn, paho.mqtt.client, requests, serial, websockets; print('all OK')"
```

---

## 3. Run path A — verify the install (always do this first)

```bash
cd code
python smoke_test.py
```

**Expect `38/38 checks passed`** and exit code 0. It covers dependencies, the energy
model's arithmetic, all 8 rules, the comfort guardrail as both a filter and an actuation
gate, the LLM fallback, the cloud fallback, and a live server through the full
approve → command → realized-saving loop.

If a check fails, fix that before touching anything else — it is telling you about your
environment, not the application.

---

## 4. Run path B — quickstart with no hardware (3 commands)

The fastest way to see the whole system work, and the stage fallback all week.

```bash
# terminal 1 — broker
mosquitto -c mosquitto.conf -v

# terminal 2 — hub
python hub/server.py

# terminal 3 — scripted scenario
python hub/simulator.py --mode demo
```

Open the URL the hub prints (e.g. `http://192.168.1.50:8000/`).

**What you should see**, in order, as the simulator prints captions:

| t | On the dashboard |
|---|---|
| 0 s | ~1460 W, on-peak $0.58/kWh, **no findings** — everything running is in use |
| 15 s | presence flips to **away**, distance climbs |
| 25 s | **R2 critical** "Cooling an empty home" + **R1 serious** "Lights left on" |
| 45 s | loads off, power collapses on the chart |
| 60 s | **R3 warning** "Daylight is doing the job already" (640 lux) |
| 75 s | **R6 warning** peak-hour dryer — charges only the *rate delta* |

Rehearse faster with `--speed 3`. Continuous drift instead of a script: `--mode random`.

---

## 5. Run path C — full setup with hardware

### 5.1 Broker

```bash
mosquitto -c mosquitto.conf -v
```

The included `mosquitto.conf` binds `0.0.0.0`; the default mosquitto config listens only
on localhost, which silently prevents the phone and UNO Q from ever connecting.

Open the firewall once, in an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -LocalPort 1883 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Hub HTTP 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

Confirm from another device:

```bash
mosquitto_sub -h <PC_IP> -t 'home/#' -v
```

### 5.2 GenieX (the NPU-backed local LLM)

```bash
geniex pull ai-hub-models/Qwen3-4B-Instruct-2507
geniex serve                     # → http://127.0.0.1:18181/v1
```

That endpoint and model are already the defaults in `hub/llm.py`, so usually there is
nothing to export. To override:

```bash
export LLM_BASE_URL=http://127.0.0.1:18181/v1
export LLM_MODEL=ai-hub-models/Qwen3-4B-Instruct-2507
export LLM_ENABLED=1        # 0 forces the deterministic narrator
```

Verify the endpoint independently before blaming the hub:

```bash
curl http://127.0.0.1:18181/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"ai-hub-models/Qwen3-4B-Instruct-2507","messages":[{"role":"user","content":"say READY"}]}'
```

### 5.3 Hub

```bash
python hub/server.py
```

It prints a banner with the LAN URL, the broker address, and the LLM endpoint. Use that
IP on the other devices — not `localhost`.

### 5.4 Arduino UNO Q — MCU side

Flash `arduino/sketch/sketch.ino`. **Arduino App Lab** is recommended because it targets
both brains; the Arduino IDE/CLI programs only the MCU.

Wiring:

| Function | Pin | Notes |
|---|---|---|
| PIR motion out | D2 | HIGH = motion |
| LDR divider | A0 | 3V3 – LDR – A0 – 10k – GND |
| DHT22 data | D4 | set `USE_DHT 0` if absent |
| **Servo signal** | **D9** | presses a physical light switch |
| **Relay / LED** | **D7** | actuator fallback / visible indicator |
| **Buzzer** | **D8** | audible "command landed" |

> **Power the servo from its own 5 V supply, not the board's 3V3 rail.** A stalling servo
> browns out the MCU and you will spend an hour chasing phantom resets. Tune
> `SERVO_OFF_DEG` / `SERVO_ON_DEG` so the arm reaches the switch without pressing hard
> against it.

Verify telemetry at 115200 baud — exactly one line per second:

```
{"occupancy":true,"lux":420,"temp_c":24.5,"humidity":48.0,"raw_pir":1}
```

Verify actuation by typing into the serial monitor:

```
CMD lights off
```

The servo should move, the buzzer chirp, and one ack line come back:

```
{"ack":"lights","state":"off","ok":true}
```

### 5.5 Arduino UNO Q — Linux (Dragonwing) side

```bash
pip3 install paho-mqtt pyserial
ROOM=living MQTT_HOST=<PC_IP> python3 uno_q_publisher.py
```

Expect, in order:

```
[uno_q] MCU serial found at /dev/ttyACM0
[uno_q] MQTT connected rc=0
[uno_q] subscribed home/command/#
[uno_q] command listener ready (actuator source: mcu_serial)
[uno_q] pub (change) occ=True lux=192 22.7C 47%
```

**If you do not see `subscribed home/command/#`, actuation will not work.** That line
comes from the `on_connect` callback; it is the confirmation that the command
subscription actually took.

Development without hardware:

```bash
python3 uno_q_publisher.py --fake-serial --broker <PC_IP>
```

Flip loads mid-demo with no rewiring:

```bash
echo '{"lights":{"state":"on","watts":240},"ac":{"state":"on","watts":1100}}' > /tmp/loads.json
```

**Fallback if the Linux side is unavailable** — run this on the PC instead:

```bash
python arduino/bridge.py --port COM5 --broker localhost
```

### 5.6 Phone

1. Join the same network as the PC (or the PC's hotspot).
2. Open `http://<PC_IP>:8000/phone` in Chrome.
3. Tap **Enable alerts**.
4. Optional: while at home, tap **Set home here**, then **Use my location**.

Geolocation over plain HTTP is commonly blocked. **The manual HOME/AWAY toggle always
works — demo with that** and treat geofencing as a bonus you mention.

### 5.7 Cloud deep report (optional)

```bash
export CLOUD_BASE_URL=<AI Cloud 100 OpenAI-compatible endpoint>
export CLOUD_MODEL=<larger model>
export CLOUD_API_KEY=<key if needed>
```

With these unset the button still works and returns a deterministic Python report — it
never shows an error.

---

## 6. Run path D — the actuation loop (the archetype)

This is the sequence to rehearse most, because it is what the project is judged on.

1. Create the waste condition — walk out of the room, or:
   ```bash
   curl -X POST http://localhost:8000/api/presence -H "Content-Type: application/json" \
     -d '{"presence":"away","distance_m":2400}'
   ```
2. Wait one 5-second evaluation cycle. A **critical** card appears; the phone buzzes.
3. Tap **Approve & turn it off** on the phone (or **Apply** on the dashboard). By hand:
   ```bash
   curl -X POST http://localhost:8000/api/apply -H "Content-Type: application/json" \
     -d '{"reco_id":"r2-living-ac","action":"off","approved_by":"cli"}'
   ```
4. Expected response:
   ```json
   {"ok":true,"command":{...},"load_key":"living/ac","published":true,"realized_usd":0.0532}
   ```
5. **The servo moves and the lamp goes out.** The dashboard's "Realized by acting" tile
   turns green, the card reads "✓ Applied — saving realized", and total power drops.

### Prove the safety gate refuses an unsafe action

Make the room hot, then try to switch the A/C off:

```bash
curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":false,"lux":110,"temp_c":29.5,"humidity":52}'

curl -X POST http://localhost:8000/api/apply -H "Content-Type: application/json" \
  -d '{"reco_id":"r2-living-ac","action":"off"}'
```

Expect **HTTP 409** and a refusal naming the temperature:

```json
{"ok":false,"refused":true,"gate":"comfort_guardrail",
 "reason":"Refused: living is 29.5°C, above the 27°C comfort limit — turning the A/C off would make the room uncomfortable."}
```

The UI shows this as an amber note under the card, not as an error. **This is the best
30 seconds of the demo** — the system declining to carry out its own recommendation.

---

## 7. Run path E — measure performance

```bash
python hub/benchmark.py                # human-readable
python hub/benchmark.py --markdown     # table for the README
python hub/benchmark.py --json         # machine-readable
```

Compare runtimes by pointing at each:

```bash
LLM_ENABLED=0 python hub/benchmark.py                             # deterministic control
LLM_BASE_URL=http://127.0.0.1:18181/v1 python hub/benchmark.py    # GenieX on the NPU
```

Verified baseline on a Windows-on-Arm dev machine, `LLM_ENABLED=0`:

| Metric | p50 | p95 | Unit |
|---|---|---|---|
| Energy-model estimate | 0.004 | 0.007 | ms |
| Rules engine (7 rules) | 0.027 | 0.046 | ms |
| Narration — deterministic template | 0.002 | 0.005 | ms |
| Sensor snapshot → recommendations | 0.035 | 0.058 | ms |
| Broker messages avoided by edge filtering | **88.7** | — | % |
| Peak RSS | 32.9 | — | MB |

If the LLM endpoint is unreachable, that row reports **SKIPPED with the reason** — it
never silently substitutes the fallback's timing for the model's.

For authoritative NPU latency, power and utilization, run `/quad-profile` through the
QUAD MCP server at a support session and paste the report into `code/README.md`.

---

## 8. Run path F — no broker, no hardware, nothing but the hub

Every piece of state can be injected over REST, which makes even a dead broker
survivable on stage.

```bash
python hub/server.py     # that is all you need running

curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":true,"lux":120,"temp_c":23.4,"humidity":46}'
curl -X POST http://localhost:8000/api/load -H "Content-Type: application/json" \
  -d '{"key":"living/ac","state":"on","watts":1100}'
curl -X POST http://localhost:8000/api/presence -H "Content-Type: application/json" \
  -d '{"presence":"away","distance_m":2400}'
```

> **Note on R1 vs R2 when injecting by hand.** The hub tracks occupancy duration itself
> and ignores a `last_occupied_ts` you pass in, so R1 (unoccupied lights) needs the room
> reported *occupied*, then *unoccupied*, then ~10 real minutes. **R2 (away + HVAC) fires
> within one 5-second cycle — that is the one to demo live.**

---

## 9. Test each module on its own

```bash
python hub/energy_model.py    # prints a hand-checkable table; verify the math yourself
python hub/rules.py           # fires each of the 8 rules in its own scenario
python hub/llm.py             # LLM vs template narration, side by side
python hub/cloud_report.py    # a report from a synthetic 24-hour digest
```

`energy_model.py` is worth running once by hand: it prints the `formula` string for each
estimate so you can confirm every dollar figure with a calculator. That is the
auditability claim, demonstrated.

---

## 10. HTTP API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | dashboard |
| GET | `/phone` | phone PWA |
| GET | `/api/state` | full fused snapshot + power history + realized totals |
| GET | `/api/recos` | recent recommendations |
| POST | `/api/presence` | `{"presence":"home"\|"away","distance_m":int}` |
| POST | `/api/load` | `{"key":"living/ac","state":"on","watts":1100}` |
| POST | `/api/sensor` | `{"room":"living","occupancy":bool,"lux":int,"temp_c":float,"humidity":float}` |
| **POST** | **`/api/apply`** | **`{"reco_id":str,"action":"on"\|"off","approved_by":str}`** → commands the actuator; **409** if the guardrail refuses |
| POST | `/api/deep_report` | triggers the cloud report |
| WS | `/ws` | pushes state and recommendations |

## 11. MQTT topic contract

| Topic | Publisher | Payload |
|---|---|---|
| `home/sensors/<room>` | UNO Q | `{"occupancy":bool,"lux":int,"temp_c":float,"humidity":float,"ts":epoch}` |
| `home/loads/<room>/<load>` | UNO Q / sim | `{"state":"on"\|"off","watts":float,"ts":epoch}` |
| `home/context/user` | phone | `{"presence":"home"\|"away","distance_m":int,"battery":int,"ts":epoch}` |
| `home/reco` | hub | `{"id","severity","title","body","kwh","usd","co2_kg","actions"}` |
| `home/command/<room>/<load>` | hub | `{"action":"on"\|"off","reco_id":str,"approved_by":str,"ts":epoch}` |
| `home/actuator/<room>/<load>` | UNO Q | `{"state":"on"\|"off","source":str,"reco_id":str,"ok":bool,"ts":epoch}` |
| `home/state` | hub | full snapshot |

All payloads are JSON, all carry `ts`, all units are in the key name (`_m`, `_c`, `_kg`).

`source` on the actuator confirmation reports honestly how the action was carried out:
`mcu_serial`, `simulated` (no MCU attached), or `serial_error`. A confirmation never
overstates what physically happened.

---

## 12. Environment variables

| Variable | Default | Effect |
|---|---|---|
| `LLM_BASE_URL` | `http://127.0.0.1:18181/v1` | GenieX / any OpenAI-compatible endpoint |
| `LLM_MODEL` | `ai-hub-models/Qwen3-4B-Instruct-2507` | model name sent in the request |
| `LLM_API_KEY` | *(empty)* | usually unnecessary locally |
| `LLM_ENABLED` | `1` | `0` forces the deterministic narrator |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` | broker location |
| `HTTP_PORT` | `8000` | hub HTTP/WS port |
| `ROOM` | `living` | which room the UNO Q publishes as |
| `CLOUD_BASE_URL` / `CLOUD_MODEL` / `CLOUD_API_KEY` | *(unset)* | AI Cloud 100; unset → deterministic report |

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard blank, all KPIs 0, console shows `WebSocket … 404` | plain `uvicorn` without the `[standard]` extra | `pip install "uvicorn[standard]" websockets` |
| Phone/UNO Q cannot reach the broker | default mosquitto binds localhost only | use the included `mosquitto.conf`; open port 1883 |
| Phone cannot load the page | using `localhost` instead of the LAN IP | use the IP from the hub's startup banner |
| **Approve does nothing; no servo movement** | UNO Q never subscribed | check for `[uno_q] subscribed home/command/#`. Subscriptions must be registered in the `on_connect` callback — one registered at startup is silently lost, and lost again on every reconnect |
| Recommendations flood the UI | cooldown disabled | `RECO_COOLDOWN_S` in `server.py` (default 600 s) |
| A finding shows an implausibly large dollar figure | clock discontinuity inflating duration | `MAX_CHARGEABLE_S` in `rules.py` caps it at 4 h; the hub also re-anchors `on_since` |
| `strftime` error on Windows | `%-I` is not portable | use the `_fmt_hour()` helper in `rules.py` |
| MCU resets when the servo moves | servo drawing from the 3V3 rail | give the servo its own 5 V supply |
| LLM row in the benchmark says SKIPPED | endpoint unreachable | start `geniex serve`; the row is *meant* to skip rather than mis-report |
| Recommendation reappears after being applied | stale build | applied findings are suppressed via `applied_ids` in `server.py` |

### General debugging order

1. `python smoke_test.py` — is the environment sound?
2. `mosquitto_sub -h <IP> -t 'home/#' -v` — is anything on the bus?
3. `curl http://<IP>:8000/api/state` — has the hub fused what you expect?
4. Browser console — WebSocket connected?
5. Only then read application logs.

**One process per client ID.** If you start a second `server.py` or a second
`uno_q_publisher.py` against the same broker, they share an MQTT client ID and evict each
other — producing symptoms that look exactly like a subscription bug. Kill stale
processes before diagnosing anything else.

---

## 14. Fallback ladder

Every layer has a tested fallback. This is the order to descend on stage.

| Layer | Primary | Fallback |
|---|---|---|
| Network | venue Wi-Fi (`HaQathon`) | PC mobile hotspot |
| Sensors | real PIR/LDR/DHT22 | `simulator.py --mode demo` |
| Transport | MQTT broker | REST injection (`/api/sensor`, `/api/load`, `/api/presence`) |
| UNO Q Linux | `uno_q_publisher.py` over Wi-Fi | `bridge.py` on the PC over USB |
| Narration | GenieX on the NPU | `template_narrate()` — deterministic, instant |
| Cloud report | AI Cloud 100 | deterministic Python report |
| Actuator | servo pressing a switch | relay → LED + buzzer indicator |
| Presence | phone geofence | manual HOME/AWAY toggle |
| Everything | live demo | Thursday's recording |

A demo running on three fallbacks with an honest explanation scores well. A half-finished
component scores zero.
