# AI Home Energy Concierge

A multi-device AI system that notices wasted household energy, explains in plain
language what it is costing you, and - with your approval - **physically switches it
off**.

Built for the Snapdragon Multiverse hackathon, August 2026.
**Archetype E - IoT Sensor to Actuator Physical AI.**

---

## The problem

Homes waste energy because devices operate independently and have no awareness of
context. Your air conditioner does not know you left the house. Your lights do not
know the sun is out. And in San Diego, SDG&E charges roughly 81% more per kilowatt-hour
between 4 PM and 9 PM than it does overnight - so *when* a load runs matters as much
as *whether* it runs.

No single device can see enough to fix this. A motion sensor sees an occupied room and
concludes everything is fine, even at noon with the blinds open and every light on.

## Closing the loop: human-approved actuation

The system does not stop at advice, and it does not act blindly either:

```
sense -> reason -> recommend -> USER APPROVES -> physically act -> confirm -> book the saving
```

Tap **Approve** on the phone or dashboard and the hub publishes a command; the UNO Q
drives a servo that presses a real light switch (or a relay), then publishes a
confirmation. The hub books the saving as **realized** rather than merely avoidable -
the UI distinguishes *"you could save $X"* from *"you saved $X."*

**The comfort guardrail gates the actuator.** Rule R7 is not advisory: it is enforced
as a pre-flight check in `POST /api/apply`, so the system will **refuse** to switch off
the A/C when the room is above 27 C, even if a user asks it to. That is the difference
between automation and judgment.

## Architecture

```
+------------------------------------+
|  Arduino UNO Q                     |
|  +--------------+ +--------------+ |
|  | STM32U585    | | Dragonwing   | |  MQTT over Wi-Fi
|  | Cortex-M33   | | QRB2210      | |----------------+
|  | (Zephyr)     |>| Debian/Python| |                |
|  | PIR/LDR/temp |<| MQTT client  | |<---- commands  |
|  | SERVO/RELAY  | | edge filter  | |                |
|  +--------------+ +--------------+ |                v
+------------------------------------+   +----------------------------+
                  ^                      | Copilot+ PC (Snapdragon    |
                  | physical action      | X Elite, 45 TOPS NPU)      |
                  |                      |        ORCHESTRATOR        |
+------------------------------+         |                            |
|  Galaxy S25 (Snapdragon      |         |  mosquitto broker          |
|  8 Elite)                    |-------->|  FastAPI + WebSocket       |
|  PWA: presence, geofence,    |         |  rules engine (determin.)  |
|  notifications, APPROVE      |         |  energy model (determin.)  |
+------------------------------+         |  GenieX LLM -- narration   |
                                         |  R7 guardrail -- gates     |
       +-----------------------+         |     physical action        |
       | Qualcomm AI Cloud 100 |<--------|  deep report (off-path)    |
       | weekly deep report    |         +-------------+--------------+
       +-----------------------+                       | WebSocket
                                                       v
                                             Dark dashboard (browser)
```

**Where each piece of intelligence lives, and why:**

| Tier | Device | Intelligence | Why here |
|---|---|---|---|
| Edge | UNO Q - STM32U585 (Zephyr) | 1 Hz sensor sampling; **actuator drive** | hard real time, no OS jitter |
| Edge | UNO Q - Dragonwing QRB2210 (Linux) | median smoothing, occupancy FSM, change-triggered publish, command subscriber | cuts broker traffic ~89%; raw sensor data stays local |
| Hub | Snapdragon X Elite (45 TOPS NPU) | rules engine, energy model, GenieX LLM, safety gate | sub-2 s narration; occupancy data never leaves the house |
| Cloud | AI Cloud 100 | larger model, weekly pattern analysis | seconds of latency are fine off the critical path |

Data flow: sensors + phone presence -> hub fuses a situation snapshot -> deterministic
rules produce findings -> the energy model attaches auditable savings -> the LLM turns
findings into natural language -> user approves -> **the UNO Q physically actuates** ->
the saving is booked as realized.

## What makes this different

**1. Where the AI runs is a design decision, not an accident.** Rules at the edge in
microseconds. A small model on-device via GenieX for private, low-latency narration. A
large model in the cloud for depth. Same problem, three tiers, each chosen against a
latency and privacy budget.

**2. Auditable AI - the model narrates, Python computes.** The LLM never performs
arithmetic and never invents a figure. It receives pre-computed values and may only
phrase them; afterwards every numeric field is overwritten from the deterministic
source. Each recommendation carries a `formula` string, shown in the UI, so any number
on screen can be recomputed by hand:

```
1100 W x 7200 s = 2.2000 kWh; 2.2000 kWh x $0.58/kWh (on_peak) = $1.276;
2.2000 kWh x 0.25 kg/kWh = 0.5500 kg CO2
```

**3. The recommendations require cross-device context fusion.** "Your A/C is cooling an
empty house" needs phone presence AND room occupancy AND load state AND the tariff
window, together. No single device in this system could produce it.

**4. The loop closes, and safely.** Physical actuation, human-approved, with the comfort
guardrail able to veto the machine's own advice.

**5. Every tier degrades gracefully.** Kill the LLM and a deterministic narrator takes
over. Kill the cloud and the report still generates. Kill the sensors and the simulator
drives the same real pipeline. Kill the broker and the hub accepts state over REST. Kill
the servo and the relay/buzzer still confirms the command landed.

## Measured performance

Technical Implementation is judged on *resource utilization, optimization, latency and
performance, and energy efficiency* - so we measure rather than claim. Reproduce with
`python hub/benchmark.py --markdown`.

Measured on a Windows-on-Arm dev machine with `LLM_ENABLED=0` (deterministic path):

| Metric | p50 | p95 | Unit | Notes |
|---|---|---|---|---|
| Energy-model estimate | 0.004 | 0.007 | ms | pure arithmetic + formula string |
| Rules engine (7 rules) | 0.027 | 0.046 | ms | 1 room / 3 loads -> 2 findings |
| Narration - deterministic template | 0.002 | 0.005 | ms | control path; no model, no network |
| Narration - local LLM (GenieX) | 3110.2 | 3272.7 | ms | run with `geniex serve` active, model `qualcomm/Qwen3-4B-Instruct-2507` (W4A16) |
| Sensor snapshot -> recommendations | 0.017 | 0.019 | ms | rules + energy model + narration |
| **Broker messages avoided by edge filtering** | **88.7** | - | **%** | 68 published of 600 sampled (10 min @ 1 Hz) |
| Peak RSS | 34.0 | - | MB | hub reasoning stack loaded |

**The reasoning tier costs microseconds.** All the latency that matters is LLM
inference, which is why it is the only part we accelerate on the NPU - and why the
deterministic fallback is instant when the model is unavailable.

**Edge filtering removes ~89% of broker traffic**: the UNO Q samples at 1 Hz but only
publishes on a material change (lux +/-15%, temp +/-0.3 C, occupancy transition) or a
10 s heartbeat.

**NPU row above is real, measured evidence**: `geniex serve` running the `qairt` (W4A16, NPU-only) runtime
on this machine's actual Hexagon NPU — this is the app's real production narration path, not a synthetic
benchmark.

> **`/quad-profile` / `/quad-orchestrate` on a separate demo model — currently blocked, documented honestly.**
> We attempted a supplementary CPU/GPU/NPU allocation comparison via QUAD's `convert_model` +
> `orchestrate_workload` on a small sample ONNX model (unrelated to this app's own AI path). Two independent
> server-side infrastructure failures on the hosted QUAD MCP server blocked it:
> 1. `qairt-converter` (QNN/SNPE target) fails to even import — `ImportError: libpython3.10.so.1.0: cannot
>    open shared object file` — a broken Python env on the server, not something fixable client-side.
> 2. The ExecuTorch target fails with `No ExecuTorch checkout or installed package found` — the toolchain
>    isn't fetched on the server (`bash scripts/sdk_fetch.sh executorch` was never run there).
>
> Also worth noting: the hosted MCP server itself (`quad.infra.foundries.io`) is a virtualized x86_64 Ubuntu
> host (`AMD EPYC 7B12`, `available_runtimes: ["cpu"]` only) — even if conversion had succeeded, profiling
> directly against it would **not** produce real NPU numbers. Real on-device profiling needs the client-side
> `quad-client profile-device --transport ssh --host <board-ip>` path (server plans, your machine executes on
> hardware it can reach) — see `/quad-detect`'s findings on this repo's UNO Q board for why that path isn't
> populated with NPU numbers yet either (no SNPE/fastrpc installed on-device).
>
> Flag this at a QUAD office-hours session (Tue/Thu 1:30-3:30, Fri 9-noon) if you want the `/quad-profile`
> evidence filled in before submission — the GenieX row above already satisfies "real NPU numbers" for the
> app's actual AI path in the meantime.

## Rules implemented

| Rule | Detects |
|---|---|
| R1 `unoccupied_lights_on` | room empty > 10 min, lights on |
| R2 `away_with_hvac_on` | user away, A/C or heater running |
| R3 `daylight_waste` | > 300 lux ambient, lights still on |
| R4 `hvac_with_window_open` | A/C running 15+ min, no temperature drop, high humidity (heuristic) |
| R5 `phantom_standby` | standby draw during an absence > 2 h |
| R6 `peak_hour_heavy_load` | deferrable heavy load inside the 4–9 PM peak window; charges only the rate delta |
| R7 `comfort_guardrail` | **suppresses** advice - and **refuses actuation** - above 27 °C / below 16 °C |

R7 is why this is an assistant rather than a thermostat: it removes recommendations that
would make the home uncomfortable, and it blocks the actuator from carrying them out.

### The decisions it declined

A guardrail that silently removes advice is hard to trust, and it throws away the most
interesting thing the system does. `rules.evaluate_all()` returns vetoed findings
**tagged rather than dropped**, fully computed and fully costed, and the dashboard shows
them greyed out beside the live ones:

```
Considered 2 · recommending 1 · declined 1 on safety grounds

⃠ DECLINED   comfort_guardrail
  A/C cooling an empty home  ($0.029 withheld)
  living is 29.5 C, above the 27 C comfort limit — cutting cooling here would
  make the room uncomfortable, so this saving is not offered.
  ↳ comfort protected here — the saving is taken on lights instead
```

That last line is the system resolving a **conflict between two loads**: both are
wasting money, and it declines the one that would cost you comfort while still taking
the one that would not. `evaluate()` is unchanged and still returns only actionable
advice, so nothing downstream can accidentally offer a suppressed finding, and
`/api/apply` enforces the same limit a second time as a pre-flight gate.

Vetoes are recorded as their own row type, **never as a `refusal`**. A refusal is a
human asking and being told no — a real preference signal. A veto is advice nobody was
ever shown, and scoring it as a negative would teach a model that people dislike
recommendations they never saw.

## The learned tier — and what it is not allowed to do

Adding a model must not weaken the safety story, so each kind of intelligence has a
**bounded mandate** and none may override the one above it:

| Tier | Decides | May not |
|---|---|---|
| Deterministic rules + R7 | what is **legal / safe** | be overridden by anything |
| Learned model | what is **likely** | change a number, or unlock a refused action |
| LLM (on the NPU) | **wording** | compute, or invent a figure |

### Occupancy inference

The system has no occupancy sensor — occupancy is a simulator toggle, a Modulino button
override, or a ToF node if one is attached. `hub/occupancy_model.py` turns it into an
inference: given temperature, humidity and ambient light, is the room occupied?

Trained by `tools/train_occupancy.py` on the [UCI Occupancy Detection
benchmark](https://archive.ics.uci.edu/dataset/357/occupancy+detection) (Candanedo &
Feldheim, 2016) — 8,143 training rows and two official held-out splits:

| Variant | Features | acc (datatest) | acc (datatest2) |
|---|---|---|---|
| `full` | temp + humidity + **lux** | **0.9786** | **0.9884** |
| `no_light` | temp + humidity | 0.8608 | 0.8452 |

Both ship in the artifact; `OCCUPANCY_VARIANT` picks one. **`no_light` exists for a
reason:** lux dominates the `full` model (standardised weights: lux +3.59, humidity
+0.76, temp −0.25), and R3 already thresholds on lux — so inferred occupancy and R3
would not be independent evidence. The weights are printed at training time rather than
buried, because that is a fair question to be asked.

Plain-Python logistic regression, trained by full-batch gradient descent. **No numpy, no
sklearn, no ML runtime** — inference is a dot product over three floats, so it adds no
dependency to a Windows-on-ARM machine and runs identically on the hub or the UNO Q's
A53. `python hub/occupancy_model.py` replays the held-out split through the inference
path and asserts it reproduces the trainer's accuracy.

**It runs in shadow mode by default** (`OCCUPANCY_MODEL=1`): it predicts, the dashboard
shows its call beside the reported value, and no rule reads it — so a wrong prediction
cannot change a recommendation or an actuation. `OCCUPANCY_MODEL=2` additionally lets it
*fill a gap* when no other source supplied occupancy; it never overwrites a reported one.

**Honest limit, stated up front:** that benchmark is an office in Belgium in February,
19–23.2 °C at 16.8–39.1 % RH. This demo drives 16–32 °C and 0–100 % RH, so many demo
states are outside anything the model has seen. `predict()` returns `in_domain: False`
there and the dashboard says *extrapolating* — the held-out accuracy above does **not**
transfer to those conditions, and should not be quoted as if it does.

### The session dataset

`hub/recorder.py` appends one JSONL row per evaluation tick, plus a row for every
finding, approval, guardrail refusal, hardware confirmation and dashboard thumb. Two
jobs: an audit trail of decisions (the equivalent of the `formula` string, for choices
rather than arithmetic), and the substrate for training.

Labels come from the human: an **approval** is a positive, a **guardrail refusal** is a
hard negative, a **thumb** is an explicit judgement, and a card shown and never touched
is a *weak* negative carried at weight 0.25 and marked as such — because "ignored" might
only mean nobody was looking.

```bash
python tools/build_dataset.py        # sessions -> data/reco_dataset.csv
```

It ends by printing a verdict on whether there is enough signal to train on at all, and
refuses to look impressive when there is not — below 30 positives it says so outright.
Rows carry the `*_src` provenance stamps and `metered` flags, and the summary reports
what share of the corpus was measured versus simulated versus synthetic, so a model
trained on it can never be described as having learned from a real household when it did
not.

Recording is on by default and writes to `code/data/sessions/`, which is **gitignored** —
the project claims occupancy data never leaves the house, and that has to be true of the
repo too. `RECORD_ENABLED=0` turns it off.

## Hardware

Breadboard-free. Everything connects over Qwiic or Wi-Fi.

| Piece | What it is | Role |
|---|---|---|
| Copilot+ PC, Snapdragon X Elite | 45 TOPS Hexagon NPU | hub: rules, energy model, GenieX LLM narration, safety gate |
| Arduino UNO Q | Dragonwing QRB2210 + STM32U585 | edge node: reads sensors, drives the actuator |
| **Modulino Knob** | Qwiic, I2C `0x3A` on `Wire1` | **declared simulated thermostat dial** — turn past 27 °C to make R7 refuse |
| **TP-Link Kasa KL120 bulb** | LAN, port 9999 | the `lights` load — **and it meters its own watts** |
| **TP-Link Kasa HS110 plug** | LAN, port 9999 | the `ac` load — **also metered** |
| Any phone or laptop browser | — | the sensor simulator at `/simulator`, and the approve UI at `/phone` |

Optional, auto-detected if present: Modulino **Thermo** (real temp + humidity, takes over
from the Knob), **Light** (real lux), **Distance** (presence), **Buttons**, **Buzzer**,
**Pixels**. The firmware probes for each at boot; a missing node degrades exactly one
signal and never blocks startup.

**Nothing here is soldered, and no breadboard is required.** The Modulino attaches by a
single Qwiic cable; the Kasa devices are ordinary smart plugs/bulbs on the LAN.

### Load power is measured, not modelled

The KL120 and HS110 report **real instantaneous watts**, so `home/loads/...` carries
`"metered": true` and the savings arithmetic runs on measured power rather than a
published typical value. Loads with no metered device fall back to the table in
`hub/energy_model.py` and are marked `"metered": false`, so the two are never confused.

### Where each signal comes from

Every value is stamped with its source, so a simulated reading can never be mistaken for
a measurement:

| Signal | Source | Stamp |
|---|---|---|
| `temp_c` | Modulino Knob (or Thermo if attached) | `temp_src: knob_sim` / `hs3003` |
| `lux`, `humidity`, `occupancy` | phone simulator (or Light/Distance nodes) | `lux_src`, `hum_src`, `occ_src` |
| `presence` | phone geofence or manual toggle | — |
| load watts | Kasa device meter | `metered: true` |

Because the hub merges room state, the **last writer of a key wins** — so
`MCU_SIGNALS` in `arduino/board.env` declares explicitly which signals the board owns
(default `temp_c`) and leaves the rest to the simulator. Set it empty to run entirely
hands-free from the simulator.

## Setup

### Quickstart, no hardware — 2 commands

```bash
pip install --only-binary=:all: -r requirements.txt   # --only-binary matters on Win-ARM
python hub/server.py
```

Then open two pages:

| Page | What it is |
|---|---|
| `http://localhost:8000/` | the dashboard — findings, savings, Apply buttons |
| `http://localhost:8000/simulator` | **the sensor simulator** — sliders for lux/temp/humidity, occupancy and presence toggles, load switches, and one-tap demo presets |

The simulator drives the same `/api/sensor`, `/api/load` and `/api/presence` endpoints a
real sensor would, so the rules engine cannot tell the difference — which is exactly why
values it sends stay labelled as simulated wherever they surface. It needs no broker and
no hardware. Open it from a phone at `http://<PC_IP>:8000/simulator`.

For the original scripted 90-second narrative instead, start the broker and run the
scenario player:

```bash
mosquitto -c mosquitto.conf -v            # terminal 1
python hub/simulator.py --mode demo       # terminal 2
```

### Full setup

**1. Dependencies**

```bash
pip install -r requirements.txt
```

> **Important:** it must be `uvicorn[standard]` and `websockets`. Plain `uvicorn` has no
> WebSocket support — the API works but `/ws` returns 404 and the dashboard renders
> blank with no server-side error.

**2. MQTT broker**

Install mosquitto, then run it with the included config (the default config binds only
localhost, so the phone and UNO Q could not reach it):

```bash
mosquitto -c mosquitto.conf -v
```

Open the firewall once, elevated:

```powershell
New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -LocalPort 1883 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Hub HTTP 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

**3. Local LLM via GenieX (optional but recommended)**

[GenieX](https://github.com/qualcomm/geniex) is Qualcomm's on-device inference runtime.
It ships an OpenAI-compatible server, so no application code changes:

```bash
# Windows ARM64: installer from https://github.com/qualcomm/geniex/releases
geniex pull ai-hub-models/Qwen3-4B-Instruct-2507
geniex serve                        # -> http://127.0.0.1:18181/v1
```

Those are already the defaults in `hub/llm.py`, so usually nothing to export. To
override, or to point at any other OpenAI-compatible endpoint:

```bash
export LLM_BASE_URL=http://127.0.0.1:18181/v1
export LLM_MODEL=ai-hub-models/Qwen3-4B-Instruct-2507
export LLM_ENABLED=1          # set 0 to force the deterministic narrator
```

GenieX offers two runtimes, and the choice is deliberate:

| Runtime | Model source | Format | Compute | We use it when |
|---|---|---|---|---|
| `qairt` | Qualcomm AI Hub, pre-compiled | per-chipset bundle | **NPU only** | default — maximum NPU performance |
| `llama_cpp` | Hugging Face | GGUF (`Q4_0` best for Hexagon) | NPU · GPU · CPU | a specific model is needed |

**Without any LLM the system still works** — `template_narrate()` produces the same
recommendations deterministically, and the dashboard labels which path produced each
card.

**4. Run the hub**

```bash
python hub/server.py
```

It prints the LAN URL to use from other devices.

**5. Kasa smart devices — the physical actuator**

Any TP-Link Kasa plug or bulb on the same LAN. Controlled **locally over port 9999** —
no cloud account, no API token, no credentials of any kind. Find yours:

```bash
.venv/Scripts/kasa.exe discover        # prints alias, IP, model, and whether it meters
```

> **Windows on ARM:** install deps with `pip install --only-binary=:all: -r requirements.txt`.
> `python-kasa` pulls `cryptography`, whose newest release has no `win_arm64` wheel and
> otherwise tries to build from Rust + OpenSSL source and fails. `--only-binary` makes pip
> resolve back to a version that ships one.

**6. Arduino UNO Q — Modulino sensing**

Attach a **Modulino** node to the Qwiic connector. On the UNO Q that connector is the
**secondary I2C bus (`Wire1`, MCU pins PD12/PD13)** — not the default `Wire`. Confirm what
is attached before writing any application code:

```bash
# on the board (or over `adb shell`), flash the scanner and read the monitor
arduino-cli compile --fqbn arduino:zephyr:unoq arduino/scanner
arduino-cli upload  --fqbn arduino:zephyr:unoq arduino/scanner
```

Expect at least one address under `Wire1` — a Knob answers at `0x3A`. Then flash the real
firmware and start the publisher:

```bash
arduino-cli compile --fqbn arduino:zephyr:unoq arduino/sketch
arduino-cli upload  --fqbn arduino:zephyr:unoq arduino/sketch

cp arduino/board.env.example arduino/board.env    # edit IPs/aliases for your LAN
cd arduino && set -a && . ./board.env && set +a
python3 uno_q_publisher.py
```

Expect `[kasa] lights -> …`, `MQTT connected`, and **`subscribed home/command/#`** — that
last line is what makes approvals reach the actuator; if it is missing, actuation is dead.

Three UNO Q specifics that will cost you an afternoon if you do not know them:

- **`Serial` needs the `Arduino_RouterBridge` library.** The board platform ships a stub
  header that hard-errors with "Please install the Arduino_RouterBridge library" until you
  do. It pulls `Arduino_RPClite` → `MsgPack` → `ArxContainer`/`ArxTypeTraits`/`DebugLog`.
- **The MCU's `Serial` is NOT a `/dev/ttyACM*` device.** It travels over RPMsg to the
  `arduino-router` service, which republishes it on **`tcp://127.0.0.1:7500`**. That is what
  `uno_q_publisher.py` reads; it falls back to `/dev/ttyACM*` for classic Arduino boards.
- **Board platform must be ≥ 0.55.0** for `Serial` support at all (and for the
  `ARDUINO_UNO_Q` define that points the Modulino library at `Wire1`).

No hardware at all: `python3 uno_q_publisher.py --fake-serial`.
No board, PC-attached MCU only: `python arduino/bridge.py --port COM5`.

**6b. USB-only fallback (no Wi-Fi for the board)**

The UNO Q exposes an **ADB interface over USB-C**, so the whole thing works with no
wireless at all — useful when venue Wi-Fi is hostile:

```bash
adb shell                              # a shell on the Dragonwing Linux side
adb reverse tcp:1883 tcp:1883          # board reaches the PC broker at 127.0.0.1:1883
```

Kasa devices still need the LAN, so in USB-only mode actuation degrades to `simulated`
and reports itself as such rather than pretending.

**6. Phone**

Join the same network, open `http://<PC_IP>:8000/phone`, tap **Enable alerts**.
Geolocation is optional and degrades silently to the manual HOME/AWAY toggle — browsers
commonly block it over plain HTTP.

**7. Cloud report (optional)**

```bash
export CLOUD_BASE_URL=<AI Cloud 100 endpoint>
export CLOUD_MODEL=<larger model>
```

Unset, the button still works and returns a deterministic Python report.

## Usage

### Run the scripted demo

```bash
python hub/simulator.py --mode demo          # 90-second narrative, loops
python hub/simulator.py --mode demo --speed 3 # faster, for rehearsal
python hub/simulator.py --mode random         # continuous plausible drift
```

The scenario prints a caption at each beat:

| t | What happens | Expected |
|---|---|---|
| 0s | home, evening, on-peak, TV + A/C + lights | no findings — everything is in use |
| 15s | user leaves, geofence crosses | presence → away |
| 25s | motion times out, loads still on | **R2 critical + R1 serious** |
| 45s | user acts, loads off | power collapses |
| 60s | next morning, 640 lux, lights on | **R3 warning** |
| 75s | dryer starts at 17:00 | **R6, rate delta only** |

### Drive it with no broker and no hardware

```bash
curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":false,"lux":110,"temp_c":23.6,"humidity":47}'
curl -X POST http://localhost:8000/api/load -H "Content-Type: application/json" \
  -d '{"key":"living/ac","state":"on","watts":1100}'
curl -X POST http://localhost:8000/api/presence -H "Content-Type: application/json" \
  -d '{"presence":"away","distance_m":2400}'
```

### Approve an action — the closed loop

Tap **Approve & turn it off** on the phone (or **Apply** on the dashboard). Or by hand:

```bash
curl -X POST http://localhost:8000/api/apply -H "Content-Type: application/json" \
  -d '{"reco_id":"r2-living-ac","action":"off","approved_by":"cli"}'
```

What happens: the hub runs the R7 pre-flight check → publishes
`home/command/living/ac` → the UNO Q drives the servo/relay and publishes
`home/actuator/living/ac` → the hub books the saving as **realized** and the finding
closes.

To see the guardrail refuse an unsafe action, set the room above 27 °C first:

```bash
curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":false,"lux":110,"temp_c":29.5,"humidity":52}'
# a subsequent apply on the A/C returns HTTP 409 with the reason
```

### Verify the install

```bash
python smoke_test.py
```

38 checks covering arithmetic, every rule, the comfort guardrail (as a filter, an
actuation gate, and a visible declined decision), LLM fallback, cloud fallback, the
session recorder and feedback endpoint, the live server, and the full approve → command
→ realized-saving loop. Exits non-zero on failure.

> **Run it with nothing else live.** `smoke_test.py` starts its own hub and sets up its
> own fixtures, so a running instance of the system will make it fail in confusing ways:
>
> - **A live `uno_q_publisher.py`** keeps publishing real load state to the same broker.
>   The test's hub ingests it and the fixtures get overwritten — seen as
>   `total watts computed 10.8 W` (that 10.8 W was a real smart bulb).
> - **Another `hub/server.py`** connects with the same MQTT client id
>   (`hub-orchestrator`), and the broker evicts whichever connected first. It also
>   already holds port 8000, so the test ends up reading the *other* hub's state.
>
> Stop both first:
> ```bash
> adb shell "pkill -f uno_q_publisher.py"     # board publisher
> # and stop any hub/server.py you started
> ```
> This is the same "one process per client id" rule that applies to the demo itself.

### Measure performance

```bash
python hub/benchmark.py              # human-readable report
python hub/benchmark.py --markdown   # table for this README
python hub/benchmark.py --json       # machine-readable
```

Run it twice to compare runtimes:

```bash
LLM_ENABLED=0 python hub/benchmark.py                             # deterministic control
LLM_BASE_URL=http://127.0.0.1:18181/v1 python hub/benchmark.py    # GenieX on the NPU
```

### Test the individual modules

```bash
python hub/energy_model.py    # prints a hand-checkable table of estimates
python hub/rules.py           # fires each rule in its own scenario
python hub/llm.py             # compares LLM and template narration
python hub/cloud_report.py    # generates a report from a synthetic digest
```

Actuation can also be tested directly from a serial monitor at 115200 baud — type
`CMD lights off` and the sketch replies `{"ack":"lights","state":"off","ok":true}`.

## MQTT topic contract

| Topic | Publisher | Payload |
|---|---|---|
| `home/sensors/<room>` | UNO Q | `{"occupancy":bool,"lux":int,"temp_c":float,"humidity":float,"ts":epoch}` |
| `home/loads/<room>/<load>` | UNO Q / sim | `{"state":"on"\|"off","watts":float,"ts":epoch}` |
| `home/context/user` | phone | `{"presence":"home"\|"away","distance_m":int,"battery":int,"ts":epoch}` |
| `home/reco` | hub | `{"id","severity","title","body","kwh","usd","co2_kg","actions"}` |
| `home/command/<room>/<load>` | hub | `{"action":"on"\|"off","reco_id":str,"approved_by":str,"ts":epoch}` |
| `home/actuator/<room>/<load>` | UNO Q | `{"state":"on"\|"off","source":str,"reco_id":str,"ok":bool,"ts":epoch}` |
| `home/state` | hub | full snapshot |

All payloads are JSON, all carry `ts`, all units appear in the key name.

The `command` / `actuator` pair is the actuation loop: the hub publishes an approved
command, the UNO Q executes it physically and confirms. `source` reports honestly how it
was carried out (`mcu_serial`, `simulated`, or `serial_error`), so a confirmation never
overstates what happened.

## Repo layout

```
code/
  hub/
    energy_model.py    deterministic arithmetic — the source of every number
    rules.py           R1-R7 waste detection, no LLM
    llm.py             GenieX narration + deterministic fallback
    server.py          MQTT, state fusion, FastAPI, WebSocket, /api/apply + safety gate
    simulator.py       scripted and random sensor feeds
    cloud_report.py    AI Cloud 100 deep report + deterministic fallback
    benchmark.py       latency / memory / efficiency measurements
    recorder.py        session JSONL — audit trail and training substrate
    occupancy_model.py learned occupancy inference (shadow by default)
    models/            trained artifacts, weights in plain JSON
  tools/
    train_occupancy.py fetch UCI 357, train, export models/occupancy_lr.json
    build_dataset.py   sessions -> labelled CSV, with an honest verdict
    reconfigure_network.py  re-point the demo at the current LAN
  dashboard/index.html hub dashboard with Apply buttons (no build step)
  phone/index.html     phone PWA with Approve buttons (no build step)
  arduino/
    sketch/sketch.ino  STM32 firmware: sensors + servo/relay actuator
    uno_q_publisher.py Dragonwing Linux publisher + command subscriber
    bridge.py          USB fallback, runs on the PC
  mosquitto.conf       broker config that binds 0.0.0.0
  requirements.txt
  smoke_test.py        38 checks, including the actuation loop and safety gate
  LICENSE              MIT
```

## Limitations, honestly

- **Load power is modelled, not metered.** Wattages are published typical figures (DOE,
  Energy Star), each carrying a `source` string visible in the audit panel. Clamp meters
  or smart plugs are the obvious next step.
- **Lux is uncalibrated.** A photoresistor divider approximates lux; we use it only
  against a coarse threshold, and the rule evidence says so.
- **R4 is a heuristic.** We infer an open window from A/C runtime, temperature stall and
  humidity — we do not sense the window. The evidence text states this.
- **Actuation is human-approved by design, not autonomous.** The system will not act
  without a tap. That is a deliberate trust decision rather than a missing feature: R7
  demonstrates the machine refusing its own advice, and we would want a lot more
  validation before removing the human from that loop.
- **One actuator, one room.** The command topic is already per-room/per-load and the
  rules iterate over rooms, so scaling is a matter of adding publishers — but we
  demonstrate one room and one switch.
- **NPU figures need the QUAD profile to be authoritative.** `benchmark.py` measures
  end-to-end latency on this machine; `/quad-profile` measures on real silicon with power
  and utilization. Run both before quoting NPU numbers.
- **NPU acceleration not yet measured.** The local model runs through the runtime's
  default path. QNN/Hexagon acceleration is the natural next step; we did not want to
  claim a number we had not measured.

## Submission

**Repository:** <https://github.com/gowtham612/ai-home-energy-concierge> — public, MIT licensed.

This is the repository submitted via the organizers' Microsoft Form. The application
lives in `code/`; this file is its README (description, setup, usage). The repository
root additionally carries the planning packet and the presentation deck.

## Team

| Name | Email | Role |
|---|---|---|
| Gowtham Raj Baskaran | gbaskara@qti.qualcomm.com | Implementation — hub, rules engine, UNO Q firmware, Kasa actuation |
| Nanda Kishore Nagabhushana | nnagabhu@qti.qualcomm.com | Architecture & requirements · project proposal |
| Yash Joshi | yashjosh@qti.qualcomm.com | On-device AI — GenieX NPU narration, QUAD profiling, benchmarks |
| Ajay Reddy | areddy@qti.qualcomm.com | Product & demo — energy model, dashboard/simulator UX, presentation |

The four of us developed the concept and architecture together; the roles above are
where each of us carried the work.

## License

Licensed under the **MIT License** — see [LICENSE](LICENSE). All code in this
repository is open source.

## References

- [Qualcomm GenieX](https://github.com/qualcomm/geniex) — on-device inference runtime used for local LLM narration
- [Qualcomm AI Hub](https://aihub.qualcomm.com) — model source for the `qairt` NPU path
- [Arduino UNO Q documentation](https://docs.arduino.cc/hardware/uno-q/) — dual-brain MPU/MCU architecture and the Arduino Bridge RPC library
- [Qualcomm AI Developer Workflow docs](https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/welcome.html)
- [UCI Occupancy Detection (dataset 357)](https://archive.ics.uci.edu/dataset/357/occupancy+detection) — Candanedo, L. & Feldheim, V. (2016), *Accurate occupancy detection of an office room from light, temperature, humidity and CO2 measurements using statistical learning models*, Energy and Buildings 112, 28–39. Training corpus for `hub/occupancy_model.py`
- Load power figures: US DOE and ENERGY STAR published typical values (each entry in `hub/energy_model.py` carries its own `source` string)
- Tariff structure: SDG&E TOU-DR1 residential time-of-use schedule
- Grid carbon intensity: EPA eGRID CAMX region / California Energy Commission

## Notes

- **Archetype:** IoT Sensor → Actuator Physical AI. The loop closes with human approval —
  the system recommends, the user approves, the UNO Q physically actuates. See
  [Closing the loop](#closing-the-loop-human-approved-actuation).
- **On QUAD codegen:** the organizers' project sheet flags gap **G6** (UNO Q
  sensor/actuator + GPIO codegen, not yet implemented) as blocking the `generate_code`
  stage. We did not need it — the sketch, the MPU-side publisher, and the actuator path
  are hand-written and tested. QUAD is used for what it does well here: profiling on
  real silicon.
- Lux, load power and the R4 window heuristic are approximations, each labelled as such
  in the UI evidence text. See [Limitations](#limitations-honestly).
