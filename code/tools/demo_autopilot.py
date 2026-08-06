"""Autonomous demo driver — runs the demo beats with no hands on the hardware.

    python tools/demo_autopilot.py            # shot 1 (the default)
    python tools/demo_autopilot.py --no-browser
    python tools/demo_autopilot.py --check     # preflight only, changes nothing

WHAT THIS IS FOR
    Rehearsal. Every beat runs through the SAME path a human would drive, so if
    the wiring is broken this script fails instead of printing success:

      * the button "press" bumps the MCU's own counter (SIMBTN), so the switch
        happens because uno_q_publisher.py saw a counter delta and called Kasa —
        not because this script called Kasa. A script that shortcut to the Kasa
        API would still pass with the button path completely dead, which would
        make it worthless as a rehearsal.
      * simulator controls move via a cue the PAGE applies to its own widgets,
        so what you see on screen is the actual UI reacting.
      * the final check reads the BULB, not the hub's opinion of the bulb.

    The one thing it cannot reproduce is the physical switch contact and the
    two-poll debounce above it. Press the real button at least once before
    filming.

OUTPUT
    Each step prints what it is about to do, what it EXPECTS, and what actually
    happened. A step that cannot be verified fails loudly rather than moving on.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
HUB = os.environ.get("HUB_URL", "http://localhost:8000")
ADB = os.environ.get("ADB_PATH", str(Path(os.environ.get("LOCALAPPDATA", "")) /
                                     "Android/Sdk/platform-tools/adb.exe"))
BOARD_DIR = "/home/arduino/ai-home-energy-concierge/code/arduino"
MCU_TCP = ("127.0.0.1", 7500)

# The publisher polls Kasa every KASA_POLL_S (5 s) and only then sees the button
# counter move, so give a press a couple of poll windows before calling it dead.
PRESS_TIMEOUT_S = 25
SETTLE_S = 2.0

C_OK, C_BAD, C_DIM, C_HDR, C_END = "\033[92m", "\033[91m", "\033[90m", "\033[96m", "\033[0m"


def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


COLOUR = _supports_colour()


def c(text: str, colour: str) -> str:
    return f"{colour}{text}{C_END}" if COLOUR else text


class Step:
    """Prints intent -> expectation -> outcome, and tracks pass/fail."""

    n = 0
    failures = 0

    @classmethod
    def start(cls, title: str, expect: str) -> None:
        cls.n += 1
        print()
        print(c(f"[{cls.n}] {title}", C_HDR))
        print(c(f"    expect: {expect}", C_DIM))

    @classmethod
    def ok(cls, msg: str) -> None:
        print(c(f"    OK    : {msg}", C_OK))

    @classmethod
    def fail(cls, msg: str) -> None:
        cls.failures += 1
        print(c(f"    FAIL  : {msg}", C_BAD))

    @classmethod
    def info(cls, msg: str) -> None:
        print(c(f"            {msg}", C_DIM))


# ---------------------------------------------------------------- hub helpers

def hub_get(path: str, timeout: int = 20) -> Dict:
    with urllib.request.urlopen(HUB + path, timeout=timeout) as r:
        return json.loads(r.read() or "{}")


def hub_post(path: str, body: Dict, timeout: int = 40) -> Tuple[int, Dict]:
    rq = urllib.request.Request(HUB + path, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:
            return e.code, {}


def load_state(key: str) -> Dict:
    return (hub_get("/api/state").get("loads") or {}).get(key, {})


# -------------------------------------------------------------- board helpers

def adb(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)


def board_python(snippet: str, timeout: int = 120) -> str:
    """Run a python snippet on the board inside the env that has python-kasa.

    Base64 rather than quoting the source. `adb shell` hands the command to a
    remote shell, which does not expand \\n inside a double-quoted string — so a
    JSON-escaped multi-line snippet arrives as a single line of literal
    backslash-n and dies on the first statement. Encoding sidesteps every layer
    of quoting between here and the board at once.
    """
    import base64
    blob = base64.b64encode(snippet.encode("utf-8")).decode("ascii")
    inner = f"import base64;exec(base64.b64decode('{blob}').decode())"
    cmd = (f"cd {BOARD_DIR} && set -a && . ./board.env && set +a && "
           f"~/energy-venv/bin/python3 -c \"{inner}\"")
    r = adb("shell", cmd, timeout=timeout)
    if r.returncode != 0 and r.stderr.strip():
        Step.info(f"board stderr: {r.stderr.strip()[:160]}")
    return r.stdout.strip()


def command_load(load: str, action: str, room: str = "living") -> bool:
    """Switch a load by publishing the hub's own MQTT command.

    Deliberately NOT a direct Kasa call. TP-Link firmware serves one connection
    at a time and uno_q_publisher.py already holds it, polling every 5 s — so
    writing to the bulb from here loses that race repeatedly, and worse, an
    errored write can still land later and undo a subsequent step. An earlier
    version of this script did exactly that and produced a run where every step
    had an explanation and the end state was still wrong.

    Publishing `home/command/<room>/<load>` hands the work to the process that
    owns the connection, which serialises it against its own poll. It is also
    the same path the hub uses when a human taps Approve, so this exercises
    production code rather than a test-only shortcut.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        Step.info("paho-mqtt not installed — cannot publish a command")
        return False
    try:
        c = mqtt.Client(client_id="demo-autopilot")
    except Exception:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="demo-autopilot")
    try:
        c.connect(os.environ.get("MQTT_HOST", "127.0.0.1"),
                  int(os.environ.get("MQTT_PORT", "1883")), 30)
        c.loop_start()
        c.publish(f"home/command/{room}/{load}",
                  json.dumps({"action": action, "reco_id": "autopilot",
                              "approved_by": "demo_autopilot", "ts": time.time()}))
        time.sleep(1.0)
        c.loop_stop()
        c.disconnect()
        return True
    except Exception as exc:
        Step.info(f"MQTT publish failed: {type(exc).__name__}: {exc}")
        return False


def read_bulb(retries: int = 3) -> Optional[Dict]:
    """Read the bulb DIRECTLY, not via the hub.

    The whole point of the final check is independence: asking the hub whether
    the bulb is off only proves the hub believes it. This asks the device.

    Retried, because a read can also lose the one-connection race against the
    publisher's poll — and a verification step that flakes is worse than one that
    is merely slow.
    """
    for attempt in range(retries):
        got = _read_bulb_once()
        if got is not None:
            return got
        time.sleep(3.0)
    return None


def _read_bulb_once() -> Optional[Dict]:
    out = board_python(
        "import asyncio, json\n"
        "from kasa import Discover, Module\n"
        "async def m():\n"
        "    d = await Discover.discover_single('192.168.86.49', timeout=8)\n"
        "    await d.update()\n"
        "    w = None\n"
        "    try: w = d.modules[Module.Energy].current_consumption\n"
        "    except Exception: pass\n"
        "    print('BULB' + json.dumps({'on': bool(d.is_on), 'watts': w, 'alias': d.alias}))\n"
        "asyncio.run(m())")
    for line in out.splitlines():
        if line.startswith("BULB"):
            try:
                return json.loads(line[4:])
            except json.JSONDecodeError:
                return None
    return None


def set_bulb(on: bool, brightness: Optional[int] = None) -> Optional[Dict]:
    """Switch the bulb, then VERIFY by reading it back. Retries on contention.

    TP-Link firmware serves one connection at a time, and the publisher polls
    every 5 s — so a write from here can raise even though it landed, or be
    refused outright. The first version of this failed exactly that way: the
    turn_on errored, was reported as a failure, and then took effect a moment
    later, after the button press had already switched the bulb off. The result
    was a run where every individual step looked explicable and the end state was
    wrong.

    So: attempt, sleep, read back, and believe the DEVICE rather than the return
    value of the write. Same discipline as KasaBank.switch().
    """
    # 12 spaces: this sits inside `for -> try`, at the same level as the update
    # below it. At 8 it dedents out of the try block and the whole snippet is a
    # SyntaxError — which surfaced only as "bulb did not report on", because the
    # failure happened on the board and the parse error never reached the caller.
    b = ("            await d.modules[Module.Light].set_brightness(%d)\n" % brightness) if brightness else ""
    out = board_python(
        "import asyncio, json\n"
        "from kasa import Discover, Module\n"
        f"WANT = {bool(on)}\n"
        "async def m():\n"
        "    last = None\n"
        "    for attempt in range(4):\n"
        "        try:\n"
        "            d = await Discover.discover_single('192.168.86.49', timeout=8)\n"
        "            await d.update()\n"
        "            if bool(d.is_on) != WANT:\n"
        "                await (d.turn_on() if WANT else d.turn_off())\n"
        + b +
        "            await asyncio.sleep(1.8)\n"
        "            await d.update()\n"
        "            if bool(d.is_on) == WANT:\n"
        "                w = None\n"
        "                try: w = d.modules[Module.Energy].current_consumption\n"
        "                except Exception: pass\n"
        "                print('BULB' + json.dumps({'on': bool(d.is_on), 'watts': w,\n"
        "                                           'attempts': attempt + 1}))\n"
        "                return\n"
        "        except Exception as exc:\n"
        "            last = type(exc).__name__\n"
        "        await asyncio.sleep(2.5)\n"
        "    print('BULBFAIL' + json.dumps({'last_error': last}))\n"
        "asyncio.run(m())")
    for line in out.splitlines():
        if line.startswith("BULBFAIL"):
            return None
        if line.startswith("BULB"):
            try:
                return json.loads(line[4:])
            except json.JSONDecodeError:
                return None
    return None


def press_button(which: str) -> bool:
    """Simulate a Modulino button press by bumping the MCU's own counter.

    Writes SIMBTN over the router's monitor socket ON THE BOARD — the same
    socket the publisher reads telemetry from and writes CMD confirmations back
    through.
    """
    snippet = (
        "import socket, time\n"
        "s = socket.create_connection(('127.0.0.1', 7500), 5); s.settimeout(2)\n"
        "time.sleep(0.4)\n"
        f"s.sendall(b'SIMBTN {which}\\n')\n"
        "end = time.time() + 8; buf = b''\n"
        "while time.time() < end:\n"
        "    try: ch = s.recv(512)\n"
        "    except socket.timeout: continue\n"
        "    if not ch: break\n"
        "    buf += ch\n"
        "    while b'\\n' in buf:\n"
        "        line, buf = buf.split(b'\\n', 1)\n"
        "        t = line.decode('utf-8', 'ignore').strip()\n"
        "        if 'simbtn' in t:\n"
        "            print('ACK' + t); end = 0; break\n"
        "s.close()")
    out = board_python(snippet, timeout=60)
    return any(l.startswith("ACK") for l in out.splitlines())


def mcu_counters() -> Optional[Dict[str, int]]:
    """Current bl/ba counters, read straight off the telemetry stream."""
    snippet = (
        "import socket, json, time\n"
        "s = socket.create_connection(('127.0.0.1', 7500), 5); s.settimeout(2)\n"
        "buf = b''; end = time.time() + 6\n"
        "while time.time() < end:\n"
        "    try: ch = s.recv(512)\n"
        "    except socket.timeout: continue\n"
        "    if not ch: break\n"
        "    buf += ch\n"
        "    while b'\\n' in buf:\n"
        "        line, buf = buf.split(b'\\n', 1)\n"
        "        t = line.decode('utf-8', 'ignore').strip()\n"
        "        if t.startswith('{') and '\"bl\"' in t:\n"
        "            print('CNT' + t); end = 0; break\n"
        "s.close()")
    out = board_python(snippet, timeout=60)
    for line in out.splitlines():
        if line.startswith("CNT"):
            try:
                d = json.loads(line[3:])
                return {"bl": int(d.get("bl", -1)), "ba": int(d.get("ba", -1))}
            except Exception:
                return None
    return None


# ------------------------------------------------------------------- browser

def open_side_by_side(urls) -> bool:
    """Two browser windows, tiled left and right of the primary display."""
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
    except Exception:
        sw, sh = 1920, 1080

    half = sw // 2
    height = sh - 80

    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    # Close windows this script opened previously. They are identifiable by the
    # dedicated user-data-dir, so nothing of the user's own browsing is touched.
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
             "'*quad_demo_*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=30)
        time.sleep(1.0)
    except Exception:
        pass

    browser = next((p for p in candidates if os.path.exists(p)), None)
    if not browser:
        Step.info("no Edge/Chrome found — opening in the default browser, untiled")
        import webbrowser
        for u in urls:
            webbrowser.open(u)
        return False

    for i, url in enumerate(urls[:2]):
        subprocess.Popen([
            browser,
            f"--app={url}",                     # chromeless window, no tab strip
            f"--window-position={i * half},0",
            f"--window-size={half},{height}",
            f"--user-data-dir={Path(os.environ.get('TEMP', '.')) / f'quad_demo_{i}'}",
            "--no-first-run", "--no-default-browser-check",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
    return True


# ----------------------------------------------------------------- preflight

def preflight() -> bool:
    Step.start("Preflight", "hub up, board attached, publisher live on the real MCU")
    ok = True

    try:
        st = hub_get("/api/state", timeout=10)
        Step.ok(f"hub reachable at {HUB}  (mqtt_connected={st.get('mqtt_connected')})")
    except Exception as exc:
        Step.fail(f"hub not reachable at {HUB}: {exc}")
        return False

    if not Path(ADB).exists():
        Step.fail(f"adb not found at {ADB} — set ADB_PATH")
        return False
    if "\tdevice" not in adb("devices", timeout=30).stdout:
        Step.fail("no board over ADB — plug in USB-C")
        return False
    Step.ok("board attached over ADB")

    log = adb("shell", "tail -n 200 /tmp/publisher.log", timeout=30).stdout
    if "pgrep" not in log and not adb(
            "shell", "pgrep -f '[u]no_q_publisher.py'", timeout=30).stdout.strip():
        Step.fail("publisher is not running on the board")
        ok = False
    else:
        Step.ok("publisher running")

    if "PUBLISHING SYNTHETIC DATA" in log:
        Step.fail("publisher is on SYNTHETIC data — the MCU was not found. "
                  "Check CRLF in board.env, then restart it.")
        ok = False
    elif "MCU monitor connected" in log:
        Step.ok("publisher attached to the real MCU")

    counters = mcu_counters()
    if counters is None:
        Step.fail("could not read bl/ba counters from the MCU telemetry stream")
        ok = False
    else:
        Step.ok(f"MCU telemetry live — button counters bl={counters['bl']} ba={counters['ba']}")

    # Brightness is a filming concern, not a correctness one, so it warns rather
    # than fails. At 9% the bulb draws 1.7 W and "watts fall to zero" lands on
    # nobody; at 100% it is 10.8 W and visibly darker when it switches. Done here
    # because preflight is unhurried — mid-demo is the wrong time to fight the
    # publisher for the bulb's one connection.
    bulb = read_bulb(retries=2)
    if bulb is None:
        Step.info("could not read the bulb for a brightness check (non-fatal)")
    else:
        br = set_bulb(True, brightness=100)
        if br and br.get("watts") is not None and br["watts"] >= 8:
            Step.ok(f"bulb at full brightness — {br['watts']} W, a number that reads "
                    f"on camera (attempts: {br.get('attempts')})")
        else:
            Step.info("WARNING: could not confirm full brightness. If shot 2 shows "
                      "~1.7 W instead of ~10.8 W, set it by hand — see "
                      "10_DEMO_PLAN.md §9.1")

    return ok


# --------------------------------------------------------------------- shot 1

def shot1(browser: bool) -> None:
    print()
    print(c("=" * 74, C_HDR))
    print(c("SHOT 1 — hook: a button on the board, a real bulb goes dark", C_HDR))
    print(c("=" * 74, C_HDR))

    # -- 1. windows --------------------------------------------------------
    if browser:
        Step.start("Open simulator and dashboard side by side",
                   "two chromeless windows, simulator left, dashboard right")
        tiled = open_side_by_side([f"{HUB}/simulator", f"{HUB}/"])
        Step.ok("windows opened" + (" and tiled" if tiled else ""))
        Step.info("give them ~4 s to connect their websockets")
        time.sleep(4)

    # -- 2. a visible cue into the simulator's own UI -----------------------
    Step.start("Cue the simulator: presence -> away",
               "the Away button in the simulator visibly highlights and its wire "
               "log prints an AUTOPILOT line")
    # Proof the PAGE acted, not just that the cue was accepted: this script never
    # posts /api/presence itself. setPres() in the page does, as a side effect of
    # moving its own control. So if the hub's presence flips, the browser applied
    # the cue to a real widget. Without this check the step would print OK with
    # no browser open at all.
    # Force the OPPOSITE state first, or this proves nothing. On the second run of
    # the day presence is already "away" from the first, and "it is away now"
    # passes without the page doing anything at all. A check that can succeed
    # while the thing under test is dead is worse than no check.
    hub_post("/api/presence", {"presence": "home", "distance_m": 10})
    time.sleep(1.5)
    before_presence = (hub_get("/api/state").get("user") or {}).get("presence")
    if before_presence != "home":
        Step.fail(f"could not establish a known starting state "
                  f"(presence is {before_presence!r}, wanted 'home')")
        return
    Step.info("forced presence to 'home' first, so the flip below can only come "
              "from the page — this script posts nothing else after this point")

    code, _ = hub_post("/api/demo/cue", {"control": "presence", "value": "away",
                                         "note": "autopilot: shot 1"})
    if code != 200:
        Step.fail(f"cue rejected, HTTP {code}")
    else:
        applied = False
        for _ in range(10):
            time.sleep(1.2)
            if (hub_get("/api/state").get("user") or {}).get("presence") == "away":
                applied = True
                break
        if applied:
            Step.ok("presence is now 'away' — the SIMULATOR PAGE moved its own "
                    "control and posted it. The UI really reacted.")
        elif not browser:
            Step.info("no browser open (--no-browser), so nothing could apply the "
                      "cue — not counted as a failure")
        else:
            Step.fail("cue was accepted but the page never applied it — is the "
                      "simulator window actually open and connected?")

    # -- 3. lights ON ------------------------------------------------------
    Step.start("Turn the bulb ON",
               "bulb lights; dashboard and simulator both show ~10.8 W, "
               "labelled 'real device / measured'")
    Step.info("publishing home/command/living/lights — the same path a human tapping "
              "Approve uses, so the publisher does the switching and nothing races "
              "it for the bulb's single connection")
    if not command_load("lights", "on"):
        Step.fail("could not publish the command")
    else:
        Step.ok("command published")

    Step.info("waiting for the publisher to execute it and report back…")
    seen = None
    for _ in range(8):
        time.sleep(3)
        seen = load_state("living/lights")
        if seen.get("state") == "on" and (seen.get("watts") or 0) > 1:
            break
    if seen and seen.get("state") == "on":
        Step.ok(f"hub agrees: living/lights on at {seen.get('watts')} W "
                f"metered={seen.get('metered')}")
    else:
        Step.fail(f"hub did not pick up the change: {seen}")

    # -- 4. the button press ----------------------------------------------
    Step.start("Press Modulino button A (simulated via the MCU's own counter)",
               "counter bl increments -> publisher sees the delta -> switches the "
               "REAL bulb off -> writes CMD back -> button LED 0 goes out")

    # The button TOGGLES, so the direction depends entirely on where the bulb is
    # now. Confirm that from the device before pressing, or a failed setup step
    # silently inverts this step and it "passes" having turned the bulb ON.
    pre = read_bulb()
    if not pre or not pre.get("on"):
        Step.fail(f"bulb is not ON before the press ({pre}), so a toggle cannot "
                  f"turn it off — aborting this step rather than reporting a "
                  f"misleading result")
        return
    Step.info(f"confirmed starting state: bulb ON at {pre.get('watts')} W, so one "
              f"press must turn it OFF")

    before = mcu_counters()
    Step.info(f"button A = 'lights'.  counter before: bl={before['bl'] if before else '?'}")

    if not press_button("a"):
        Step.fail("MCU did not acknowledge SIMBTN — is the sketch current?")
        return
    Step.ok("MCU acknowledged the press and bumped its counter")

    after = mcu_counters()
    if before and after and after["bl"] != before["bl"]:
        Step.ok(f"counter moved bl={before['bl']} -> bl={after['bl']}")
    else:
        Step.fail(f"counter did not move: {before} -> {after}")

    Step.info(f"waiting up to {PRESS_TIMEOUT_S}s for the publisher to act on the delta…")
    deadline = time.time() + PRESS_TIMEOUT_S
    acted = False
    while time.time() < deadline:
        time.sleep(2.5)
        if load_state("living/lights").get("state") == "off":
            acted = True
            break
    if acted:
        Step.ok("publisher acted on the press — hub now reports living/lights off")
    else:
        Step.fail("publisher did not switch the load within the timeout")

    # Proof it went through the BUTTON path specifically. The hub reporting "off"
    # would look identical if something else had switched the bulb; this line is
    # only printed by ButtonWatch._act(), and only after a real Kasa read-back.
    log = adb("shell", "tail -n 40 /tmp/publisher.log", timeout=30).stdout
    btn_lines = [l for l in log.splitlines() if "[button]" in l]
    if btn_lines:
        Step.ok(f"publisher log confirms the button path: {btn_lines[-1].strip()}")
    else:
        Step.fail("no [button] line in the publisher log — the load changed, but "
                  "not via the button path this shot is meant to prove")

    # And that the MCU got its confirmation, which is what drives the LED.
    if "could not write back to the MCU" in log:
        Step.fail("publisher could not write the confirmation back — the LED will "
                  "time out and light the error indicator instead")
    else:
        Step.ok("confirmation written back to the MCU — button LED 0 should now be out")

    # -- 5. independent verification --------------------------------------
    Step.start("Verify the BULB itself is off",
               "read the device directly, not the hub's opinion of it")
    time.sleep(SETTLE_S)
    bulb = read_bulb()
    if bulb is None:
        Step.fail("could not read the bulb")
    elif bulb.get("on") is False:
        Step.ok(f"bulb '{bulb.get('alias')}' is OFF, drawing {bulb.get('watts')} W "
                f"— confirmed by the device, not the hub")
    else:
        Step.fail(f"bulb still reports on: {bulb}")


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous demo driver")
    ap.add_argument("--no-browser", action="store_true", help="skip opening windows")
    ap.add_argument("--check", action="store_true", help="preflight only, change nothing")
    args = ap.parse_args()

    print(c("\nAUTONOMOUS DEMO — AI Home Energy Concierge", C_HDR))
    print(c(f"hub={HUB}", C_DIM))

    if not preflight():
        print()
        print(c("Preflight failed. Fix the above before running the demo.", C_BAD))
        return 1
    if args.check:
        print()
        print(c("Preflight only — nothing was changed.", C_OK))
        return 0

    shot1(browser=not args.no_browser)

    print()
    print(c("=" * 74, C_HDR))
    if Step.failures:
        print(c(f"{Step.failures} step(s) FAILED — see above.", C_BAD))
    else:
        print(c("All steps passed. The button path drove a real device end to end.", C_OK))
    print(c("=" * 74, C_HDR))
    return 1 if Step.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
