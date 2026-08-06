# Bring-up guide — getting this running on the Arduino UNO Q

> ## ⚠ SUPERSEDED for the hardware steps — read this first
>
> **Steps 2-5 and 9 below describe a breadboard build (PIR on D2, photoresistor
> on A0, DHT22 on D4, servo on D9) that we never had the parts for.** They are
> kept for reference and for anyone who does have that hardware.
>
> **What was actually built and verified instead** (2026-08-05):
> a **Modulino Knob** on the Qwiic connector for sensing, **TP-Link Kasa** smart
> bulb/plug on the LAN for real physical actuation, and a browser **sensor
> simulator** at `/simulator` for everything else. The full loop is closed:
> approve → the actual bulb goes dark, confirmed by its own energy meter.
>
> - Architecture and reasoning: **`08_HARDWARE_PIVOT_PLAN.md`**
> - What was built, every gotcha, and how to restart it: **`07_QUAD_SESSION_LOG.md` §0 and §14**
> - Setup instructions: **`code/README.md`**
>
> Steps that still apply as written: **Step 0** (prove the PC side first) and
> **Step 12** (Plan B, no board at all). Step 1's `/quad-detect` advice is still
> useful, but the board is now reached over **ADB/USB-C**, not SSH.

Step-by-step, in the order you should actually do it, with a **verification gate after
every step** so you find problems one at a time instead of debugging four at once.

> **Current state, confirmed on the hackathon Copilot+ PC (update this if it drifts):**
> board connected via USB-C and visible in **App Lab** · Wi-Fi configured on the board ·
> a Linux user password is set · **SSH is enabled and reachable via PuTTY / `plink.exe`** ·
> **Claude Code with the QUAD skills is installed on this same PC, and `/quad-detect`
> already finds the UNO Q.**
>
> That resolves the shell-access question this guide originally treated as a discovery
> step — see the rewritten Step 1. **Two things remain genuinely unverified** and stay
> written as decision points: whether `DHT.h` / `Servo.h` compile for this Zephyr-based
> Arduino core (Step 4), and exactly how the Linux side exposes the MCU's serial port
> (Step 6). **Do not let anyone invent an API for either** — check, then take the
> documented fallback if the answer is no. Every fallback here is already tested.
>
> **⚠ New secret to protect.** The board's Linux password must **never** appear in any
> file, commit message, or log that could reach the public repo
> (`github.com/gowtham612/ai-home-energy-concierge`). Type it interactively when PuTTY
> prompts; don't put it on a command line with `-pw`, and don't paste it into a README,
> a script, or this guide.

**Time budget: 60–75 minutes to a working sensor stream (shell access is no longer the
unknown it was), 2 hours including actuation.** If you blow through that, jump to §12
(Plan B) — it gets you a demo with zero board work.

---

## What you are building toward

```
STM32U585 (MCU)                    Dragonwing QRB2210 (Linux)          Copilot+ PC
─────────────────                  ──────────────────────────          ───────────
sketch.ino                         uno_q_publisher.py                  server.py
  1 Hz sensor JSON  ──serial──▶      smoothing, occupancy FSM  ──MQTT──▶  rules + LLM
  servo/relay/buzzer ◀──serial──     command subscriber        ◀──MQTT──   /api/apply
```

Two programs on two different processors, talking over a serial link, plus MQTT over
Wi-Fi to the PC. Each is independently testable — and that is how we bring it up.

**Everything below runs on the same Copilot+ PC** — the hub, App Lab, PuTTY/plink, and
Claude Code + QUAD are all on that one X Elite laptop. `<PC_IP>` is *that laptop's own*
IP on whatever network the UNO Q joined (venue Wi-Fi or its hotspot) — not a second
machine.

---

## Step 0 — Get the PC side working first (15 min)

**Do not touch the board yet.** Prove the hub works, so that when the board misbehaves you
know the problem is the board.

Clone the tested, public repo if this PC doesn't have it yet — everything from here on
assumes this exact code:

```bash
git clone https://github.com/gowtham612/ai-home-energy-concierge.git
cd ai-home-energy-concierge/code
pip install -r requirements.txt
python smoke_test.py
```

✅ **Gate: `38/38 checks passed`.** If not, fix that before going near hardware. The most
likely cause is a plain `pip install uvicorn` without the `[standard]` extra.

Then start the broker and hub in two terminals, and note the LAN IP the hub prints:

```bash
mosquitto -c mosquitto.conf -v      # terminal 1
python hub/server.py                # terminal 2  → note the printed IP
```

✅ **Gate:** the dashboard loads in a browser at the printed URL. Write that IP down —
call it `<PC_IP>`. You will need it three more times.

---

## Step 1 — Confirm the board and reach a Linux shell (10 min)

This step used to be a discovery exercise. It isn't anymore — SSH already works. Use it.

### 1a. Free diagnostic first: ask QUAD what it sees

Since Claude Code + QUAD are already set up and `/quad-detect` already finds the board,
get its report before touching anything:

```
/quad-detect
```

or, from a plain terminal on the PC:

```bash
quad-client detect
quad-client doctor      # flags setup problems with an exact fix for each
```

Read the chipset/NPU/SDK-status output. It confirms you're talking to the right board and
may surface driver or permission issues before you go any further. This costs nothing —
`/quad-detect` is fully automated per the organizers' own tool table.

### 1b. Reach the Dragonwing Linux shell over SSH

PuTTY and `plink.exe` are already configured on this PC. Two ways to connect, in order of
preference:

**If a saved PuTTY session for the board already exists** (check PuTTY's *Session → Load*
list, or ask whoever set it up what it's named):

```bash
plink.exe -load "<saved-session-name>" "uname -a && python3 --version"
```

**Otherwise, connect directly** — you will need the board's IP (check the router's client
list, App Lab's device info panel, or `arp -a` on the PC right after the board joins
Wi-Fi) and the Linux username:

```bash
plink.exe -ssh <user>@<board-ip>
# type the password when prompted — do NOT pass it with -pw on the command line;
# that leaves it sitting in shell history and process listings
```

For an interactive session instead of one-shot commands, use `putty.exe` with the same
`-load` or `-ssh` arguments — it opens a normal terminal window.

✅ **Gate 1:** you get a shell and can run:

```bash
uname -a                 # confirms the Dragonwing Linux side
python3 --version        # need 3.x
ip addr | grep inet      # the board's IP — must be reachable from <PC_IP>
```

✅ **Gate 2:** `ping <PC_IP>` from the board succeeds. **If this fails, nothing else will
work** — fix networking now. Both devices must be on the same network (venue `HaQathon`
or the PC's own hotspot).

> **If SSH ever stops working** (session expired, board rebooted with different
> settings), the fallbacks are: App Lab's own console if it exposes one, `adb shell` if
> `adb` turns out to be present (QUAD's `hardware_detect` for Linux/robotics targets
> commonly uses ADB, so it may already be on this PC — check `where adb.exe`), or HDMI +
> keyboard directly into the board.

---

## Step 2 — Wire the sensors only (15 min)

**Wire sensors first, actuator later.** One subsystem at a time.

| Function | Pin | Wiring |
|---|---|---|
| PIR motion `OUT` | **D2** | VCC→5V, GND→GND, OUT→D2 |
| LDR / photoresistor | **A0** | 3V3 → LDR → A0 → 10 kΩ → GND (a divider) |
| DHT22 `DATA` | **D4** | VCC→3V3, GND→GND, DATA→D4, plus a 10 kΩ pull-up from DATA to VCC |

Leave the servo, relay and buzzer disconnected for now.

> **If you have Modulino nodes** (Qwiic connector) instead of discrete sensors, use them —
> they are far less error-prone. You will need to adapt the sketch's read calls to the
> Modulino library, which is a small, contained change. Keep the **JSON output format
> byte-identical**; everything downstream depends on it.

---

## Step 3 — Flash a minimal sensor sketch and prove the JSON stream (20 min)

**Do not flash our full sketch yet.** Our sketch has two external dependencies that may not
be available for Zephyr on this board (`DHT.h`, `Servo.h`). Prove the basics first.

In App Lab, create a new sketch and flash this:

```cpp
// Bring-up test 1: sensors only, no libraries, no actuator.
const uint8_t PIN_PIR = 2;
const uint8_t PIN_LDR = A0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_PIR, INPUT);
}

void loop() {
  int pir = digitalRead(PIN_PIR);
  int raw = analogRead(PIN_LDR);
  Serial.print("{\"pir\":");   Serial.print(pir);
  Serial.print(",\"ldr\":");   Serial.print(raw);
  Serial.println("}");
  delay(1000);
}
```

Open the serial monitor at **115200 baud**.

✅ **Gate 1:** one clean JSON line per second.
✅ **Gate 2:** `"pir":1` when you wave at the sensor, `0` when still.
✅ **Gate 3:** `"ldr"` changes substantially when you cover the photoresistor.

**If the LDR reading barely moves**, your divider is wrong — check the 10 kΩ goes to GND
and that you are reading the junction, not a rail.

**If PIR is stuck at 1**, most PIR modules have sensitivity and hold-time trimpots. Turn
the hold time down. Some also have a jumper for retriggering mode.

---

## Step 4 — DECISION POINT: which libraries does this board actually have? (10 min)

In App Lab's library manager, search for:

1. **`DHT sensor library`** (Adafruit) — for the DHT22
2. **`Servo`** — for the actuator

Then set the flags at the top of `arduino/sketch/sketch.ino` accordingly:

| Situation | What to do |
|---|---|
| Both libraries available | Leave `#define USE_DHT 1` and `#define USE_SERVO 1`. Proceed. |
| DHT unavailable / won't compile | Set **`#define USE_DHT 0`**. The sketch emits documented stub temp/humidity and keeps working. **Say so if a judge asks** — never claim a reading you did not take. |
| Servo unavailable / won't compile | Set **`#define USE_SERVO 0`** and drive the **relay on D7** instead. Actuation still happens physically. |
| Neither available | Set both to 0. You still get PIR + lux + relay — enough for the full demo. |

> **Why this is a decision point, not an instruction:** Zephyr-based Arduino cores do not
> always carry the classic AVR libraries. This is exactly the kind of thing that eats two
> hours if you fight it. **Check, choose, move on.**

Rule R4 (open-window heuristic) is the only thing that degrades without a real DHT22, and
it is the least important of the eight rules.

---

## Step 5 — Flash the real sketch (10 min)

Now flash `code/arduino/sketch/sketch.ino` with your flags from Step 4.

✅ **Gate 1:** one JSON line per second, in this exact shape:

```json
{"occupancy":true,"lux":420,"temp_c":24.5,"humidity":48.0,"raw_pir":1}
```

✅ **Gate 2:** `occupancy` goes `true` when you wave, and stays true for ~30 s after motion
stops (that is the debounce hold — it is deliberate, not a bug).

✅ **Gate 3:** nothing else appears on serial. No banners, no debug prints. The Linux-side
parser expects JSON lines only.

**If you see garbage characters**, the baud rate is wrong — it must be 115200 on both ends.

---

## Step 6 — DECISION POINT: how does Linux see the MCU's serial? (15 min)

This is the step that could not be verified off-site, and the one place you must look at
the actual board rather than trust this document. You have an SSH shell from Step 1 —
use it.

On the Linux side (over your `plink`/PuTTY session), run:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null
dmesg | tail -30
python3 -c "import serial; print('pyserial OK')"
```

| What you find | What to do |
|---|---|
| A `/dev/ttyACM*` or `/dev/ttyUSB*` device appears | **Best case.** Our `uno_q_publisher.py` auto-detects these — go to Step 7 unchanged. |
| Nothing, but App Lab has a **Bridge / RPC** mechanism | Use it. Read the App Lab **example projects** — they show the sketch-side registration and the Python-side call as a matched pair. Also read `github.com/qualcomm/edge-ai-labs-arduino/tree/main/rpc`. Then use the prompt in `01_LLM_PROMPT_PACK.md` §"Day-1 discovery task" to port the publisher, **pasting the real docs in.** Since Claude Code is already installed on this PC with the relevant skills, this is a good candidate to hand to that session directly, with the real Bridge documentation pasted in — not guessed. |
| Nothing, and Bridge is unclear | **Take the fallback.** Go to Step 11 — run `bridge.py` on the PC over USB. Costs you one architecture bullet point and nothing else. |

Install the Python deps on the board while you are here:

```bash
pip3 install paho-mqtt pyserial
```

> **`/quad-detect`'s own tool table calls `hardware_detect` fully automated for a
> `linux/robotics` target** — it is worth re-running now that you have shell access, since
> its output may directly answer what the Linux side calls the MCU's serial device
> (Zephyr boards over USB commonly enumerate as ACM CDC devices, which is exactly the
> `/dev/ttyACM*` path our code already auto-detects).

> **Do not let anyone guess at the Bridge API.** If a teammate starts writing
> `from arduino import Bridge` without having read real documentation, stop them — that is
> how you lose an afternoon. Plain serial already works and is already tested.

---

## Step 7 — Get the code onto the board, then run the publisher with fake data (10 min)

Prove MQTT works *before* adding the serial link as a variable.

The code is public now, so pull it directly onto the board rather than copying files by
hand:

```bash
# on the board, over the same SSH session
git clone https://github.com/gowtham612/ai-home-energy-concierge.git
cd ai-home-energy-concierge/code/arduino
```

If the board has no outbound internet on the venue network, `scp` the `arduino/` folder
from the PC instead:

```bash
# from the PC
plink.exe -load "<saved-session-name>" -pscp -r "code\arduino" <user>@<board-ip>:~/arduino
```

(`pscp.exe` — PuTTY's `scp` equivalent — should already be alongside `plink.exe`; check
`where pscp.exe` on the PC if the command above isn't found.)

Then run it with fake sensor data:

```bash
ROOM=living MQTT_HOST=<PC_IP> python3 uno_q_publisher.py --fake-serial
```

✅ **Gate — you must see all four lines:**

```
[uno_q] room=living broker=<PC_IP>:1883 fake=True
[uno_q] MQTT connected rc=0
[uno_q] subscribed home/command/#
[uno_q] command listener ready (actuator source: simulated)
[uno_q] pub (change) occ=True lux=192 22.7C 47%
```

**`subscribed home/command/#` is the critical line.** If it is missing, actuation will not
work later — and it is the bug that would silently break your demo. It comes from the
`on_connect` callback, which is the only reliable place to register subscriptions.

On the PC, confirm the traffic arrives:

```bash
mosquitto_sub -h localhost -t 'home/#' -v
```

✅ **Gate:** you see `home/sensors/living {...}` messages, and the dashboard's "Live device
state" table shows a `living room` row updating.

**If MQTT will not connect:** check `ping <PC_IP>` from the board, then the PC firewall
rule for port 1883, then that `mosquitto.conf` has `listener 1883 0.0.0.0` (the default
mosquitto config binds localhost only — this is the single most common failure).

---

## Step 8 — Swap fake data for the real sensors (10 min)

Drop the `--fake-serial` flag:

```bash
ROOM=living MQTT_HOST=<PC_IP> python3 uno_q_publisher.py
```

✅ **Gate 1:** `[uno_q] MCU serial found at /dev/ttyACM0` (or whichever path).
✅ **Gate 2:** `actuator source: mcu_serial` — **not** `simulated`.
✅ **Gate 3:** wave at the PIR → within a second or two you see
`[uno_q] pub (change) occ=True ...` and the dashboard's occupancy tile flips to
**Occupied**.
✅ **Gate 4:** cover the LDR → lux drops on the dashboard.

**This is the moment the real sensor reaches the real dashboard.** If it works, you have
the sensing half of Archetype E.

Note the publish rate: you will see far fewer messages than one per second. That is the
edge filter working — it publishes only on a material change or a 10 s heartbeat, which is
where the measured **88.7% traffic reduction** comes from.

---

## Step 9 — Wire and test the actuator (20 min)

Now the half that satisfies the archetype.

| Function | Pin | Notes |
|---|---|---|
| Servo signal | **D9** | orange/white wire |
| Servo power | **its own 5 V supply** | ⚠ see the warning below |
| Relay `IN` | **D7** | or an LED with a 220 Ω resistor |
| Buzzer `+` | **D8** | passive buzzer or piezo |

> ⚠ **Power the servo from its own 5 V supply, sharing GND with the board — not from the
> board's 3V3 rail.** A servo stalling against a switch draws enough current to brown out
> the MCU, and you will chase phantom resets for an hour. This is the single most common
> hardware mistake in this build.

### Test the actuator from the serial monitor first

Before involving MQTT at all, open App Lab's serial monitor and type:

```
CMD lights off
```

(No App Lab access at that moment? The same serial port is reachable from your SSH
session on the Linux side too — `python3 -c "import serial; s=serial.Serial('/dev/ttyACM0',115200); s.write(b'CMD lights off\n')"` using whatever port Step 6 identified. App Lab's monitor is just the more convenient UI.)

✅ **Gate 1:** the buzzer chirps.
✅ **Gate 2:** the servo sweeps to the "off" angle, holds ~500 ms, and **returns to rest**.
✅ **Gate 3:** exactly one ack line comes back:

```json
{"ack":"lights","state":"off","ok":true}
```

Try `CMD lights on` too — the servo should go to the opposite angle.

### Aim the servo at a real switch

Mount the servo so its arm presses one side of a rocker light switch. Then tune the two
angles at the top of the sketch:

```cpp
const int SERVO_REST_DEG = 90;    // arm clear of the switch
const int SERVO_OFF_DEG  = 150;   // presses the "off" side
const int SERVO_ON_DEG   = 30;    // presses the "on" side
```

Adjust until the arm **reaches the switch and flips it without stalling against it.** If
the servo buzzes and holds under load, back the angle off by 10–15°.

**No servo available?** Set `#define USE_SERVO 0` and use the relay on D7 to switch a lamp.
The demo story is identical — the loop still closes physically.

---

## Step 10 — Close the loop end to end (15 min)

Everything is now in place. Test the full path.

1. Make sure the publisher is running with `actuator source: mcu_serial`.
2. Create the waste condition — walk away with the load on, or from the PC:
   ```bash
   curl -X POST http://localhost:8000/api/presence -H "Content-Type: application/json" \
     -d '{"presence":"away","distance_m":2400}'
   curl -X POST http://localhost:8000/api/load -H "Content-Type: application/json" \
     -d '{"key":"living/ac","state":"on","watts":1100}'
   ```
3. Wait one 5-second evaluation cycle. A **critical** card appears on the dashboard and
   the phone buzzes.
4. **Tap "Approve & turn it off" on the phone** (or "Apply" on the dashboard).

✅ **Gate 1 — the servo physically moves and the switch flips.**
✅ **Gate 2 — on the board:**
```
[uno_q] COMMAND ac -> off (reco r2-living-ac)
[uno_q] sent to MCU: b'CMD ac off\n'
[uno_q] MCU ack: {'ack': 'ac', 'state': 'off', 'ok': True}
```
✅ **Gate 3 — on the PC:** `[mqtt] actuator confirmed living/ac -> off via mcu_serial ok=True`
✅ **Gate 4 — on the dashboard:** "Realized by acting" turns green, the card reads
**"✓ Applied — saving realized"**, and total power drops.

**This is the demo.** Rehearse it until it is boring.

### Then prove the safety gate — your best 30 seconds

Warm the DHT22 (hold it, or breathe on it) until the room reads above 27 °C, or inject it:

```bash
curl -X POST http://localhost:8000/api/sensor -H "Content-Type: application/json" \
  -d '{"room":"living","occupancy":false,"lux":110,"temp_c":29.5,"humidity":52}'
```

Now tap Approve again on the A/C recommendation.

✅ **Gate: the system refuses.** HTTP 409, and an amber note under the card:
*"Refused: living is 29.5 °C, above the 27 °C comfort limit — turning the A/C off would
make the room uncomfortable."*

**Nothing moves.** The servo does not fire. That is R7 gating the actuator, and it is the
most persuasive thing you will show.

---

## Step 11 — Fallback: run the bridge on the PC instead

If Step 6 found no usable serial path on the Linux side, or the Linux side fights you,
stop fighting. Leave the UNO Q as a USB-attached MCU and run the bridge on the PC:

```bash
# on the PC, board connected by USB
python arduino/bridge.py --broker localhost
# it auto-detects the port; override with --port COM5 if needed
```

The hub sees **identical** MQTT traffic. Sensing works exactly as before.

**What you lose:** the "UNO Q is an independent network peer doing its own edge
processing" architecture point, and command-driven actuation over MQTT (the bridge is
sensor-only by design — keeping it under 120 lines was the point).

**To keep actuation in this mode**, drive the MCU directly from the PC over serial when a
recommendation is approved. The MCU protocol is the same three words: write
`CMD <load> <on|off>\n` to the COM port. That is a ~20-line addition to `bridge.py`, and
`01_LLM_PROMPT_PACK.md` §PROMPT 13 describes the exact behaviour to replicate.

---

## Step 12 — Plan B: demo with no board at all

If the board is unrecoverable, you still have a complete, honest demo. Everything except
the sensors and the servo is real:

```bash
mosquitto -c mosquitto.conf -v          # terminal 1
python hub/server.py                    # terminal 2
python hub/simulator.py --mode demo     # terminal 3
```

The scripted 90-second scenario drives the real rules engine, the real energy model, the
real LLM, and the real dashboard — including the Apply button and the R7 refusal.

**Say "simulated sensor feed" out loud.** The simulator prints a banner saying exactly
that, deliberately. Judges respect a declared simulation; they punish a concealed one.

---

## Troubleshooting, by symptom

| Symptom | Most likely cause | Fix |
|---|---|---|
| No serial output at all | wrong baud | 115200 on both ends |
| Garbled characters | baud mismatch | as above |
| `occupancy` always `true` | PIR hold time too long | turn the module's trimpot down |
| `lux` barely changes | divider miswired | 3V3 → LDR → A0 → 10 kΩ → GND |
| `temp_c` always 23.5 | DHT read failing → stub value | check the 10 kΩ pull-up; or set `USE_DHT 0` and say so |
| Sketch won't compile | `DHT.h` / `Servo.h` missing for Zephyr | set `USE_DHT 0` / `USE_SERVO 0` (Step 4) |
| `no MCU serial port found` | Linux cannot see the MCU | Step 6 decision tree |
| MQTT never connects | firewall, or mosquitto bound to localhost | port 1883 rule + `listener 1883 0.0.0.0` |
| Sensors arrive but **Approve does nothing** | **subscription lost** | look for `[uno_q] subscribed home/command/#`. Must be registered in `on_connect` — one registered at startup is silently dropped, and dropped again on every reconnect |
| MCU resets when the servo moves | servo on the 3V3 rail | give it its own 5 V supply, common GND |
| Servo buzzes and holds | stalling against the switch | back `SERVO_*_DEG` off by 10–15° |
| Two publishers fighting | duplicate MQTT client ID | one `uno_q_publisher.py` per room; kill strays |
| Dashboard blank, console `WebSocket 404` | plain `uvicorn` | `pip install "uvicorn[standard]" websockets` |
| `plink`/PuTTY connection refused or times out | board's IP changed (DHCP re-lease after reboot), or SSH service not up yet on the board | re-check the board's current IP; give the Linux side ~30s after boot before connecting |
| `plink` asks to accept a host key every time | board's SSH host key changed (re-flash, factory reset) | accept once interactively; if scripting, add `-hostkey` with the new fingerprint rather than `-batch`, which would silently fail closed |
| Board has no route to `<PC_IP>` | board and PC on different networks/VLANs, or PC's hotspot not actually active | confirm both devices show the *same* subnet in `ip addr` (board) and `ipconfig` (PC) |

**Debug in this order — it saves the most time:**

1. Serial monitor — is the MCU emitting correct JSON?
2. `mosquitto_sub -h <PC_IP> -t 'home/#' -v` — is anything on the bus?
3. `curl http://<PC_IP>:8000/api/state` — did the hub fuse it?
4. Browser console — is the WebSocket connected?
5. Only then read application logs.

**One process per client ID.** A second `server.py` or `uno_q_publisher.py` against the
same broker will evict the first — producing symptoms that look exactly like a
subscription bug. Kill stale processes before diagnosing anything else.

**Never type the board's Linux password on a command line where it can be logged or
appear in shell history** (e.g. `plink -pw <password> ...`). Let PuTTY/plink prompt for
it interactively, or set up key-based auth once and skip the password entirely.

---

## Bring-up checklist

Tick these in order. Do not skip ahead — each gate makes the next failure interpretable.

- [ ] **0** `smoke_test.py` → 38/38 on the PC; hub + broker running; `<PC_IP>` written down
- [ ] **1** `/quad-detect` confirms the board; SSH shell reachable via `plink`/PuTTY; `ping <PC_IP>` succeeds from the board
- [ ] **2** PIR, LDR, DHT22 wired (actuator not yet)
- [ ] **3** Minimal test sketch → clean JSON, PIR and LDR both respond
- [ ] **4** Library availability checked; `USE_DHT` / `USE_SERVO` flags set accordingly
- [ ] **5** Real sketch flashed → correct JSON shape, occupancy debounce works
- [ ] **6** Serial path on Linux identified (or fallback chosen); `paho-mqtt` + `pyserial` installed
- [ ] **7** Repo cloned (or `pscp`'d) onto the board; publisher with `--fake-serial` → **`subscribed home/command/#`** appears; dashboard updates
- [ ] **8** Publisher on real sensors → `actuator source: mcu_serial`; waving changes the dashboard
- [ ] **9** Servo/relay wired on its **own 5 V**; `CMD lights off` works from the serial monitor
- [ ] **10** Approve on the phone → **the servo flips a real switch** → saving booked as realized
- [ ] **10b** R7 refusal verified at 29.5 °C — nothing moves
- [ ] **Record a video of Step 10 and 10b immediately.** For Archetype E this footage is
      your insurance. Do it the moment it works, not on Thursday night.
