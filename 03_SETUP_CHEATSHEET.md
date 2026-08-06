# Setup cheatsheet — carry this on paper

Everything here has been run and verified on Windows on Arm. The one non-obvious
dependency that will cost you an hour if you miss it is flagged **CRITICAL**.

---

## 0. Venue facts

| Item | Value |
|---|---|
| **Wi-Fi** | `HaQathon` / `tA20LO26s` — **do NOT use Hydra or Pandora** |
| Laptop login | `QCWorkshopX` / `QCWorkshop123`, PIN `13243546` |
| Office hours | Tue & Thu 1:30–3:30 PM · Fri 9 AM–12 PM · Discord `#support` |
| **Submission** | **Fri 12:00 PM** (decks disagree, 12 vs 1 PM — assume 12, confirm Tuesday) |
| Demos | Fri 1:00–4:15 PM, order randomized, emailed Thursday AM |

## 1. The gotcha that already bit us

**`pip install uvicorn` is NOT enough — the WebSocket endpoint returns 404 and the
dashboard renders blank with zero errors in the server log.** You need:

```bash
pip install "uvicorn[standard]" websockets
```

Symptom if you forget: browser console shows
`WebSocket connection failed: Unexpected response code: 404`, all KPIs read 0, no
chart paths. The REST API works fine, which makes it look like a front-end bug.
It is not.

---

## 2. Python deps (Copilot+ PC)

```bash
pip install -r requirements.txt
```

Qualcomm internal mirror, if the public one is blocked:

```bash
pip install <pkgs> -i https://devpi.qualcomm.com/qcom/dev/+simple --trusted-host devpi.qualcomm.com
```

Verify:
```bash
python -c "import fastapi, uvicorn, paho.mqtt.client, requests, serial, websockets; print('all OK')"
```

## 3. GenieX — the NPU-backed local LLM

This is what turns "a local LLM" into "an NPU-accelerated local LLM," which is the
language the 40-point Technical Implementation criterion uses.

```bash
# Windows ARM64: use the installer from https://github.com/qualcomm/geniex/releases
# (a `pip install geniex` Python binding also exists)

geniex pull ai-hub-models/Qwen3-4B-Instruct-2507
geniex serve                      # serves http://127.0.0.1:18181/v1
```

Point the hub at it (these are now the **defaults** in `hub/llm.py`, so usually no
env vars are needed at all):

```bash
export LLM_BASE_URL=http://127.0.0.1:18181/v1
export LLM_MODEL=ai-hub-models/Qwen3-4B-Instruct-2507
```

Verify independently before blaming the hub:
```bash
curl http://127.0.0.1:18181/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"ai-hub-models/Qwen3-4B-Instruct-2507","messages":[{"role":"user","content":"say READY"}]}'
```

**Two runtimes — know which you chose and why (judges reward a justified choice):**

| Runtime | Model source | Format | Compute | Use when |
|---|---|---|---|---|
| `qairt` | Qualcomm AI Hub, pre-compiled | per-chipset bundle | **NPU only** | you want maximum NPU performance — **our default** |
| `llama_cpp` | Hugging Face, any GGUF | GGUF | NPU · GPU · CPU | you need a specific model; use **`Q4_0`** (best Hexagon support) |

Browse models: `https://aihub.qualcomm.com/models?runtime=geniex_qairt,geniex_llamacpp`

Force the deterministic narrator for rehearsals (stable timing, no model needed):
```bash
export LLM_ENABLED=0
```

## 4. QUAD — for measurement, not codegen

QUAD is a hosted MCP server on real Qualcomm silicon, driven from Claude Code or
another MCP client. **Use it at the dedicated QUAD support sessions / office hours.**

```powershell
.\install.ps1

quad-client install --transport sse-http `
  --sse-url https://quad.infra.foundries.io/mcp `
  --sse-auth-token-env QUAD_MCP_TOKEN

quad-client connect-test sse-http `
  --sse-url https://quad.infra.foundries.io/mcp --auth-token "$env:QUAD_MCP_TOKEN"

quad-client detect     # what hardware do I have?
quad-client doctor     # is my setup healthy?
```

Then, in the MCP client:

| Skill | Why we run it |
|---|---|
| `/quad-detect` | confirms chipset / NPU / SDK status — free, do it first |
| **`/quad-profile`** | **the important one** — real NPU latency, power, utilization for the 40-pt criterion |
| **`/quad-orchestrate`** | CPU vs GPU vs NPU allocation comparison |
| `/quad-aihub` | browse/score AI Hub models if you want to swap the narration model |

**Skip `/quad-codegen`.** The organizers' own project sheet marks `generate_code` as
**Blocked by gap G6** (UNO Q sensor/actuator + GPIO codegen, not started). Our sketch,
publisher and actuator are hand-written and tested — we do not need it. Say so on
stage rather than pretending the stage ran.

Paste the profile report into `README.md` next to the `benchmark.py` table.

## 5. MQTT broker (Copilot+ PC)

Install mosquitto (winget, or the installer from mosquitto.org).

Use the included `mosquitto.conf` — the default config only listens on localhost,
which means the phone and UNO Q cannot reach it:

```
listener 1883 0.0.0.0
allow_anonymous true
```

Run it in a visible window so you can see connects during the demo:
```bash
mosquitto -c mosquitto.conf -v
```

Open the firewall (elevated PowerShell, once):
```powershell
New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -LocalPort 1883 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Hub HTTP 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

Verify from another machine on the same network:
```bash
mosquitto_sub -h <PC_IP> -t 'home/#' -v
```

## 6. Run the hub

```bash
cd code/hub
python server.py
```

It prints the LAN URL. Dashboard at `http://<PC_IP>:8000/`, phone at
`http://<PC_IP>:8000/phone`.

## 7. Quickstart with no hardware (3 commands)

This is the path to have working on Day 1, and your stage fallback all week.

```bash
mosquitto -c mosquitto.conf -v          # terminal 1
python hub/server.py                    # terminal 2
python hub/simulator.py --mode demo     # terminal 3
```

Rehearse faster:
```bash
python hub/simulator.py --mode demo --speed 3
```

## 8. Verify everything

```bash
python smoke_test.py       # 38 checks incl. the actuation loop and safety gate
python hub/benchmark.py    # the numbers for the 40-point criterion
python hub/benchmark.py --markdown   # paste straight into README.md
```

## 9. No broker at all — REST injection

The hub can be driven entirely over REST, so even a dead broker is survivable:

```bash
curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":false,"lux":110,"temp_c":23.6,"humidity":47}'
curl -X POST http://localhost:8000/api/load -H "Content-Type: application/json" \
  -d '{"key":"living/ac","state":"on","watts":1100}'
curl -X POST http://localhost:8000/api/presence -H "Content-Type: application/json" \
  -d '{"presence":"away","distance_m":2400}'

# approve a recommendation -> commands the actuator
curl -X POST http://localhost:8000/api/apply -H "Content-Type: application/json" \
  -d '{"reco_id":"r2-living-ac","action":"off","approved_by":"cli"}'
```

Note the hub tracks occupancy duration itself, so R1 (unoccupied lights) needs the
room reported occupied first, then unoccupied, then ~10 minutes. **R2 (away + HVAC)
fires within one 5-second cycle — that is the one to demo live.**

## 10. Arduino UNO Q — sensors AND actuator

**MCU side (STM32, sketches run over Zephyr):** flash `arduino/sketch/sketch.ino`
via Arduino App Lab (recommended — it targets both brains) or the Arduino IDE/CLI
(MCU only). Verify at 115200 baud — one JSON line per second:
```
{"occupancy":true,"lux":420,"temp_c":24.5,"humidity":48.0,"raw_pir":1}
```

Test actuation by hand from the serial monitor — type:
```
CMD lights off
```
Expect the servo to move, the buzzer to chirp, and an ack line back:
```
{"ack":"lights","state":"off","ok":true}
```

**Linux side (Dragonwing, Debian):**
```bash
pip3 install paho-mqtt pyserial
ROOM=living MQTT_HOST=<PC_IP> python3 uno_q_publisher.py
```

It publishes sensors **and subscribes to `home/command/#`** to drive the actuator.

Develop before hardware works:
```bash
python3 uno_q_publisher.py --fake-serial --broker <PC_IP>
```

Flip loads mid-demo with no rewiring:
```bash
echo '{"lights":{"state":"on","watts":240},"ac":{"state":"on","watts":1100}}' > /tmp/loads.json
```

**Fallback if the Linux side is unavailable** — run on the PC over USB:
```bash
python arduino/bridge.py --port COM5 --broker localhost
```

### Wiring

| Function | Pin | Notes |
|---|---|---|
| PIR motion out | D2 | HIGH = motion |
| LDR divider (10k to GND) | A0 | 3V3 – LDR – A0 – 10k – GND |
| DHT22 data | D4 | set `USE_DHT 0` if absent |
| **Servo signal** | **D9** | presses a physical light switch |
| **Relay / LED** | **D7** | actuator fallback / visible indicator |
| **Buzzer** | **D8** | audible "command landed" |

**Power the servo from its own 5 V supply, not the board's 3V3 rail** — a stalling
servo browns out the MCU and you will spend an hour chasing phantom resets. Tune
`SERVO_OFF_DEG` / `SERVO_ON_DEG` on the bench so the arm reaches the switch without
pressing hard against it.

If the DHT22 misbehaves, set `USE_DHT 0` — it emits documented stub values rather than
breaking the JSON stream. Say so if asked; never claim a reading you did not take.

**Also read on Day 1:** `github.com/qualcomm/edge-ai-labs-arduino/tree/main/rpc` — the
official Arduino RPC example, directly relevant to the MPU↔MCU path. And find the real
**App Lab Python API** on the board (`pip list`, App Lab example projects). **Do not
invent that API** — plain serial, which our tested code uses, is the fallback.

## 11. Phone (Galaxy S25)

1. Join the **same network** as the PC (or the PC's hotspot).
2. Chrome → `http://<PC_IP>:8000/phone`
3. Tap **Enable alerts**.
4. Optional: at home, tap **Set home here**, then **Use my location**.

Geolocation over plain HTTP is often blocked. **The manual HOME/AWAY toggle always
works — demo with that.** Treat geofencing as a bonus you mention, not a dependency.

The **Approve & turn it off** button on each card is what triggers physical actuation.

## 12. Cloud deep report

```bash
export CLOUD_BASE_URL=<AI-100 OpenAI-compatible endpoint>
export CLOUD_MODEL=<larger model>
export CLOUD_API_KEY=<key if needed>
```

With these unset the button still works and returns a deterministic Python report —
verified. Never shows an error.

## 13. PC as hotspot (rehearse this — likely your demo primary)

Windows Settings → Network & internet → Mobile hotspot → On. Join the UNO Q and
phone to it. Re-check the PC's IP afterwards (`ipconfig`) — it changes, and every
device needs the new one.

---

## Pre-demo checklist — run at the actual demo table

- [ ] `ipconfig` — note the IP; it changed
- [ ] mosquitto running in a visible window
- [ ] `mosquitto_sub -h <IP> -t 'home/#' -v` shows UNO Q traffic
- [ ] hub started, banner shows the right IP
- [ ] dashboard loads on the presentation display, at the resolution you will project
- [ ] phone loads, alerts enabled, presence toggle moves the dashboard
- [ ] **Approve button physically actuates the servo** — test it once, for real
- [ ] `geniex serve` running, or `LLM_ENABLED=0` set for deterministic timing
- [ ] simulator ready in a terminal, one keystroke from running
- [ ] Thursday's video open in a background tab (**including the actuation shot**)
- [ ] benchmark numbers and QUAD report on the slide
- [ ] laptop on mains power, sleep disabled, notifications silenced
