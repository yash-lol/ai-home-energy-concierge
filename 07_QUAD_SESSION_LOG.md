# QUAD setup & session log — AI Home Energy Concierge

**Purpose of this file — two jobs:**
1. **Golden reference.** If you (or an LLM) had to reproduce this entire
   setup from a completely fresh machine, everything needed is below, in
   order, with exact commands.
2. **Continuity.** If this Claude Code session is lost, compacted, or you
   move to a new one, this file is the full state dump — what's done, what
   broke and how it was fixed, what's blocked, and exactly what to do next.

Not part of the required README content — an internal working log (same
spirit as `CLEANUP_REMINDER.md`), so keep/remove it from the public repo at
your own discretion. **No secrets are written into this file** — every
credential is referenced by variable name and storage location only; see
"Secrets" at the bottom.

---

## 0. Resume point — read this first

> ### ⚡ LATEST STATE (2026-08-06) — read §18 before anything else
>
> **The network moved.** Everything is now on a home LAN, NOT the old
> `172.20.10.x` phone hotspot:
>
> | | |
> |---|---|
> | PC (broker + hub) | `192.168.86.34` |
> | UNO Q board | `192.168.86.51` (Wi-Fi SSID `ArtiFi`) |
> | `lights` — KL120 bulb "Bedroom light 2" | `192.168.86.49` |
> | `ac` — HS110 plug "Space heater" | `192.168.86.20` (HS110 = real energy metering) |
>
> MQTT goes over **Wi-Fi** now (`MQTT_HOST=192.168.86.34`), not the USB tunnel.
> `adb reverse tcp:1883` is still a valid fallback if the LAN path dies.
>
> **Modulino Buttons work** (§17): A = toggle bulb, B = toggle heater,
> C = rescan bus. LEDs show *device-confirmed* state.
>
> **⚠ The single worst trap on this project — read §18.1.** If the board looks
> alive but the hub gets nothing, suspect **CRLF in `board.env`** before
> anything else. It makes the publisher serve *invented* sensor data while
> looking perfectly healthy. Check first:
> ```bash
> adb shell "grep -c $'
' /home/arduino/ai-home-energy-concierge/code/arduino/board.env"
> ```
> Non-zero = that is your bug. Fix: `sed -i 's/
$//' board.env` and restart
> the publisher.

**Current position (2026-08-05): the full Archetype E loop is CLOSED and
verified on real hardware.** Approve on the hub → the actual Kasa bulb
physically goes dark, confirmed by its own energy meter (10.8 W → 0.0 W).

The breadboard plan in `06_UNO_Q_BRINGUP.md` Steps 2-5/9 is **obsolete** —
we had none of that hardware. See `08_HARDWARE_PIVOT_PLAN.md` for the
replacement architecture, and §14 below for what was actually built.

Verified working end to end:

| Piece | Status |
|---|---|
| Modulino Knob on Qwiic (`Wire1`, `0x3A`) | reads + writes, drives `temp_c` |
| MCU firmware (`arduino/sketch`) | 1 Hz JSON, provenance-stamped, no banner |
| MCU→Linux link | `tcp://127.0.0.1:7500` via `arduino-router` |
| Publisher on the board | MQTT + Kasa actuation + metered watts |
| Kasa KL120 bulb / HS110 plug | switch + **real measured watts** |
| Hub, rules, GenieX narration | unchanged, still 32/32 smoke tests |
| Sensor simulator | served at `/simulator` |
| R7 comfort guardrail | refuses at 29.5 °C with HTTP 409 |

**If picking this up cold**, run §0.1 below, then read §14.

### 0.1 Restart everything after a reboot

```bash
# 1. PC: broker + hub
cd ai-home-energy-concierge/code
"/c/Program Files/mosquitto/mosquitto.exe" -c mosquitto.conf -v > mosquitto.log 2>&1 &
.venv/Scripts/python.exe hub/server.py > hub.log 2>&1 &
curl -s http://localhost:8000/api/state      # expect mqtt_connected: true

# 2. Board: publisher (over ADB; no Wi-Fi needed for this step)
ADB="$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe"
$ADB devices                                  # expect one device
$ADB shell "cd /home/arduino/ai-home-energy-concierge/code/arduino \
   && set -a && . ./board.env && set +a \
   && MCU_SIGNALS= setsid nohup ~/energy-venv/bin/python3 -u uno_q_publisher.py \
      > /tmp/publisher.log 2>&1 < /dev/null &"
$ADB shell "grep -E 'ready|kasa' /tmp/publisher.log"
```

Expect `[kasa] lights -> …`, `[kasa] ac -> …`, and **`subscribed home/command/#`**.

**If the board's Wi-Fi dropped** (it will, on a new network):
```bash
$ADB shell "nmcli device wifi connect '<SSID>' password '<PASSWORD>' ifname wlan0"
$ADB shell "ip -4 addr show wlan0 | grep inet"
```
Then update `MQTT_HOST` and the `KASA_*_HOST` values in
`code/arduino/board.env` to match the new subnet (`kasa discover` on the PC
prints current IPs).

GenieX (the NPU LLM) also dies on reboot — restart it per §8:
```bash
"C:\Users\QCWorkshop22\AppData\Local\GenieX CLI\geniex.exe" serve &
curl -s http://127.0.0.1:18181/v1/models
```

---

## 1. Machines & network topology

| Machine | Role | Path |
|---|---|---|
| This PC | Copilot+ laptop, Snapdragon X Elite (X1E80100), 45 TOPS Hexagon NPU, 32GB RAM, Windows 11 | — |
| `QUAD-Client-main` | The QUAD client tooling repo — **not** the hackathon project | `C:\Users\QCWorkshop22\Downloads\QUAD\QUAD-Client-main` |
| `ai-home-energy-concierge` | The hackathon project (this repo), cloned as a **sibling** folder | `C:\Users\QCWorkshop22\Downloads\QUAD\ai-home-energy-concierge` |
| Arduino UNO Q board | Dragonwing QRB2210, Debian 13 (trixie), hostname `hec` | **reached over ADB/USB-C** (preferred) or SSH |

> **UPDATE (2026-08-05): use ADB over USB-C, not SSH.** The board exposes an
> `ADB Interface` (`USB\VID_2341&PID_0078&MI_00`), which gives a shell, file
> push, and `adb reverse` port tunnelling **without any network at all**. That
> makes every instruction below that depends on the board's IP optional.
> `adb` lives at `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`
> (installed from Google's official zip — winget's package failed its hash check).

**Network was unstable on this PC** — during setup it flapped across five
subnets (`192.168.4.x`, `10.73.51.x` Qualcomm Wi-Fi, `172.20.10.x` a phone
hotspot, `10.123.72.x`, and finally `192.168.86.x` home Wi-Fi "ArtiFi"). This
is exactly why the ADB path matters: it is immune to all of it.

If you do need the board's IP: `adb shell "ip -4 addr show wlan0 | grep inet"`.

---

## 2. Part A — QUAD-Client environment setup (done once, in `QUAD-Client-main`)

This is the prerequisite tooling setup that happened *before* the hackathon
project was even cloned. All commands below run from
`C:\Users\QCWorkshop22\Downloads\QUAD\QUAD-Client-main`.

### 2.1 Local hardware (already true, nothing to install)
Snapdragon X Elite X1E80100 · Adreno X1-85 GPU (4.6 TFLOPS) · Hexagon NPU
v73 (45 TOPS) · 31.6GB RAM · QAIRT SDK 2.43.0.260128. Confirm anytime with:
```bash
.venv/Scripts/python.exe -m quad_mcp_client.cli detect
```

### 2.2 UNO Q board connection (SSH via plink)
Board credentials live in `.env_boards` in this directory (gitignored,
**never committed** — copy from `.env_boards.example` if starting fresh).
Vars: `UNOQ_HOST`, `UNOQ_SSH_USER`, `UNOQ_SSH_PASSWORD`, `UNOQ_PLINK_PATH`,
`UNOQ_SSH_HOST_KEY_FINGERPRINT`.

- PuTTY was not installed → `winget install --id PuTTY.PuTTY -e` (plink.exe
  lands at `C:\Program Files\PuTTY\plink.exe`).
- **Gotcha:** that path has a space (`Program Files`) — if you write it into
  `.env_boards` **unquoted**, Bash's `source` silently word-splits it and
  the variable ends up empty with no error. Always quote:
  `UNOQ_PLINK_PATH="C:\Program Files\PuTTY\plink.exe"`.
- Host key fingerprint: unknown on first connect → run `plink` **without**
  `-hostkey` once, it prints the real fingerprint, paste that into
  `UNOQ_SSH_HOST_KEY_FINGERPRINT`. Changes on every board reflash.
- Standard connect pattern used everywhere in this session:
  ```bash
  set -a; source .env_boards 2>/dev/null; set +a
  "$UNOQ_PLINK_PATH" -ssh -pw "$UNOQ_SSH_PASSWORD" -batch \
      -hostkey "$UNOQ_SSH_HOST_KEY_FINGERPRINT" \
      "$UNOQ_SSH_USER@$UNOQ_HOST" "<command>"
  ```
  (`set -a; source .env_boards` must be re-run in **every** Bash tool call —
  shell state does not persist between calls.)

### 2.3 Board identity fix (real bug found and fixed)
`quad.toml`/`.env_boards` originally assumed this board was a Dragonwing
IQ-9075 (QCS9075). A live probe found `soc0/machine = QRB2210`,
device-tree model `"Arduino SA,Imola"` — it's actually the **Arduino UNO Q**
(QCS2210-family), not the IQ-9075. Fixed by renaming throughout:
- `quad.toml`: `[target.iq-9075]` → `[target.uno-q]`
  (`board=uno-q`, `soc=qcs2210`, `hexagon=v66`)
- `.env_boards` / `.env_boards.example`: `IQ9075_*` → `UNOQ_*`
- `CLAUDE.md`: "Board connect (IQ-9075)" → "Board connect (UNO Q)"
- `src/quad_mcp_client/local/capabilities.py`: added `qrb2210`/`qcm2290`
  aliases → `qcs2210` (the registry had **no** entry for the SoC string this
  board actually reports — `lookup_for_chipset('QRB2210')` returned `None`
  before this fix). Regression test added in
  `tests/test_local/test_capabilities.py`.

This board currently has **no on-device AI acceleration stack** — no
fastrpc/DSP skel, no SNPE, no onnxruntime, GPU reports as `rusticl` (Mesa
software, not real Adreno OpenCL). It's bare Debian, CPU-only, until/unless
that's provisioned separately.

### 2.4 NPU runtime provisioning (native ARM64 venv)
**Trap hit and worked around:** the default `.venv` here is x86_64 emulated
Python under Windows-on-ARM (Prism) — `platform.machine()` reports `AMD64`
even though the CPU is genuinely ARM64. NPU wheels need **native** ARM64
Python. Found one already installed:
`C:\Users\QCWorkshop22\AppData\Local\Programs\Python\Python311-arm64\python.exe`.

```bash
"C:\Users\QCWorkshop22\AppData\Local\Programs\Python\Python311-arm64\python.exe" -m venv .venv-arm64
.venv-arm64/Scripts/python.exe -m pip install --upgrade pip
.venv-arm64/Scripts/python.exe -m pip install qai-hub "onnxruntime-qnn>=1.17.0" onnxruntime-genai
```

Verify the NPU device is actually reachable (completing pip install is
**not** proof — must check `get_ep_devices()`):
```bash
.venv-arm64/Scripts/python.exe -c "
import os, onnxruntime_qnn as q
os.add_dll_directory(os.path.dirname(q.__file__))
import onnxruntime as o
o.register_execution_provider_library('QNNExecutionProvider', q.get_library_path())
devs = o.get_ep_devices()
print('QNN NPU device found:', any(d.ep_name=='QNNExecutionProvider' and str(d.device.type).endswith('NPU') for d in devs))
"
# -> QNN NPU device found: True  (confirmed working)
```

### 2.5 AI Hub Models token
Stored in `QUAD-Client-main\.env` as `QAI_HUB_API_KEY` /
`QAI_HUB_API_TOKEN` (same value, two names — some tools read one, some the
other). Also configured into the SDK's own credential store:
```bash
.venv-arm64/Scripts/qai-hub.exe configure --api_token "<token>"
```
Verified live: `.venv-arm64/Scripts/qai-hub.exe list-devices` successfully
returned the full AI Hub Models device catalog, including the exact device
label needed for exports: **`Snapdragon X Elite CRD`**.

### 2.6 QUAD MCP server connectivity
Already configured and confirmed working — `claude mcp list` shows
`quad: https://quad.infra.foundries.io/mcp (HTTP) - ✔ Connected`. (That
command segfaults on exit after printing the correct status — a `claude` CLI
bug, unrelated to QUAD; ignore it.)

**Important, discovered via `hardware_detect`:** the MCP server itself runs
on a **virtualized `AMD EPYC 7B12` Ubuntu host**, `available_runtimes:
["cpu"]` only — it has **no real NPU**. Profiling a model directly against
the server would produce fake/simulated NPU numbers, not real ones. See §8
(Blocked) for what this affects.

Note also: `QUAD-Client-main` is **not a git repo** — it's an extracted
`-main` zip, not a clone. Not an issue for anything done here, just don't
expect `git status` to work in that directory.

---

## 3. Part B — Cloning the hackathon project

```bash
cd /c/Users/QCWorkshop22/Downloads/QUAD
git clone https://github.com/gowtham612/ai-home-energy-concierge.git
```
Clean clone, branch `main`, 4 commits at clone time (latest `051d735`).
`gh auth` is logged in as `gowtham612` (matches repo owner) via a token set
up in a parallel PowerShell session.

**Read-order for this repo's own docs** (per its `README.md`): start with
`CLEANUP_REMINDER.md` and `04_ORGANIZER_REQUIREMENTS.md` before anything
else. **Flagged, not yet acted on:** `04_ORGANIZER_REQUIREMENTS.md` §E
contains the venue Wi-Fi password and laptop login credentials in a
currently-public repo — `CLEANUP_REMINDER.md` already flags this as a
keep-or-redact decision for the team, not something touched automatically.

The authoritative external instructions are also mirrored from
`file:///C:/Users/QCWorkshop22/Downloads/Snapdragon Multiverse Hackathon_Internal.pdf`
(the organizer deck) — its QUAD section (pp. 28–37) matches everything set
up in Part A above.

---

## 4. PC hub environment (this repo's `code/` dir)

```bash
cd /c/Users/QCWorkshop22/Downloads/QUAD/ai-home-energy-concierge/code
"C:\Users\QCWorkshop22\AppData\Local\Programs\Python\Python311-arm64\python.exe" -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install psutil   # needed for Peak RSS in benchmark.py, not in requirements.txt
```
`requirements.txt` was already correctly pinned (`uvicorn[standard]`,
`websockets` — avoids the documented WebSocket-404-on-plain-uvicorn gotcha).

**Gate:**
```bash
.venv/Scripts/python.exe smoke_test.py
# -> 32/32 checks passed
```

---

## 5. MQTT broker

```bash
winget install --id EclipseFoundation.Mosquitto -e --accept-source-agreements --accept-package-agreements
```
Repo's own `mosquitto.conf` is already correct
(`listener 1883 0.0.0.0`, `allow_anonymous true` — needed so the phone/board
can reach it, the mosquitto default binds localhost only).

**Bug found and permanently fixed:** the winget install also auto-registers
a **Windows Service** running mosquitto with its own default config
(loopback-only bind on both `127.0.0.1` and `[::1]`). This coexisted
silently with our own console instance. The hub's default
`MQTT_HOST=localhost` resolved to `::1` and connected to the **wrong**
broker — `mqtt_connected: true` looked completely healthy while zero board
traffic ever arrived, no error anywhere. Diagnosed via `netstat -ano | grep
1883` (two listeners, two different PIDs) and `tasklist | grep mosquitto`.
**Now permanently resolved** — from an elevated PowerShell:
```powershell
Stop-Service mosquitto
Set-Service mosquitto -StartupType Disabled     # must be ONE line, not split across two
```
Confirmed via `Get-Service mosquitto` → `Status: Stopped`, `StartType:
Disabled`. `MQTT_HOST` can safely default to plain `localhost` again now —
there is only one mosquitto instance on this machine going forward.

To (re)start our broker:
```bash
cd /c/Users/QCWorkshop22/Downloads/QUAD/ai-home-energy-concierge/code
"/c/Program Files/mosquitto/mosquitto.exe" -c mosquitto.conf -v > mosquitto.log 2>&1 &
```

**Still outstanding (not blocking, just not done properly):** inbound
firewall rules for ports 1883/8000 could not be created — needs admin
rights. Not currently a problem (both ports tested reachable from the board
regardless — this network's Windows Firewall profile already permits it),
but for correctness, from an elevated PowerShell:
```powershell
New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -LocalPort 1883 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Hub HTTP 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

---

## 6. Hub server

```bash
cd /c/Users/QCWorkshop22/Downloads/QUAD/ai-home-energy-concierge/code
nohup .venv/Scripts/python.exe hub/server.py > hub_server.log 2>&1 &
```
(Now that the mosquitto service conflict is permanently fixed, plain
`localhost` default works — no need to pass `MQTT_HOST` explicitly anymore.)

Verify:
```bash
curl -s http://localhost:8000/api/state
# check "mqtt_connected": true AND (once something publishes) "rooms" actually populates —
# mqtt_connected:true alone is NOT sufficient proof, per the bug above.
```
Dashboard: `http://<PC_IP>:8000/` · Phone: `http://<PC_IP>:8000/phone`

---

## 7. UNO Q board — software side

```bash
set -a; source /c/Users/QCWorkshop22/Downloads/QUAD/QUAD-Client-main/.env_boards 2>/dev/null; set +a
PLINK="$UNOQ_PLINK_PATH"
```

**Installing deps — do NOT use pip.** The board's system Python is a PEP 668
externally-managed environment with **no `pip` module at all**
(`No module named pip`), and `python3 -m venv` also fails (`ensurepip` not
available, needs the `python3.13-venv` apt package, which itself needs
sudo). The clean fix is `apt` directly — both needed packages already exist
there:
```bash
"$PLINK" -ssh -pw "$UNOQ_SSH_PASSWORD" -batch -hostkey "$UNOQ_SSH_HOST_KEY_FINGERPRINT" \
    "$UNOQ_SSH_USER@$UNOQ_HOST" \
    "echo '$UNOQ_SSH_PASSWORD' | sudo -S apt install -y python3-serial python3-paho-mqtt"
```
(sudo password happens to be the same as the SSH login password on this
board — confirmed working, not guessed blind.)

Repo cloned directly onto the board (it has outbound internet):
```bash
"$PLINK" -ssh -pw "$UNOQ_SSH_PASSWORD" -batch -hostkey "$UNOQ_SSH_HOST_KEY_FINGERPRINT" \
    "$UNOQ_SSH_USER@$UNOQ_HOST" \
    "rm -rf ~/ai-home-energy-concierge && git clone https://github.com/gowtham612/ai-home-energy-concierge.git"
```

**Full MQTT loop proven** (before real sensors are wired) with the fake-serial publisher:
```bash
"$PLINK" -ssh -pw "$UNOQ_SSH_PASSWORD" -batch -hostkey "$UNOQ_SSH_HOST_KEY_FINGERPRINT" \
    "$UNOQ_SSH_USER@$UNOQ_HOST" \
    "cd ai-home-energy-concierge/code/arduino && ROOM=living MQTT_HOST=<PC_IP> timeout 8 python3 uno_q_publisher.py --fake-serial"
```
✅ Gate confirmed: `subscribed home/command/#` line present (the guide flags
this as the one most likely to silently break actuation later — must be
registered in `on_connect`), and — after the mosquitto bug fix above — the
hub's `/api/state` really does populate `rooms.living` with live data.

---

## 8. GenieX (on-device NPU LLM)

```bash
# Find + verify the exact Windows ARM64 installer (don't guess a URL — check the release):
gh release list --repo qualcomm/geniex --limit 5
gh release view v0.3.18 --repo qualcomm/geniex --json assets --jq '.assets[].name'
# -> geniex-cli-setup-windows-arm64-v0.3.18.exe (+ .sha256)

gh release download v0.3.18 --repo qualcomm/geniex --pattern "geniex-cli-setup-windows-arm64-v0.3.18.exe*"
sha256sum geniex-cli-setup-windows-arm64-v0.3.18.exe   # verify against the .sha256 file before running
```
Installed silently: `Start-Process <installer> -ArgumentList "/S" -Wait`.
Lands at `C:\Users\QCWorkshop22\AppData\Local\GenieX CLI\geniex.exe` (not on
PATH — invoke by full path).

```bash
GENIEX="C:\Users\QCWorkshop22\AppData\Local\GenieX CLI\geniex.exe"
"$GENIEX" --version                                          # v0.3.18, QAIRT Runtime v2.45.0.260326
"$GENIEX" pull ai-hub-models/Qwen3-4B-Instruct-2507           # 3.2 GB, takes several minutes
"$GENIEX" list                                                 # confirm: qualcomm/Qwen3-4B-Instruct-2507, W4A16
nohup "$GENIEX" serve > geniex_serve.log 2>&1 &                 # -> http://127.0.0.1:18181/
```
Verify:
```bash
curl -s http://127.0.0.1:18181/v1/models
curl -s -X POST http://127.0.0.1:18181/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"ai-hub-models/Qwen3-4B-Instruct-2507","messages":[{"role":"user","content":"Reply with exactly one word: OK"}],"max_tokens":10}'
```
Note: the pulled/registered model id (`qualcomm/Qwen3-4B-Instruct-2507:W4A16`)
does **not** exactly match `hub/llm.py`'s default `LLM_MODEL` string
(`ai-hub-models/Qwen3-4B-Instruct-2507`) — tested and confirmed **not a
problem**, GenieX ignores the `model` field when only one model is loaded.

`hub/llm.py` self-test confirms real LLM narration distinct from the
template fallback:
```bash
cd /c/Users/QCWorkshop22/Downloads/QUAD/ai-home-energy-concierge/code
.venv/Scripts/python.exe hub/llm.py
```

Real, measured benchmark with GenieX active (already written into
`code/README.md`'s performance table):
```bash
LLM_BASE_URL=http://127.0.0.1:18181/v1 .venv/Scripts/python.exe hub/benchmark.py --markdown
# Narration - local LLM: p50 3110.222 ms, p95 3272.678 ms
LLM_ENABLED=0 .venv/Scripts/python.exe hub/benchmark.py --markdown
# Peak RSS: 33.984 MB (deterministic control path)
```

---

## 9. Verified-working checklist

| Component | Status |
|---|---|
| `code/.venv` (native ARM64), `requirements.txt` | ✅ `smoke_test.py` 32/32 |
| Mosquitto broker (single instance now, service conflict resolved) | ✅ |
| `hub/server.py` | ✅ dashboard + `/api/state` responding |
| UNO Q board SSH + deps + repo clone | ✅ |
| Board → broker → hub MQTT loop (fake-serial) | ✅ `rooms.living` populated live |
| GenieX (`qairt`, W4A16, Qwen3-4B) | ✅ real narration + benchmark numbers |
| QUAD MCP server connectivity | ✅ (`quad: ... - ✔ Connected`) |
| `/quad-detect` (host + `uno-q` board target) | ✅ |
| `.venv-arm64` NPU runtime (QUAD-Client-main) | ✅ `QNN NPU device found: True` |
| AI Hub Models token | ✅ verified live via `qai-hub list-devices` |

---

## 10. Blocked — documented honestly, not faked

`/quad-profile` / `/quad-orchestrate` on a supplementary demo model (small
MNIST ONNX, `onnx/models` repo, verified as real binary content not an LFS
pointer before use) hit **two independent server-side infrastructure
failures** on the hosted QUAD MCP server (`quad.infra.foundries.io`) — not
fixable client-side, since only MCP tool-level access exists (no shell/SSH
to that server):

**1. QNN/SNPE target (`convert_model`, `target_sdk=qnn`):**
```
Error calling tool 'convert_model': qairt-converter failed (exit 1):
Traceback (most recent call last):
  File "/home/quad/work/QUAD/QUAD/sdks/v2.41.0.251128/lib/python/qti/aisw/dlc_utils/__init__.py", line 59, in <module>
    import libDlModelToolsPy as modeltools
ImportError: libpython3.10.so.1.0: cannot open shared object file: No such file or directory
[... full traceback shows a circular-import cascade from the same missing .so ...]
```

**2. ExecuTorch target (`convert_model`, `target_sdk=executorch`,
`runtime=qnn-htp`):** first call timed out; retry returned:
```
Error calling tool 'convert_model': No ExecuTorch checkout or installed package found. Fetch with `bash scripts/sdk_fetch.sh executorch`.
```

Both are genuine server environment bugs (broken Python shared-library dep;
toolchain never fetched) — raise verbatim at a QUAD office-hours session
(Tue/Thu 1:30–3:30, Fri 9–noon) if you want this filled in before
submission. **GenieX's real, measured NPU benchmark (§8 above) already
stands as valid "real NPU numbers" evidence** for the rubric — it's the
app's actual production AI path on this machine's genuine Hexagon NPU, not
an unrelated demo model, and was the deliberate choice over chasing the
broken profiling path further.

Also worth remembering: even if conversion had worked, profiling directly
against the MCP server would **still** not give real NPU numbers — see §2.6
(the server has no real NPU). Real on-device profiling needs the
client-side `quad-client profile-device --transport ssh --host <board-ip>`
path, which was considered and deliberately not pursued against the UNO Q
board since it currently has no working on-device NPU stack (§2.3) — would
likely just report "NPU unavailable," honest but not useful evidence.

---

## 11. Remaining — physical hardware, needs your hands

**Currently at Step 2/3** (see §0). Full remaining checklist from
`06_UNO_Q_BRINGUP.md`:

- [ ] **Step 2** — Wire sensors (actuator stays disconnected for now):
  | Function | Pin | Wiring |
  |---|---|---|
  | PIR motion `OUT` | D2 | VCC→5V, GND→GND, OUT→D2 |
  | LDR / photoresistor | A0 | 3V3 → LDR → A0 → 10kΩ → GND (divider) |
  | DHT22 `DATA` | D4 | VCC→3V3, GND→GND, DATA→D4, + 10kΩ pull-up DATA→VCC |
- [ ] **Step 3** — Flash this minimal test sketch via App Lab, verify at
      115200 baud: clean JSON once/sec, `"pir"` flips on wave, `"ldr"`
      changes substantially when covered:
      ```cpp
      const uint8_t PIN_PIR = 2;
      const uint8_t PIN_LDR = A0;
      void setup() { Serial.begin(115200); pinMode(PIN_PIR, INPUT); }
      void loop() {
        int pir = digitalRead(PIN_PIR);
        int raw = analogRead(PIN_LDR);
        Serial.print("{\"pir\":"); Serial.print(pir);
        Serial.print(",\"ldr\":"); Serial.print(raw);
        Serial.println("}");
        delay(1000);
      }
      ```
- [ ] **Step 4 (decision point)** — In App Lab's library manager, check
      whether `DHT sensor library` (Adafruit) and `Servo` are available for
      this Zephyr core. Set `#define USE_DHT` / `#define USE_SERVO` (0 or 1)
      at the top of `arduino/sketch/sketch.ino` accordingly. If Servo is
      unavailable, use the relay on D7 instead — actuation still happens
      physically either way.
- [ ] **Step 5** — Flash the real `sketch.ino`. Verify exact JSON shape
      `{"occupancy":bool,"lux":int,"temp_c":float,"humidity":float,"raw_pir":int}`
      and the ~30s occupancy debounce hold (deliberate, not a bug).
- [ ] **Step 6** — Confirm how Linux sees the MCU's serial
      (`ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null` over SSH) — our
      publisher auto-detects `/dev/ttyACM*`, the common case.
- [ ] **Step 8** — Swap `--fake-serial` for the real feed:
      `ROOM=living MQTT_HOST=<PC_IP> python3 uno_q_publisher.py` — verify
      `actuator source: mcu_serial`, not `simulated`.
- [ ] **Step 9** — Wire the actuator:
      | Function | Pin | Notes |
      |---|---|---|
      | Servo signal | D9 | |
      | Servo power | **its own 5V supply** | ⚠️ NOT the board's 3V3 rail — a stalling servo browns out the MCU |
      | Relay `IN` | D7 | or LED + 220Ω |
      | Buzzer `+` | D8 | |
      Test with `CMD lights off` from the serial monitor before involving
      MQTT at all.
- [ ] **Step 10** — Close the loop end-to-end: approve on phone/dashboard →
      servo physically flips a real switch → hub logs
      `[mqtt] actuator confirmed ...` → dashboard shows "Applied — saving
      realized".
- [ ] **Step 10b** — Verify the R7 safety-gate refusal at 29.5°C — nothing
      moves, HTTP 409. This is the single most persuasive demo moment.
- [ ] **Record video of Step 10 and 10b immediately once working** — your
      Archetype E insurance if anything breaks before the actual demo.

## 12. Also outstanding (not a technical blocker, just not done)

- `code/README.md` team table still has `<name>`/`<email>` placeholders —
  hard submission requirement.
- `CLEANUP_REMINDER.md`'s open decision: redact/remove internal planning
  docs (venue Wi-Fi + laptop credentials currently sit in
  `04_ORGANIZER_REQUIREMENTS.md`) before judges see the public repo.
- Every team member's feedback form, due Friday noon (gates prize
  eligibility).
- Firewall rules for 1883/8000 (§5) and NPU stack on the UNO Q board itself
  (§2.3) — both need admin/deeper provisioning, neither currently blocking
  anything.

---

## 13. Secrets — where they live, never repeated here

| Secret | Stored in | Notes |
|---|---|---|
| Board SSH password | `QUAD-Client-main\.env_boards` → `UNOQ_SSH_PASSWORD` | gitignored, also doubles as the sudo password on this board |
| Board sudo password | same as above | confirmed same value as SSH login |
| AI Hub Models token | `QUAD-Client-main\.env` → `QAI_HUB_API_KEY` / `QAI_HUB_API_TOKEN` | obtain from app.aihub.qualcomm.com → Account → API token |
| QUAD MCP bearer token | `QUAD-Client-main\.env` → `QUAD_MCP_TOKEN` | also in `.claude/settings.json` via `${env:...}` |
| Venue Wi-Fi / laptop login | `04_ORGANIZER_REQUIREMENTS.md` §E (in **this public repo**) | flagged in `CLEANUP_REMINDER.md`, redaction is your call |

If starting fully fresh with none of the above available, you'll need to
re-obtain: the board's current IP + SSH/sudo password (physical access to
the board), an AI Hub Models API token (app.aihub.qualcomm.com → Account →
API token), and confirm the QUAD MCP server URL/token from
`QUAD-Client-main\.claude\settings.json`.

---

# 14. Hardware pivot — what was actually built (2026-08-05)

The breadboard build never happened: we had no PIR, LDR, DHT22 or servo. What
we had was a **Modulino Knob**, a pile of **TP-Link Kasa** plugs/bulbs, and a
phone. That turned out better than the original plan — see
`08_HARDWARE_PIVOT_PLAN.md` for the reasoning, this section for the outcome.

## 14.1 Architecture as built

```
Modulino Knob ──I2C/Wire1──▶ STM32 MCU ──RPMsg──▶ arduino-router ──tcp:7500──▶ publisher
                                                                                   │ MQTT
Phone simulator ──HTTP /api/sensor──────────────────────────────▶ hub ◀────────────┘
                                                                   │ approve
                                                        home/command/living/lights
                                                                   │
                                              publisher ──python-kasa──▶ REAL BULB
```

## 14.2 Verified, with the evidence

- **Qwiic is on the MCU as `Wire1`** (I2C4, pins PD12/PD13). Proven by
  `code/arduino/scanner/scanner.ino`, which scans both buses:
  `0x3A Modulino KNOB` on `Wire1`, nothing on `Wire`.
- **Knob reads and writes.** Rotation, press-to-snap, and clamp-with-writeback
  all confirmed live.
- **Telemetry contract preserved.** The sketch emits e.g.
  `{"temp_c":21.9,"temp_src":"knob_sim",...,"nodes":"K"}` at 1 Hz with no boot
  banner, so `uno_q_publisher.py` needed no contract change.
- **Full loop closed.** Approve → `[uno_q] KASA lights -> off ok=True
  measured=0.0W` → the bulb read back `is_on=False, 0.0 W` (from 10.8 W).
  Hub recorded `source=kasa ok=True` and booked the saving as realized.
- **R7 guardrail refuses.** At 29.5 °C, `POST /api/apply` → **HTTP 409**,
  `"gate": "comfort_guardrail"`, nothing switched.
- **32/32 smoke tests still pass.**

## 14.3 Upgrade: power is now MEASURED, not modelled

The KL120 bulb and HS110 plug both report real instantaneous watts. Load
payloads now carry `"metered": true` and the audit line reads
*"Lights still ON at 11 W (measured by the smart plug)"*. This retires the
README's own stated limitation ("load power is modelled, not metered") for
those loads, and it is directly relevant to the 40-point Technical
Implementation criterion. Unmetered loads still fall back to the modelled
table and are marked `"metered": false` — the two are never conflated.

## 14.4 UNO Q platform facts worth knowing (each cost real time)

1. **`Serial` requires `Arduino_RouterBridge`.** The zephyr core ships a stub
   that hard-errors until you install it. Chain:
   `Arduino_RouterBridge → Arduino_RPClite → MsgPack →
   ArxContainer/ArxTypeTraits/DebugLog`.
2. **MCU `Serial` is not `/dev/ttyACM*`.** It goes over RPMsg to
   `arduino-router` (`/dev/ttyHS1`), which republishes on
   **`tcp://127.0.0.1:7500`**; a `socat` unit mirrors that to `/dev/ttyGS0`
   → USB CDC → a COM port on the PC. `find_port()` finds nothing here, by design.
3. **The board flashes itself.** `arduino-cli` is on the board and programs the
   STM32 over SWD (`linuxgpiod` bitbang). No App Lab GUI needed —
   compile + upload + read the monitor entirely over `adb`.
4. **The board has Wi-Fi** (`wlan0`, nmcli). It is simply *disconnected* out of
   the box. It also exposes **ADB over USB-C**, and `adb reverse tcp:1883` tunnels
   MQTT over the cable — immune to venue Wi-Fi.
5. **`tzdata` gap.** python-kasa parses the device timezone and Debian moved
   legacy zone names (`PST8PDT`) into `tzdata-legacy`. Without it every Kasa
   connect fails with `'No time zone found with key PST8PDT'`, which reads like
   a network error and is not. Fix: `pip install tzdata` inside the venv.

## 14.5 Bugs found and fixed this session

1. **Load publishing was accidentally coupled to sensor content.** An early
   `continue` for provenance-only telemetry lines skipped the Kasa load-poll
   block below it, so with `MCU_SIGNALS=` empty the hub received **zero loads**
   and no rule could ever fire. Sensor and load publishing are now independent.
2. **False `ok=False` on a successful switch.** TP-Link firmware serves one
   connection at a time; a concurrent reader makes the write raise
   `Communication error … transition_light_state` *even though the command
   landed*. `KasaBank.switch()` now retries and judges success by **reading the
   device back**, not by whether the write returned cleanly.
3. **Unquoted config values with spaces.** `KASA_AC_ALIAS=Space heater` in
   `board.env` made bash assign `Space` and then try to execute `heater`. Same
   class of bug as the `.env_boards` plink path earlier in this project. All
   values with spaces are now quoted, with a comment saying why.
4. **Stale honesty claim in the rules.** R3's evidence asserted "approximate lux
   from an uncalibrated photoresistor" — hardware we never had. It now reports
   its real source (`lux_src`, or `simulator`).
5. **UDP discovery is unusable here.** The ArtiFi mesh drops client-to-client
   broadcast, *and* a KL120 that is switched OFF stops answering discovery
   entirely — precisely the state you most need to verify after switching
   something off. All device access now uses `Device.connect(host=…)` over TCP.

## 14.6 Gotchas for whoever runs this next

- **Only one process should talk to a Kasa device.** The publisher polls them
  every `KASA_POLL_S`. Running `kasa` CLI or the phone app at the same time
  causes timeouts that look like faults but are contention. Query the hub
  instead of the device.
- **Run `smoke_test.py` with port 8000 free.** It starts its own hub; if another
  hub is already bound it can pick up that instance's state and fail spuriously
  (seen once: "total watts computed 0 W", which passed cleanly on a rerun).
- **`MCU_SIGNALS` decides who owns a signal.** The hub merges room state, so the
  last writer wins and the MCU's 1 Hz stream beats anything POSTed to
  `/api/sensor`. Default `temp_c` (knob owns temperature — the tactile R7 demo);
  set it empty to run entirely from the simulator.
- **`adb shell "... &"` hangs.** adb waits for the stream. Use
  `setsid nohup … > log 2>&1 < /dev/null &`.

## 14.7 Files changed

| File | Change |
|---|---|
| `code/arduino/scanner/scanner.ino` | **new** — dual-bus I2C scanner |
| `code/arduino/knob_test/knob_test.ino` | **new** — Knob smoke test |
| `code/arduino/sketch/sketch.ino` | rewritten for Modulino; provenance keys; print-only |
| `code/arduino/uno_q_publisher.py` | TCP source, partial payloads, `KasaBank`, metered watts, `MCU_SIGNALS` |
| `code/arduino/board.env.example` | **new** — all LAN/device config, env-overridable |
| `code/simulator/index.html` | moved from repo root; served at `/simulator` |
| `code/hub/server.py` | added the `/simulator` route |
| `code/hub/rules.py` | R3 evidence now states its real lux source |
| `code/requirements.txt` | `python-kasa`, `psutil`, Win-ARM `--only-binary` note |
| `code/README.md` | Hardware, Setup, Quickstart rewritten |

## 14.8 Still open

- `06_UNO_Q_BRINGUP.md` Steps 2-5 and 9 still describe the breadboard build and
  should be replaced or marked superseded by `08_HARDWARE_PIVOT_PLAN.md`.
- Team names/emails in `code/README.md` are still placeholders (**hard
  submission requirement**).
- `CLEANUP_REMINDER.md`'s decision on the venue credentials in
  `04_ORGANIZER_REQUIREMENTS.md` is still open.
- `/quad-profile` remains blocked by the two server-side failures in §10.
- Optional: a Modulino **Thermo** would make `temp_c` a real measurement and
  retire the last declared simulation in the sensing path.

---

# 15. Rebuilding the board from scratch

Nothing here is needed for a normal power cycle — the firmware lives in STM32
flash and the Wi-Fi profile lives in NetworkManager, so both survive unplugging.
This section is for a reflash, a factory reset, or a second board.

## 15.1 What survives a power cycle (do not redo)

| Thing | Survives? |
|---|---|
| Flashed MCU firmware | yes — in STM32 flash |
| Wi-Fi credentials | yes — NetworkManager profile |
| `~/energy-venv`, `~/Arduino/libraries`, the repo clone | yes — on the eMMC |
| The running publisher process | **no** — restart it, see §0.1 |
| `adb reverse` tunnels | **no** — re-run if using USB-only mode |

## 15.2 Full rebuild

```bash
ADB="$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe"   # from Google's official zip
$ADB devices                                             # expect one device

# 1. Network (skip if you will run USB-only via adb reverse)
$ADB shell "nmcli device wifi connect '<SSID>' password '<PASSWORD>' ifname wlan0"
$ADB shell "ip -4 addr show wlan0 | grep inet"

# 2. Arduino libraries.
#    WITH internet on the board this is one line - arduino-cli resolves the whole
#    dependency tree for you:
$ADB shell "arduino-cli lib install Arduino_Modulino Arduino_RouterBridge"
#
#    WITHOUT internet, clone them on the PC and push. 14 libraries, two chains:
#      Serial support : Arduino_RouterBridge -> Arduino_RPClite -> MsgPack
#                       -> ArxContainer, ArxTypeTraits, DebugLog
#      Modulino       : Arduino_Modulino -> VL53L4CD, VL53L4ED, Arduino_LSM6DSOX,
#                       Arduino_LPS22HB, Arduino_HS300x, ArduinoGraphics,
#                       Arduino_LTR381RGB
#    arduino-libraries/* and stm32duino/{VL53L4CD,VL53L4ED} and hideakitai/{MsgPack,
#    ArxContainer,ArxTypeTraits,DebugLog} on GitHub. Then for each:
#      git clone --depth 1 https://github.com/<org>/<lib>.git && rm -rf <lib>/.git
#      $ADB push <lib> /home/arduino/Arduino/libraries/<lib>
$ADB shell "arduino-cli lib list"        # expect 14

# 3. Python environment
$ADB shell "echo '<sudo-pw>' | sudo -S apt install -y python3-serial python3-paho-mqtt"
$ADB shell "python3 -m venv --system-site-packages ~/energy-venv"
$ADB shell "~/energy-venv/bin/python3 -m pip install python-kasa tzdata"
#   tzdata is NOT optional: python-kasa reads the device timezone and Debian moved
#   legacy zone names (PST8PDT) into tzdata-legacy. Without it every Kasa connect
#   fails with "No time zone found with key PST8PDT", which reads like a network
#   error and is not.

# 4. Code + config
$ADB shell "git clone https://github.com/gowtham612/ai-home-energy-concierge.git"
$ADB push code/arduino/board.env /home/arduino/ai-home-energy-concierge/code/arduino/board.env

# 5. Flash the firmware (the board programs its own STM32 over SWD)
$ADB push code/arduino/sketch /tmp/sketch
$ADB shell "arduino-cli compile --fqbn arduino:zephyr:unoq /tmp/sketch"
$ADB shell "arduino-cli upload  --fqbn arduino:zephyr:unoq /tmp/sketch"

# 6. Start the publisher — see §0.1
```

**Gate:** flash `code/arduino/scanner` first and read the monitor; expect a
Modulino address on `Wire1` (Knob = `0x3A`). If both buses are empty, fix the
Qwiic cable before going further.

## 15.3 With the board unplugged, what still works on the PC

Everything except physical actuation:

- hub, rules engine, GenieX narration, dashboard, `/simulator` — all fine
- `smoke_test.py` — 32/32 (it needs no hardware, and actually prefers the
  publisher stopped; see §14.6)
- **Kasa switching does NOT work.** The publisher that drives the smart devices
  runs *on the board*. With it gone, the hub still publishes
  `home/command/...` and returns HTTP 200, but nothing is listening, so no
  actuator confirmation ever comes back and no lamp moves. Watch for the missing
  `home/actuator/...` record rather than trusting the 200.

---

# §16 Approve returned HTTP 400 — root cause and three fixes (2026-08-05)

Reported: *"it gives the popup right. but if I approve, nothing happens, AC didnt
turn off. for lights, it was showing 0W in dashboard but simulator was showing it
as ON."* Wire log showed `APPLY r2-living-ac -> HTTP 400`, five times.

## 16.1 The bug

`code/simulator/index.html` rendered one Approve button per entry in `r.actions`
and sent that entry as the `action` field of `POST /api/apply`:

```js
var actions = (r.actions && r.actions.length ? r.actions : ["off"]);
... data-action="'+esc(a)+'">Approve: switch '+esc(a)+'
post("/api/apply", { reco_id:recoId, action:action, ... })
```

But `r.actions` is **human-readable advice** — `"Turn off the ac"`, `"Set an away
temperature"`, `"Link HVAC to geofence"` — and `hub/server.py` accepts only:

```python
action = body.get("action", "off")
if action not in ("on", "off"):
    return JSONResponse({"error": "action must be 'on' or 'off'"}, status_code=400)
```

So every approval was rejected. Two aggravating details:

- `/api/apply` derives the target from the **rule** (`load_key = f"{rec.room}/{_load_from_rule(rec)}"`),
  never from `action` — so all three advice strings mapped to the *same single
  operation*. Rendering three buttons was meaningless.
- "Approve: switch Link HVAC to geofence" advertised an action the system cannot
  perform. Worse than a failure: a false capability claim in front of judges.

**Fix:** advice renders as text; one `Approve — switch off` button per card
carrying the real API verb. Proven against the live hub:

```
OLD  action='Turn off the ac'  -> HTTP 400
NEW  action='off'              -> HTTP 200
```

## 16.2 Failed actuations were still booked as savings

`api_apply` books the saving the instant the command is published. Nothing ever
revised it when the confirmation came back `ok=false`, so an unreachable lamp
still showed **"you saved $0.0023"**. `server.py`'s own docstring says
*"USER APPROVES -> physically act -> confirm -> book the saving as realized"* —
the code did not implement its fourth step.

`StateStore.record_actuation()` now un-books on `ok=false` from a real device and
re-arms the recommendation (the problem is unsolved, so it should be re-raised).
`source="simulated"` is a *declared, labelled* path and stays booked. Verified:
`realized $0.00232 -> $0` on a `kasa_error` confirmation.

## 16.3 Duplicate recommendation cards

`RECO_COOLDOWN_S` throttles re-*narration*, but once it lapsed the same finding id
was appended to `STORE.recos` again, and `recos[-12:]` rendered each copy as its
own card — `r3-living-daylight` appeared three times. New `StateStore.latest_recos()`
keeps the append-only history for the audit trail but surfaces each id once, newest
wording first. Used by both `/api/state` and `/api/recos`.

## 16.4 Simulator fighting the real device

The simulator POSTed simulated state for loads backed by a Kasa device; the
publisher re-published true state 5 s later. That is the exact "simulator says ON,
dashboard says 0 W" divergence. Metered loads are now labelled **real device** and
left to the hardware. Also: the read-back line read `l.source || l.src` — keys the
hub never sends. It sends **`metered`** (bool).

Commit `a7b5efd`, pushed. A teammate had meanwhile switched the licence to MIT
(`0180160`); rebased cleanly, no overlap.

## 16.5 ⚠ Network state at time of writing — the Kasa half is DOWN

Not a code fault. Discovered while verifying:

| Thing | State |
|---|---|
| **ArtiFi** (phone hotspot) | **not broadcasting** — absent from a board-side rescan |
| Bulb *Bedroom light 2* (`172.20.10.7`) | off-network, was on ArtiFi |
| Heater plug (`172.20.10.5`) | **broadcasting `TP-LINK_Smart Plug_26B1` at 100%** — it has fallen back to its own setup AP, i.e. **unprovisioned** |
| Board `wlan0` | `NO-CARRIER`, `state DOWN`, no IPv4 |
| Board MQTT | **fine** — rides USB via `adb reverse tcp:1883` |
| Knob telemetry | **fine** — 21.9 °C, `temp_src=knob_sim`, 1 Hz |
| PC | HaQathon |

Board-side saved Wi-Fi profiles: `ArtiFi`, `Nanda's S26`, `QGuest`.
Visible and known-good: `Hydra`, `Qguest`, `HaQathon`.

**To restore the physical-actuation demo**, all three must land on one LAN:
1. Bring up a network the Kasa devices are on (simplest: re-enable the ArtiFi
   hotspot — bulb and board both have saved credentials for it).
2. The heater plug needs **re-provisioning** through the Kasa app — it is in
   setup mode, it will not rejoin on its own.
3. Then `python code/tools/reconfigure_network.py` to rediscover IPs and redeploy.

Until then the loop still closes end-to-end and degrades **honestly**:
`source=kasa_error, ok=false`, and — as of §16.2 — no saving is claimed.

---

# §17 Modulino Buttons as a Kasa debug path + Risk V-2 SETTLED (2026-08-05)

A Modulino Buttons node was daisy-chained off the Knob. Asked for: two buttons
that directly toggle the bulb and the heater, as a hardware check that the Kasa
path works through the UNO Q without the browser in the way.

## 17.1 ⭐ Risk V-2 is settled: host -> MCU serial WORKS

Open since the first bring-up and the reason `MCU_ACCEPTS_COMMANDS` shipped as
`0`: nobody had verified the UNO Q's Bridge/RPC path delivers **Linux -> MCU**
bytes at all. It does. Writing to the same `tcp://127.0.0.1:7500` monitor socket
that carries telemetry the other way:

```
--> CMD lights on ok
<-- {"ack":"lights","state":"on","ok":true,"via":"pixels"}
```

`MCU_ACCEPTS_COMMANDS` is now **1**. This is what lets the button LEDs show what
the *device reported* rather than what was *asked for* — for a debug tool the
difference is the whole point.

## 17.2 Button map

| Button | Action | LED |
|---|---|---|
| **A** | toggle Kasa bulb (`lights`) | LED0 = bulb **confirmed** on |
| **B** | toggle Kasa plug (`ac` / heater) | LED1 = plug **confirmed** on |
| **C** | rescan the Qwiic bus (hot-plug) | LED2 = last action failed / never confirmed |

Chirps: 660 Hz = press taken, awaiting device · 880 = confirmed on ·
440 = confirmed off · 196 = failed, or no confirmation within `ACK_TIMEOUT_MS`
(12 s).

Occupancy override moved off button A to the simulator UI, alongside the other
declared-simulation inputs.

## 17.3 Why counters, not edges

The MCU cannot reach a Kasa device — it has no network. So a press bumps a
monotonic counter (`bl`, `ba`) carried in every telemetry line;
`uno_q_publisher.py`'s new `ButtonWatch` acts on the **delta** since the line it
last saw, then writes `CMD <load> <on|off> <ok|fail>` back.

A delta survives what an edge flag does not:

- **dropped line** — the count still climbs, the press is not lost
- **duplicate line** — no change, so no double-switch
- **publisher restart** — first sight BASELINES only; without this every press
  since MCU boot would replay on each restart
- **MCU reflash** — counter resets to 0, i.e. *decreases*; treated as a reboot
  and re-baselined rather than actuated (a real wrap needs 65535 presses)

Seven cases unit-tested offline (baseline / single / repeat / even delta / odd
delta / reset / unbound) — all pass.

## 17.4 A bug this shook out

`KasaBank.toggle()` first read current state via `poll()` and returned
`"unbound"` when that came back empty. But TP-Link firmware **serves one
connection at a time**, so the publisher's own 5 s sweep (or the phone app) can
lock a read out for a moment — and a bulb sitting right there got reported as
*"no Kasa device bound"*. Caught live: toggle 1 succeeded, toggle 2 said
`unbound` with the bulb plainly connected.

`self.devices` is the authority on binding; a failed poll is not. Now: retry
once, then report `kasa_error`, which is a different claim and gets a different
log line. The distinction reaches the MCU too.

## 17.5 Verified end to end

| Link | How | Result |
|---|---|---|
| Buttons node enumerates | `"nodes":"KB"` in telemetry | ✅ |
| Press -> counter | `"bl":0,"ba":0` present and live | ✅ |
| Counter -> toggle logic | 7 offline unit cases | ✅ |
| Toggle -> real bulb | `toggle('lights')` twice on the board | ✅ physically switched, `ok=True source=kasa` |
| Publisher -> MCU LED | `CMD` write, MCU acked | ✅ |
| Contention handling | concurrent poll + write | ✅ "write reported KasaException but the device IS off — trusting the device" |

**Only the physical press itself is unverified** — that needs a finger on the
button. Everything it depends on is proven.

## 17.6 Network note

ArtiFi came back mid-session; board is on `172.20.10.8`, bulb bound at
`172.20.10.7` (1.7 W, metered). The heater plug at `172.20.10.5` is still
**unprovisioned** — it was broadcasting its own `TP-LINK_Smart Plug_26B1` setup
AP. Until it is paired, **button B will chirp low and light the error LED**,
which is the correct honest answer, not a fault in the button path.

The hotspot is flaky: restoring the bulb took three discovery attempts before
one answered. Worth knowing before blaming code on demo day.

---

# §18 Session of 2026-08-05/06 — fixes, and the CRLF trap

## 18.1 ⚠⚠ THE CRLF TRAP — most dangerous bug in the project

**Symptom:** the board looks completely healthy — publisher running, both Kasa
devices bound, a plausible temperature updating once a second on the dashboard —
but the hub receives **nothing**, and the temperature you are watching is
**invented**. The real knob sat still at 21.9 °C while the dashboard showed a
drifting 22.5–23.6 °C.

**Cause:** `reconfigure_network.py` wrote `board.env` from Windows in text mode,
so every `\n` became `\r\n`. `board.env` is sourced by **bash on the board**,
which keeps the CR as part of the value:

```
MCU_TCP_HOST = '127.0.0.1\r'
MQTT_HOST    = '192.168.86.34\r'
```

Both hostnames are invalid. The MCU connect fails → publisher falls back to
synthetic data. The MQTT connect fails → nothing reaches the hub. Nothing errors
out; it just quietly lies.

**This has now happened twice.** Defended twice as of `42b290a`:
1. the tool writes with `newline="\n"`
2. it runs `sed -i 's/\r$//' board.env` on the board after pushing

**Diagnose in one command:**
```bash
adb shell "grep -c $'\r' /home/arduino/ai-home-energy-concierge/code/arduino/board.env"
```
Non-zero → this is your bug.

## 18.2 Two hardening changes that came out of it

- **The MCU probe was one-shot.** `arduino-router` restarts with the sketch and
  takes seconds to return after a re-plug; losing that race committed the
  publisher to synthetic data *for its whole lifetime*. Now polls for
  `MCU_WAIT_S` (default 30 s, env-overridable), and the fallback prints a banner
  saying plainly that every value below is invented.
- **`fake_lines()` declared nothing.** The hub merges room state with
  `{**prev, **payload}`, so a `temp_src:"knob_sim"` from an earlier real run
  **survived** while invented values overwrote `temp_c` — a synthetic reading
  wearing the physical knob's label. Now stamps `*_src="synthetic"` on every
  line. **A generated value must never inherit a measured value's label.**

## 18.3 Simulator + dashboard (`ca725c6`)

- **Approve returned HTTP 400 on every recommendation** (§16): the card sent
  `r.actions` prose ("Turn off the ac") as the API `action`, which only accepts
  `on`/`off`. One button per card now sends the real verb.
- **Websocket reconnect loop.** `connectWS()` closed the old socket, which fired
  *its* `onclose`, which scheduled another `connectWS()`; `onopen` reset
  `wsRetry=0` so backoff never grew. One blip → permanent 1 Hz loop flooding the
  log. Handlers are now detached before closing. Logging follows real
  transitions: one line on connect, one per genuine drop, one on recovery.
- **Dashboard now labels provenance.** Real and simulated loads always shared the
  table but rendered identically with a literal `—` in the Detail column. The hub
  has always sent `metered`; the dashboard never read it. Now shows
  "real device · measured" vs "simulated · modelled".
- **Failed actuations no longer book a saving** (§16.2), and duplicate
  recommendation cards are collapsed (§16.3).

## 18.4 Git identity corrected

All commits were authored as `Chris <hl3838@columbia.edu>` — the machine's global
git identity, unrelated to the team. The *push* credential was always correct
(`gh auth status` → `gowtham612` active, `repo` scope); author identity and push
credential are independent, which is why pushes succeeded while commits read as
someone else.

- repo-**local** identity set to `gowtham612 <gowthamraj.b@gmail.com>` (global
  left alone — shared machine)
- all 18 commits rewritten and force-pushed with `--force-with-lease`
- verified via the GitHub API: **18/18 → `author=gowtham612`**, zero unlinked
- content untouched (`git diff pre-author-rewrite HEAD` empty)
- safety net kept **locally, unpushed**: tag `pre-author-rewrite` and branch
  `backup-before-author-rewrite` at the original `ab1feec`

**SHAs all changed.** Anyone holding a clone must
`git fetch && git reset --hard origin/main` — a plain `git pull` will try to
merge the old history back.

## 18.5 State at handoff

| Piece | State |
|---|---|
| Board | `192.168.86.51`, USB attached, publisher running |
| MCU | real knob, steady **21.9 °C**, `temp_src=knob_sim`, `"bl":2` |
| MQTT | `connected rc=0`, ~5 msgs / 10 s |
| `lights` | KL120 `192.168.86.49`, on, **1.7 W metered** |
| `ac` | HS110 `192.168.86.20`, on, 0.0 W metered |
| Hub / broker | up on `192.168.86.34`, dashboard `/`, simulator `/simulator` |
| Repo | clean, in sync, `42b290a` |

Cosmetic leftover: `living/dryer` sits in the load map at off/0 W from a
mixed-provenance test. Harmless; clear it by restarting the hub.

## 18.6 Still open

- **`demo.mp4`** — not recorded. Slide 13 references it; renders a placeholder
  without it.
- **Venue credential redaction** — `04_ORGANIZER_REQUIREMENTS.md` §E.
- **Yash / Ajay workstreams** — assignments in the team table are guesses.
- **Feedback forms** — every member, gates prize eligibility.
- **Third smart plug** (`TP-LINK_Smart Plug_5307`, EP40, `192.168.86.30`) was
  being paired; not yet bound to a load.
- **Stale-data indicator** — the dashboard showed 86-minute-old readings as if
  live during this session. Proposed but NOT built: grey out anything older than
  ~15 s. Worth doing before the demo.

---

# §19 AI enhancement plan (09_AI_ENHANCEMENT_PLAN.md) — all six tasks done

All of P0-A → P3-F implemented, gated and pushed. `smoke_test.py` 32/32 after
every task.

## 19.1 ⚠ Run the gate with the board publisher STOPPED

`smoke_test.py` asserts `total_watts == 1340` from its own fixtures. The live
publisher republishes the REAL bulb (1.7 W) every 5 s and overwrites them, giving
a spurious **23/25**. It is interference, not a regression.

```bash
adb shell "pkill -9 -f '[u]no_q_publisher.py'"
python smoke_test.py            # 32/32
# then relaunch the publisher
```

## 19.2 The plan's latency budget was wrong, and why

Plan assumed ~3.1 s for GenieX. Measured: **11.4 s**, because latency tracks
**output length**, steeply:

| output | latency |
|---|---|
| 135 chars | 2.6 s |
| 378 chars | 5.5 s |
| 1060 chars | 11.4 s |
| ~600 tokens | **~135 s** |

`llm.py` asked for `max_tokens=300` with no brevity instruction, so the model
rambled to ~1060 chars and blew its own 8 s timeout — **every narration had been
silently falling back to the template**, while README quoted 3110 ms. Capping
output at 160 tokens + a brevity instruction + a 20 s timeout gives **2.3 s and
`narrated_by=llm`**: 5× faster *and* actually using the NPU. Approved by the user
before changing shared code.

## 19.3 Measured, on real silicon

| Tier | Where | Measured |
|---|---|---|
| 1 · edge anomaly | **UNO Q A53**, pure Python | **30.6 µs** p50 |
| — provenance check | hub | 110 µs |
| — rules engine | hub | 0.014 ms |
| 2 · narration | Hexagon NPU | 3.33 s |
| 2 · plan synthesis | Hexagon NPU | 11.5 s, **per change** |
| 3 · Q&A | Hexagon NPU | ~3.4 s, first token 2.4 s |

Anomaly model: 14 simulated days, 840 samples, **holdout 0.9714**, precision
0.947, recall 0.900, seed 20260806. **Training data is SIMULATED** — stated in
the model file, in `model_provenance()`, and in every evidence line.

## 19.4 Two bugs the rehearsal caught — read these

**(a) A learned finding switched the WRONG DEVICE and skipped the safety gate.**
`server.py::_load_from_rule` maps `rule_name` → load and defaulted to `"lights"`.
A learned finding carries `rule_name="learned_anomaly"`, absent from that table,
so `learned-living-ac` resolved to `living/lights`. Approving it would have
published `home/command/living/lights` — and the comfort guardrail, which keys
off the load name, saw "lights" and allowed it at 29.5 °C. Two failures from one
silent default. Fixed by carrying the Finding's real `load_key` onto the
Recommendation; the six mapped rules are byte-identical. **Now HTTP 409.**

**(b) `detector` died at the narration boundary.** It lived on the Finding but
not the Recommendation, so every dashboard card looked equally rule-derived.
Now carried through and rendered as a `rule` / `learned · 0.999` badge.

## 19.5 §5 rehearsal result

| Step | Result |
|---|---|
| 1 · rule finding ranked by planner | ✅ `planned_by=llm`, provenance verified |
| 2 · learned finding at 3 AM | ✅ score 0.805, tagged `learned` |
| 3 · approve → real bulb dark | ✅ 1.7 W → 0.0 W, `source=kasa ok=True`, booked |
| 4 · R7 refusal | ✅ **HTTP 409** (after fixing 19.4a) |
| 5 · /ask on the NPU | ✅ 4.1 s, `PROVENANCE=VERIFIED` |
| 6 · GenieX dead | ✅ all paths degrade to `template`, still functional |

Step 6 used an unreachable `LLM_BASE_URL` rather than killing the user's GenieX
service — same timeout/exception path, no risk of leaving the demo broken.

## 19.6 The provenance verifier earned its place

It caught the model doing forbidden arithmetic, unprompted, during development:

```
Q: "What if I shift the dryer to 9 PM?"
A: "...reducing cost from $0.39 to $0.19, saving $0.20."   -> UNVERIFIED [0.19]
```

`$0.39` was in the digest; `$0.19` was not. Nobody anticipated that specific
failure — the check found it. Cost: 110 µs.

## 19.7 Flags

All OFF by default. `AI_ANOMALY=1` `AI_PLAN=1` `AI_ASK=1`. Q&A page at `/ask`.

---

# §20 Dataset, learned occupancy, and the declined-decisions panel (2026-08-06)

Branch **`ml-dataset-and-models`**. Additive: nothing on the existing critical path
changed behaviour. `smoke_test.py` is now **38/38** (was 32/32 — six new checks; the
count is quoted in the deck and eight docs, all swept, and `presentation.html` was
rebuilt from its template).

## 20.1 Nothing was being recorded

`power_history` was a 60-entry deque — five minutes — and everything else died with
the process. No audit trail of decisions, and no substrate to learn from.

`hub/recorder.py` appends JSONL: one row per evaluation tick plus a row for every
finding, approval, guardrail refusal, guardrail veto, hardware confirmation and
dashboard thumb. Wired into `server.py` at six points. It swallows its own
exceptions and disables itself after five consecutive write failures — a dataset is
worth less than a working demo.

Rows carry the `*_src` stamps and `metered` flags plus a computed provenance summary,
so a model trained on them can never be described as having learned from a real
household when it did not. `code/data/sessions/` is **gitignored** — the project
claims occupancy data never leaves the house.

`tools/build_dataset.py` joins findings to the tick that produced them and to their
outcome, then prints a verdict: below 30 positives it says outright that there is not
enough to train on. Labels: approval = positive, refusal = hard negative, thumb =
explicit, untouched card = weak negative at weight 0.25 and marked.

## 20.2 Learned occupancy — UCI 357, shadow by default

There is no occupancy sensor; occupancy was a toggle. `hub/occupancy_model.py` infers
it from temperature, humidity and lux, trained by `tools/train_occupancy.py` on the
UCI Occupancy Detection benchmark (Candanedo & Feldheim, 2016).

| Variant | Features | datatest | datatest2 |
|---|---|---|---|
| `full` | temp + humidity + lux | 0.9786 | 0.9884 |
| `no_light` | temp + humidity | 0.8608 | 0.8452 |

**Plain Python — no numpy, no sklearn.** Inference is a dot product over three floats,
so it adds no dependency to a Windows-on-ARM machine and would run unchanged on the
A53. `python hub/occupancy_model.py` replays the held-out split through the inference
path and asserts it reproduces the trainer's number (2608/2665 = 0.9786, matched).

**Two things to say out loud before anyone asks:**

1. **Lux dominates.** Standardised weights: lux **+3.59**, humidity +0.76, temp −0.25.
   R3 already thresholds on lux, so inferred occupancy and R3 are *not* independent
   evidence. `no_light` exists for exactly that, and the weights print at training time.
2. **Our demo range is outside the training range.** UCI is an office at 19–23.2 °C /
   16.8–39.1 % RH; we drive 16–32 °C / 0–100 %. `predict()` returns `in_domain: False`
   there and the dashboard says *extrapolating*. **Do not quote 97.9 % while standing
   in a 29.5 °C simulated room.**

Default mode is **shadow** (`OCCUPANCY_MODEL=1`): predicts, displays beside the
reported value, drives nothing. Mode 2 fills a gap only when no other source supplied
occupancy — it never overwrites a reported value.

## 20.3 The declined-decisions panel — and the conflict beat for free

`r7_comfort_guardrail()` used to `continue` past a vetoed finding. It now tags and
returns it; `evaluate_all()` gives `(offered, suppressed)` while `evaluate()` keeps its
old contract exactly, so no existing caller changed.

Live-verified against a running hub:

```
Considered 2 · recommending 1 · declined 1 on safety grounds
DECLINED  away_with_hvac_on  $0.029 withheld   traded_for -> ['lights']
```

That `traded_for` is the conflict beat we discussed building separately — it turned out
to fall out of this for free. Both loads are wasting; the system declines the one that
costs comfort and takes the one that does not, and says so.

Vetoes record as their own row type, **never as a `refusal`**: a refusal is a human
asking and being told no, a veto is advice nobody saw. Only *transitions* are logged —
one veto row across many ticks, verified.

## 20.4 Verified

Ran in an isolated venv (this machine had no fastapi/paho):

| Check | Result |
|---|---|
| `smoke_test.py` | **38/38** |
| `hub/rules.py` self-test | R7 scenario now prints the withheld $0.319 |
| `hub/recorder.py` self-test | 7 row types, provenance, degrades on unwritable dir |
| `hub/occupancy_model.py` self-test | replay matches trainer accuracy |
| `tools/train_occupancy.py` | downloads, trains both variants, exports 5 KB JSON |
| `tools/build_dataset.py` | verdict fires correctly on a thin corpus |
| Live hub | veto panel, `traded_for`, shadow model, dataset counters all correct |

## 20.5 Still open

- **Dashboard rendering is unverified in a browser.** The HTML/CSS/JS changes
  (declined cards, 👍/👎, dataset tile, shadow line) were never loaded in a real
  browser — check before the demo.
- `code/simulator/index.html` deliberately **untouched** (it was being fixed in
  parallel). It does not yet render declined decisions or the thumbs.
- The corpus is thin. Leave the hub recording to build one.
- Everything from §18.6 remains open.

---

# §21 R8 — anticipation: the first rule that can still change the outcome (2026-08-06)

Same branch. `smoke_test.py` now **44/44** (was 38/38); docs and deck swept again.

## 21.1 Why this one matters

R1–R6 are all post-mortems: they report money already spent. Advice that arrives
after the money is gone is a receipt, not a recommendation. **R8
`peak_window_imminent`** is the mirror of R6 moved earlier — R6 says the dryer is
running inside the expensive window, R8 says it is *about to be*, in the 30 minutes
before the boundary, while you can still stop it.

That is the category change: **detected** waste becomes **avoided** waste.

Nothing is predicted. The tariff calendar is published and fixed, so the claim is
"the rate changes at 16:00 and this load is running" — no model, no guess, same
arithmetic standard as everything else. `energy_model.seconds_to_peak()` is the whole
mechanism.

## 21.2 Keeping a projection from looking like a bill

R8's figure *is* a projection — the dryer's remaining cycle time is unknowable, so it
bounds the claim at one hour of continued operation and says so in the formula:

```
3000 W x 3600 s projected = 3.0000 kWh; rate delta $0.58 - $0.32 = $0.26/kWh;
3.0000 kWh x $0.26 = $0.780 AVOIDABLE if shifted (projected, not yet incurred)
```

Everything downstream had to learn the distinction:

- `Finding.kind` / `Recommendation.kind` = `detected` | `anticipated`, carried through
  both narration paths.
- The **LLM prompt changes for anticipated findings** — it is told the waste has not
  happened yet and to write in the future tense. Left alone it would have written
  about money already spent, which is the exact opposite of the point.
- `realized_totals()` splits out `avoided_usd` / `avoided_count`. Money never spent
  and money recovered are both savings, but only one of them was measured.
- The dashboard card is green, badged **↑ BEFORE IT COSTS YOU**, says "avoidable"
  rather than "cost", and carries the line "projected, not yet incurred — this money
  is still yours".

## 21.3 Anticipated cards expire

Caught while live-testing: at 17:30 the 15:48 card was still on screen saying "you can
still shift it". True at 15:48, false at 17:30 — and worse than ordinary staleness,
because it advertises an action that no longer exists.

`latest_recos()` now drops an anticipated card once its rule stops firing, unless it
was acted on. Verified:

| virtual clock | cards |
|---|---|
| 15:48 | `peak_window_imminent:anticipated` |
| 17:30 | `peak_hour_heavy_load:detected` — R8 card gone, R6 took over |
| 15:50 | `peak_window_imminent:anticipated` returns |

Detected cards are deliberately left alone; their staleness is the broader open item
already logged in §18.6 and this was not the change to settle it in.

## 21.4 `POST /api/clock` — new

R8 only speaks in the 30 minutes before 16:00. The virtual-clock offset already
existed but was reachable **only** by publishing `home/context/clock` over MQTT — so
the one rule that most needs driving was the one rule the browser simulator could not
drive, and demoing it meant waiting for the real wall clock.

```bash
curl -X POST localhost:8000/api/clock -d '{"time":"15:48"}'   # or offset_s / reset
```

Same reasoning as `/api/sensor` and `/api/load`: the no-broker path has to be able to
exercise the whole engine.

## 21.5 Demo beat

1. `{"time":"15:48"}`, dryer preset on → green card, **$0.78 avoidable**, future tense.
2. Approve → booked as **avoided in advance**, not merely realized.
3. `{"time":"17:30"}` → the card is gone and R6 has taken over, charging the same
   dryer for money now actually spent.

The line: *"Every other card tells you what you spent. This one is the only one that
can still change the answer — and once the moment passes, it takes itself down."*

## 21.6 Verified / still open

44/44 smoke, every module self-test, and the full R8 lifecycle against a live hub.

Still open, unchanged from §20.5: **no dashboard change has ever been rendered in a
browser** — the anticipated card styling, declined cards, thumbs and dataset tile are
all verified only at the data layer. `code/simulator/index.html` remains untouched.

**The benchmark table is now stale**: the rules-engine row was measured with seven
rules. `benchmark.py`'s own label says eight; re-run `python hub/benchmark.py
--markdown` on the demo machine and paste. The README says this explicitly rather than
relabelling a number nobody re-measured.

---

# §22 Synthetic corpus + the relevance model, and an honest draw (2026-08-06)

Same branch. The dataset was empty — labels only exist when a human clicks, so an
unattended run gives thousands of ticks and no training signal.
`tools/generate_sessions.py` simulates a household instead.

## 22.1 It invents inputs, never outputs

Occupancy, presence, appliance use, daylight and temperature are fabricated. Every
situation is then fed through the **real** `rules.evaluate_all()` and the real
`template_narrate()`, so each row's rule, cost, formula and evidence are exactly what
the live system would have produced from that state. Sensor values carry
`*_src="synthetic"`, files are named `session-synthetic-*`, and `build_dataset.py`
reports the share automatically (`data provenance: contains_synthetic=446`).

## 22.2 The occupant has preferences the rules cannot see

If labels came from the rules, a model trained on them would re-derive R1-R8 and know
nothing. So the simulated occupant acts on HVAC, largely ignores lighting while home,
never acts 23:00-07:00, ignores phantom standby, responds to size, and is noisy about
one decision in eight.

The model recovered all of it from 90 simulated days (446 rows, 138 positives):

```
presence_away   +1.03    occupancy      -1.03    usd             +1.23
phantom_standby -0.54    hvac_window    +0.38    peak_imminent   +0.53
```

## 22.3 Two bugs that only appeared at volume

1. **Finding ids are STABLE.** `r2-living-ac` is the same string every time the A/C is
   left on, on any day — so an id is not a decision, an *occurrence* is. Both the
   generator and `build_dataset.py` deduplicated by id and collapsed a fortnight into
   **3 findings**. Occurrences are now separated by `EPISODE_GAP_S`, and outcomes are
   claimed by the nearest occurrence and then consumed. 14 days went from 3 findings
   to 66.
2. **The generator's household never left a light on.** Lighting was computed as
   `dark and occupied`, so lights were never on in daylight or in an empty room and
   **R1 and R3 could not fire once in a fortnight** — the two rules about lights, which
   is what the demo bulb is. Lighting is now a state machine with a
   forgets-on-departure probability.

## 22.4 The result, which is a draw

Chronological held-out split, measured against two baselines:

| | acc | prec | recall | F1 | AUC |
|---|---|---|---|---|---|
| majority | 0.679 | 0.000 | 0.000 | 0.000 | 0.500 |
| **by-cost** | 0.709 | 1.000 | 0.093 | 0.170 | **0.946** |
| learned model | **0.813** | 0.636 | 0.977 | **0.771** | 0.945 |

**The dollar figure the rules already compute is an excellent ranker and the model does
not beat it.** What it adds is a usable operating point — cost alone cannot be
thresholded without losing 91% of the positives.

`train_relevance.py` prints that comparison every run, flattering or not, and refuses
to train below 30 positives. An earlier version of the verdict only checked AUC and
reported "adds nothing", which was as wrong in the other direction; it now reports
ranking and deciding separately.

**Say the draw out loud.** "We built the learned tier, measured it against the
arithmetic we already had, and it did not improve the ranking" is a stronger position
than an unmeasured claim — and it is the same instinct as R7 refusing and the model
flagging itself out of domain.

## 22.5 State

- `data/sessions/` holds a 90-day synthetic corpus (~30 MB, gitignored).
- `hub/models/relevance_lr.json` committed alongside `occupancy_lr.json`; both record
  what they were trained on, the sample count and their caveats.
- `recorder.py` gained an overridable `clock` so generated rows carry simulated
  timestamps instead of all landing in one real second.
- Unchanged from §20.5/§21.6: **no dashboard change has been rendered in a browser**,
  and `code/simulator/index.html` is still untouched.
