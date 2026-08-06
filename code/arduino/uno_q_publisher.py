#!/usr/bin/env python3
"""UNO Q Linux-side publisher — runs on the Qualcomm Dragonwing (Debian).

This is the architectural differentiator: the UNO Q is a network peer that does
its own edge processing and speaks MQTT over Wi-Fi, not a USB sensor cable.

Edge intelligence performed HERE rather than on the hub:
  - median-of-5 smoothing on lux and temperature (kills sensor spikes)
  - occupancy state machine with a grace hold (stops PIR flapping)
  - change-triggered publishing with a heartbeat (cuts broker traffic sharply)

Run:  ROOM=living MQTT_HOST=192.168.1.50 python3 uno_q_publisher.py
      python3 uno_q_publisher.py --fake-serial      # no MCU attached
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import random
import asyncio
import signal
import socket
import statistics
import sys
import threading
import time
from typing import Dict, List, Optional

try:
    import serial
except ImportError:
    serial = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("paho-mqtt required: pip3 install paho-mqtt", file=sys.stderr)
    sys.exit(1)

# Optional: real physical actuation via TP-Link Kasa smart plugs/bulbs.
# Soft import — without it the actuator degrades to 'simulated' and says so,
# exactly like a missing MCU. It must never stop the sensing half from running.
try:
    import kasa
except ImportError:
    kasa = None

ROOM = os.environ.get("ROOM", "living")
MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.1.50")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
BAUD = 115200

# Arduino Router monitor socket — how the MCU's Serial reaches Linux on a UNO Q.
# See tcp_lines() for why this exists instead of a /dev/ttyACM* device.
MCU_TCP_HOST = os.environ.get("MCU_TCP_HOST", "127.0.0.1")
MCU_TCP_PORT = int(os.environ.get("MCU_TCP_PORT", "7500"))

# Publish thresholds — the point of edge filtering.
LUX_CHANGE_PCT = 0.15         # 15% relative change is material
TEMP_CHANGE_C = 0.3
HEARTBEAT_S = 10              # publish at least this often regardless
OCC_GRACE_S = 30              # hold "occupied" this long after last motion
SMOOTH_WINDOW = 5             # median-of-5

LOADS_FILE = "/tmp/loads.json"    # flip loads during the demo with no rewiring
LOADS_DEFAULT = {"lights": {"state": "off", "watts": 240},
                 "ac": {"state": "off", "watts": 1100}}

# ---------------------------------------------------------------------------
# Kasa smart-device binding — the real physical actuator.
#
# Everything here is env-overridable ON PURPOSE. Venue DHCP will not hand out
# the same IPs as the bench, so prefer aliases (which travel with the device)
# and treat hosts as an optimisation. To add a load (e.g. a table fan standing
# in for HVAC) just extend KASA_LOADS and set KASA_FAN_ALIAS / KASA_FAN_HOST.
#
#   KASA_LIGHTS_HOST / KASA_LIGHTS_ALIAS
#   KASA_AC_HOST     / KASA_AC_ALIAS
#   KASA_FAN_HOST    / KASA_FAN_ALIAS
#   KASA_LOADS       comma-separated list, default "lights,ac"
# ---------------------------------------------------------------------------
KASA_LOADS = [s.strip() for s in
              os.environ.get("KASA_LOADS", "lights,ac").split(",") if s.strip()]
KASA_DEFAULT_ALIASES = {
    "lights": "Bedroom light 2",   # KL120 bulb  — metered
    "ac":     "Space heater",      # HS110 plug  — metered
    "fan":    "",                  # set KASA_FAN_ALIAS when the fan is on the bench
}
KASA_DISCOVER_S = float(os.environ.get("KASA_DISCOVER_S", "8"))
KASA_SETTLE_S = float(os.environ.get("KASA_SETTLE_S", "0.4"))
# Metered-watts cadence — how often we ask a bound device what it is drawing.
#
# 30 s, not 5. Every poll opens a connection to the device, and a KL120 bulb
# VISIBLY DIPS when it services one: at 5 s the bulb flickers about twelve
# times a minute, which is glaring on camera and was reported as "my light
# keeps flickering" after a demo run. Confirmed by stopping the publisher —
# the flicker stopped with it.
#
# Costs nothing that matters: this poll only detects OUT-OF-BAND changes
# (someone using the Kasa app or a wall switch). Every switch WE make —
# Actuator.execute and ButtonWatch._act — reads the device back and
# publishes immediately, so the dashboard still updates the instant the
# demo does anything. Lower it only if you need fast detection of changes
# made outside this system.
KASA_POLL_S = float(os.environ.get("KASA_POLL_S", "30"))
# TP-Link firmware serves ONE connection at a time, so a concurrent reader (the
# Kasa phone app, another script) can make a write raise even though it landed.
# Retry, and judge success by reading the device back — see KasaBank.switch().
KASA_SWITCH_RETRIES = int(os.environ.get("KASA_SWITCH_RETRIES", "3"))
KASA_RETRY_S = float(os.environ.get("KASA_RETRY_S", "0.8"))

# ---------------------------------------------------------------------------
# Which signals the MCU is allowed to own.
#
# The hub merges room state with {**prev, **payload}, so the LAST writer of a
# key wins. The MCU republishes at 1 Hz, which means anything it emits will
# overwrite a value POSTed to /api/sensor within a second.
#
# That bit us for real: injecting temp_c=29.5 to demo the R7 comfort guardrail
# silently had no effect, because the knob kept restoring 21.9 and the apply
# was then allowed instead of refused.
#
# So state it explicitly rather than leave it to whoever publishes last:
#   MCU_SIGNALS=temp_c   (default) the Modulino Knob owns temperature. Turning
#                        the physical dial past 27 C is the tactile way to drive
#                        the R7 refusal on stage.
#   MCU_SIGNALS=         the MCU publishes NO sensor values; the phone/simulator
#                        owns every signal. Use this when demoing without hands
#                        on the hardware.
# Anything the MCU is not allowed to own simply passes to whoever else sets it.
# ---------------------------------------------------------------------------
MCU_SIGNALS = [s.strip() for s in
               os.environ.get("MCU_SIGNALS", "temp_c").split(",") if s.strip()]

RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False
    print("[uno_q] SIGTERM received, shutting down", flush=True)


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def find_port() -> Optional[str]:
    """The device node differs by image — try the likely ones in order.

    NOTE: on the Arduino UNO Q this will find nothing, and that is expected —
    see mcu_tcp_available() below. It is kept for classic Arduino boards
    attached over USB CDC.
    """
    if serial is None:
        return None
    candidates: List[str] = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]
    candidates += sorted(glob.glob("/dev/serial/by-id/*"))
    for path in candidates:
        try:
            s = serial.Serial(path, BAUD, timeout=1)
            s.close()
            print(f"[uno_q] MCU serial found at {path}", flush=True)
            return path
        except Exception:
            continue
    return None


def mcu_tcp_available(host: str = MCU_TCP_HOST, port: int = MCU_TCP_PORT,
                      timeout: float = 2.0) -> bool:
    """Is the Arduino Router's monitor socket reachable?"""
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except Exception:
        return False


# How long to keep looking for the MCU before giving up and inventing data.
MCU_WAIT_S = float(os.environ.get("MCU_WAIT_S", "30"))

# --- TIER 1: edge anomaly scoring, on the board's own CPU -------------------
# The QRB2210 is a quad Cortex-A53 with no NPU/DSP stack, so an LLM here is
# physically wrong. A trained logistic regression is not: pure Python, stdlib
# only, microseconds per sample. Flagged off by default.
AI_ANOMALY = os.environ.get("AI_ANOMALY", "0") == "1"
_ANOMALY = None


def edge_anomaly_score(payload: Dict, loads_info: Dict):
    """(score, top_feature) for this sample, or None if unavailable.

    Deliberately best-effort: the scorer must never be able to interrupt
    telemetry. Any failure disables it for the rest of the run and the publisher
    carries on exactly as before.
    """
    global _ANOMALY
    if _ANOMALY is None:
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
            import anomaly as _a
            _ANOMALY = _a
            print(f"[edge-ai] {_a.model_provenance()}", flush=True)
        except Exception as exc:
            print(f"[edge-ai] unavailable, scoring disabled: {exc}", flush=True)
            _ANOMALY = False
    if _ANOMALY is False:
        return None

    try:
        loads = {f"{ROOM}/{n}": {"state": i.get("state"), "watts": i.get("watts") or 0}
                 for n, i in (loads_info or {}).items()}
        snap = {"rooms": {ROOM: dict(payload)}, "loads": loads,
                "user": {"presence": "home"}, "now": time.time()}
        s, top = _ANOMALY.score(_ANOMALY.featurize(snap))
        return round(s, 3), top
    except Exception as exc:
        print(f"[edge-ai] scoring failed, disabling: {exc}", flush=True)
        _ANOMALY = False
        return None


def wait_for_mcu(timeout_s: float = MCU_WAIT_S) -> bool:
    """Poll for the router socket instead of deciding on a single attempt.

    `arduino-router` restarts whenever the sketch is re-flashed, and takes
    several seconds to come back after the board is re-plugged over USB. A lone
    probe that lost that race used to commit this process to SYNTHETIC data for
    its entire lifetime — the publisher kept running, the dashboard kept
    updating once a second, and nothing downstream said the numbers were
    invented. Observed live: a redeploy right after a re-plug produced a
    plausible drifting `temp_c` while the real knob sat still at 21.9 °C.

    Retrying costs a few seconds on a genuinely MCU-less run and removes a
    failure mode that is nearly impossible to spot from the dashboard.
    """
    if mcu_tcp_available():
        return True
    print(f"[uno_q] MCU router socket not up yet — polling "
          f"tcp://{MCU_TCP_HOST}:{MCU_TCP_PORT} for {timeout_s:.0f}s "
          f"(it restarts with the sketch and after a re-plug)", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.0)
        if mcu_tcp_available():
            waited = timeout_s - max(0.0, deadline - time.time())
            print(f"[uno_q] MCU appeared after {waited:.0f}s", flush=True)
            return True
    return False


# The live monitor socket, so the return direction (Linux -> MCU) can reuse the
# connection tcp_lines() already holds open. The router bridges that socket to
# the MCU's Serial in BOTH directions; whether host->MCU bytes are actually
# delivered is Risk V-2 and is what the button LEDs now test in the field.
MCU_SOCK: Optional[socket.socket] = None
MCU_SOCK_LOCK = threading.Lock()


def mcu_send(text: str) -> bool:
    """Write a line back to the MCU. False if there is no socket or it failed."""
    with MCU_SOCK_LOCK:
        s = MCU_SOCK
        if s is None:
            return False
        try:
            s.sendall(text.encode("ascii", errors="ignore"))
            return True
        except Exception as exc:
            print(f"[uno_q] MCU write failed: {exc}", flush=True)
            return False


def tcp_lines(host: str = MCU_TCP_HOST, port: int = MCU_TCP_PORT):
    """Yield telemetry lines from the Arduino Router monitor socket.

    On the UNO Q the MCU's Serial does NOT appear as /dev/ttyACM* on the Linux
    side. The STM32 talks to the QRB2210 over RPMsg into the `arduino-router`
    service (/dev/ttyHS1), which republishes the stream on tcp://127.0.0.1:7500.
    A separate socat unit also mirrors that socket to /dev/ttyGS0, which is what
    surfaces as a COM port on a USB-attached host PC.

    Verified on Debian 13 / arduino-router with the zephyr core 0.90.0:
        arduino-router --unix-port /var/run/arduino-router.sock \
                       --serial-port /dev/ttyHS1 --serial-baudrate 115200

    Reconnects on its own, because the router restarts whenever a sketch is
    re-flashed and we must not die when that happens mid-demo.
    """
    buf = b""
    while RUNNING:
        s = None
        try:
            s = socket.create_connection((host, port), 5)
            s.settimeout(2)
            global MCU_SOCK
            with MCU_SOCK_LOCK:
                MCU_SOCK = s
            print(f"[uno_q] MCU monitor connected at tcp://{host}:{port}", flush=True)
            while RUNNING:
                try:
                    chunk = s.recv(512)
                except socket.timeout:
                    continue
                if not chunk:
                    break          # router closed the stream (re-flash) — reconnect
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", errors="ignore").strip()
        except Exception as exc:
            print(f"[uno_q] MCU monitor error: {exc} — retrying in 2s", flush=True)
            time.sleep(2)
        finally:
            with MCU_SOCK_LOCK:
                if MCU_SOCK is s:
                    MCU_SOCK = None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


class Smoother:
    """Median-of-N — rejects single-sample sensor spikes."""

    def __init__(self, n: int = SMOOTH_WINDOW):
        self.n = n
        self.buf: List[float] = []

    def push(self, v: float) -> float:
        self.buf.append(v)
        if len(self.buf) > self.n:
            self.buf.pop(0)
        return statistics.median(self.buf)


class OccupancyFSM:
    """State machine with a grace hold, so PIR gaps do not flap the state."""

    def __init__(self, grace_s: float = OCC_GRACE_S):
        self.grace = grace_s
        self.last_motion = 0.0
        self.state = False

    def push(self, raw_motion: bool) -> bool:
        now = time.time()
        if raw_motion:
            self.last_motion = now
        self.state = (self.last_motion > 0) and ((now - self.last_motion) < self.grace)
        return self.state


class Publisher:
    def __init__(self, host: str, port: int):
        try:
            self.c = mqtt.Client(client_id=f"unoq-{ROOM}")
        except Exception:
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"unoq-{ROOM}")
        self.c.on_connect = self._on_connect
        self.c.on_disconnect = lambda *a, **k: print("[uno_q] MQTT disconnected", flush=True)
        self.host, self.port = host, port
        self.backoff = 1.0
        # Topics to (re)subscribe on every successful connect. Subscribing once at
        # startup is not enough: connect_async() has not completed yet, and any
        # subscription is also lost across a reconnect.
        self.subscriptions: List[str] = []
        self._connect()
        self.c.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"[uno_q] MQTT connected rc={rc}", flush=True)
        for topic in self.subscriptions:
            client.subscribe(topic)
            print(f"[uno_q] subscribed {topic}", flush=True)

    def subscribe(self, topic: str, callback=None) -> None:
        """Register a subscription that survives reconnects."""
        if callback is not None:
            self.c.message_callback_add(topic, callback)
        if topic not in self.subscriptions:
            self.subscriptions.append(topic)
        self.c.subscribe(topic)   # in case we are already connected

    def _connect(self):
        """Auto-reconnect with exponential backoff."""
        while RUNNING:
            try:
                self.c.connect_async(self.host, self.port, keepalive=30)
                print(f"[uno_q] connecting to {self.host}:{self.port}", flush=True)
                self.backoff = 1.0
                return
            except Exception as exc:
                print(f"[uno_q] MQTT connect failed ({exc}); retry in {self.backoff:.0f}s", flush=True)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, 30.0)

    def sensors(self, payload: Dict):
        self.c.publish(f"home/sensors/{ROOM}", json.dumps(payload))

    def load(self, name: str, state: str, watts: float, metered: bool = False):
        """Publish load state. `metered=True` means `watts` was MEASURED by the
        device (HS110 plug / KL120 bulb), not taken from the modelled table in
        hub/energy_model.py — the distinction is carried so a reader can tell."""
        self.c.publish(f"home/loads/{ROOM}/{name}",
                       json.dumps({"state": state, "watts": watts,
                                   "metered": metered, "ts": time.time()}))

    def actuator(self, name: str, state: str, reco_id: str, ok: bool, source: str):
        """Confirm a physical action back to the hub — closes the actuation loop."""
        self.c.publish(f"home/actuator/{ROOM}/{name}",
                       json.dumps({"state": state, "source": source, "reco_id": reco_id,
                                   "ok": ok, "ts": time.time()}))

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def _read_watts(dev) -> Optional[float]:
    """Instantaneous measured power, or None if this device has no meter.

    Uses the Energy module rather than the deprecated `dev.emeter_realtime`,
    which emits a DeprecationWarning on python-kasa 0.10+ — and that warning
    would land in the middle of our stdout telemetry stream.
    """
    if kasa is None:
        return None
    try:
        energy = dev.modules.get(kasa.Module.Energy)
        if energy is None:
            return None
        w = energy.current_consumption
        return float(w) if w is not None else None
    except Exception:
        return None


class KasaBank:
    """Binds the demo's logical loads to real TP-Link Kasa devices on the LAN.

    This is the physical-action half of Archetype E. A 'lights' command does not
    light an LED that *represents* a lamp — it switches an actual lamp.

    Resolution order per load, so a changing network never hard-breaks the demo:
      1. an explicit host/IP from the environment  (KASA_<LOAD>_HOST)
      2. discovery by device alias                 (KASA_<LOAD>_ALIAS)
      3. unbound -> the caller falls back to 'simulated' and says so

    Alias resolution matters: venue DHCP will hand out different IPs than the
    bench did, and the aliases ("Bedroom light 2") travel with the device.

    Several of these devices (HS110 plug, KL120 bulb) expose an energy meter.
    Where one does, we publish MEASURED watts instead of the modelled table
    value from hub/energy_model.py — which retires the "load power is modelled,
    not metered" limitation in code/README.md for those loads.
    """

    def __init__(self) -> None:
        self.hosts: Dict[str, str] = {}
        self.aliases: Dict[str, str] = {}
        self.devices: Dict[str, object] = {}
        self.lock = threading.Lock()

        for load in KASA_LOADS:
            host = os.environ.get(f"KASA_{load.upper()}_HOST", "").strip()
            alias = os.environ.get(f"KASA_{load.upper()}_ALIAS",
                                   KASA_DEFAULT_ALIASES.get(load, "")).strip()
            if host:
                self.hosts[load] = host
            if alias:
                self.aliases[load] = alias

    @property
    def enabled(self) -> bool:
        return kasa is not None and bool(self.hosts or self.aliases)

    def _run(self, coro):
        """Run one coroutine. Commands are rare and serialised by self.lock."""
        return asyncio.new_event_loop().run_until_complete(coro)

    async def _connect_one(self, host: str):
        """Direct TCP session to a known host.

        Deliberately NOT Discover.discover_single(), which sends a UDP probe.
        Two failures made that unusable here, both reproduced on this bench:
          * the ArtiFi mesh drops client-to-client UDP broadcast, so discovery
            from the board found nothing while TCP to :9999 worked fine;
          * a KL120 bulb that is currently switched OFF stops answering UDP
            discovery altogether — so the very state we most need to verify
            after switching something off is the one discovery cannot read.
        TCP works in both cases.
        """
        dev = await kasa.Device.connect(host=host)
        await dev.update()
        return dev

    async def _resolve(self) -> None:
        # Explicit hosts first — cheap and deterministic.
        for load, host in self.hosts.items():
            if load in self.devices:
                continue
            try:
                self.devices[load] = await self._connect_one(host)
                print(f"[kasa] {load} -> {host} "
                      f"({self.devices[load].alias})", flush=True)
            except Exception as exc:
                print(f"[kasa] {load}: host {host} unreachable: {exc}", flush=True)

        # Anything still unbound and carrying an alias: fall back to discovery.
        wanted = {load: a for load, a in self.aliases.items()
                  if load not in self.devices}
        if not wanted:
            return
        try:
            found = await kasa.Discover.discover(timeout=KASA_DISCOVER_S)
        except Exception as exc:
            print(f"[kasa] discovery failed: {exc}", flush=True)
            return
        by_alias = {}
        for host, dev in found.items():
            try:
                await dev.update()
                by_alias[dev.alias.strip().lower()] = (host, dev)
            except Exception:
                continue
        for load, alias in wanted.items():
            hit = by_alias.get(alias.strip().lower())
            if hit:
                self.devices[load] = hit[1]
                print(f"[kasa] {load} -> {hit[0]} (alias '{alias}')", flush=True)
            else:
                print(f"[kasa] {load}: no device with alias '{alias}' on the LAN",
                      flush=True)

    def resolve(self) -> None:
        if not self.enabled:
            print("[kasa] disabled (python-kasa missing or no devices configured)",
                  flush=True)
            return
        with self.lock:
            try:
                self._run(self._resolve())
            except Exception as exc:
                print(f"[kasa] resolve error: {exc}", flush=True)

    def switch(self, load: str, action: str):
        """Switch a real device and READ ITS STATE BACK.

        Returns (ok, source, watts). `ok` reflects what the device reports after
        the write, not merely that a request was sent — the old serial path
        returned True for 'bytes were flushed', which is not the same claim.
        """
        if not self.enabled or load not in self.devices:
            return None, "unbound", None

        want_on = (action == "on")

        async def go():
            """Switch, then decide success by READING THE DEVICE, not by whether
            the write returned cleanly.

            TP-Link firmware serves one connection at a time, so a concurrent
            reader (the Kasa phone app, another script, our own poll loop) makes
            a write raise `Communication error ... transition_light_state` even
            when the command DID land. Reporting ok=False there is a false
            negative — the honest answer is whatever state the device is
            actually in afterwards. So: attempt, then verify, and retry only if
            the device genuinely is not where we asked it to be.
            """
            dev = self.devices[load]
            last_exc = None
            for attempt in range(KASA_SWITCH_RETRIES):
                try:
                    await (dev.turn_on() if want_on else dev.turn_off())
                except Exception as exc:
                    last_exc = exc          # may still have landed — verify below
                await asyncio.sleep(KASA_SETTLE_S)
                try:
                    await dev.update()
                    if bool(dev.is_on) == want_on:
                        if last_exc is not None:
                            print(f"[kasa] {load}: write reported "
                                  f"'{type(last_exc).__name__}' but the device IS "
                                  f"{action} — trusting the device", flush=True)
                        return True, bool(dev.is_on), _read_watts(dev)
                except Exception as exc:
                    last_exc = exc
                await asyncio.sleep(KASA_RETRY_S)
            # Out of attempts: report the last state we could read, honestly.
            try:
                await dev.update()
                return False, bool(dev.is_on), _read_watts(dev)
            except Exception:
                raise last_exc if last_exc else RuntimeError("unreachable")

        with self.lock:
            try:
                ok, is_on, watts = self._run(go())
            except Exception as exc:
                print(f"[kasa] switch {load} -> {action} failed: {exc}", flush=True)
                return False, "kasa_error", None

        if not ok:
            print(f"[kasa] {load} did NOT reach '{action}' after "
                  f"{KASA_SWITCH_RETRIES} attempts (reports "
                  f"{'on' if is_on else 'off'})", flush=True)
        return ok, "kasa", watts

    def toggle(self, load: str):
        """Flip a load, deciding the target from the device's CURRENT state.

        Returns (ok, want, source). The current state is read fresh rather than
        tracked, because the bulb can also be switched by the hub, the Kasa app
        or a wall switch — a cached mirror would invert the wrong way.

        `self.devices` is the authority on whether a load is BOUND. A failed
        poll() is not: TP-Link firmware serves one connection at a time, so our
        own 5 s sweep (or the phone app) can lock a read out for a moment. An
        earlier version returned "unbound" on any empty poll, which reported
        'no Kasa device bound' for a bulb that was sitting right there — so a
        transient read got the same LED and the same log line as a missing
        device. Retry once, then say `kasa_error`, which is a different claim.
        """
        if not self.enabled or load not in self.devices:
            return False, None, "unbound"

        info = self.poll().get(load)
        if info is None or info.get("state") is None:
            time.sleep(KASA_RETRY_S)
            info = self.poll().get(load)
        if info is None or info.get("state") is None:
            print(f"[kasa] {load}: bound but could not read state to toggle from",
                  flush=True)
            return False, None, "kasa_error"

        want = "off" if info.get("state") == "on" else "on"
        ok, source, _watts = self.switch(load, want)
        return bool(ok), want, source

    def poll(self) -> Dict[str, Dict]:
        """Current state + measured power for every bound load."""
        if not self.enabled or not self.devices:
            return {}

        async def go():
            out = {}
            for load, dev in self.devices.items():
                try:
                    await dev.update()
                    entry = {"state": "on" if dev.is_on else "off",
                             "metered": False}
                    watts = _read_watts(dev)
                    if watts is not None:
                        entry["watts"] = watts
                        entry["metered"] = True
                    out[load] = entry
                except Exception:
                    continue
            return out

        with self.lock:
            try:
                return self._run(go())
            except Exception as exc:
                print(f"[kasa] poll error: {exc}", flush=True)
                return {}


class ButtonWatch:
    """Modulino button presses -> real Kasa switching -> LED confirmation.

    A hardware debug path that does not involve the browser at all: press
    button A or B on the Buttons node and the corresponding device should
    physically switch. It answers "is the Kasa path working through the UNO Q?"
    with the hub, the rules engine and the UI all out of the way.

    The MCU sends monotonic COUNTERS (`bl`, `ba`), not edges, so this survives a
    dropped telemetry line — the count still climbs and the delta is still seen.
    """

    KEYS = {"bl": "lights", "ba": "ac"}

    def __init__(self, bank: "KasaBank", pub: "Publisher"):
        self.bank = bank
        self.pub = pub
        self.seen: Dict[str, int] = {}

    def observe(self, raw: Dict) -> None:
        for key, load in self.KEYS.items():
            if key not in raw:
                continue
            try:
                count = int(raw[key])
            except (TypeError, ValueError):
                continue

            prev = self.seen.get(key)
            self.seen[key] = count

            # First sight: BASELINE ONLY. Acting here would replay every press
            # since the MCU booted, each time this process restarts.
            if prev is None:
                continue
            if count == prev:
                continue
            # A counter that went BACKWARDS means the MCU rebooted and reset it.
            # (A genuine wrap needs 65535 presses; a re-flash needs none.)
            if count < prev:
                print(f"[button] {load}: counter reset {prev} -> {count} "
                      f"(MCU restarted) — re-baselined, not switching", flush=True)
                continue

            self._act(load, count - prev)

    def _act(self, load: str, presses: int) -> None:
        # Two presses land back where they started; only the parity matters.
        if presses > 1:
            print(f"[button] {load}: {presses} presses coalesced", flush=True)
            if presses % 2 == 0:
                state = "on" if self._is_on(load) else "off"
                mcu_send(f"CMD {load} {state} ok\n")
                return

        ok, want, source = self.bank.toggle(load)
        if want is None:
            why = ("no Kasa device bound — nothing to switch" if source == "unbound"
                   else "device is bound but did not answer — not switching blind")
            print(f"[button] {load}: {why}", flush=True)
            mcu_send(f"CMD {load} off fail\n")
            return

        print(f"[button] {load} -> {want}  ok={ok} source={source}", flush=True)

        # Tell the MCU what the DEVICE reported, so the LED cannot claim a switch
        # that did not happen. This write is also the live test of the host->MCU
        # serial direction (Risk V-2).
        if not mcu_send(f"CMD {load} {want} {'ok' if ok else 'fail'}\n"):
            print("[button] could not write back to the MCU — LED will time out",
                  flush=True)

        # Push the new state immediately rather than waiting for the next
        # KASA_POLL_S sweep, so the dashboard tracks the button in real time.
        if ok:
            info = self.bank.poll().get(load) or {}
            watts = info.get("watts")
            self.pub.load(load, want,
                          round(float(watts), 1) if watts is not None else 0.0,
                          metered=bool(info.get("metered")))
        self.pub.actuator(load, want, "manual-button", bool(ok), source)

    def _is_on(self, load: str) -> bool:
        return (self.bank.poll().get(load) or {}).get("state") == "on"


class Actuator:
    """Executes hub commands and reports back.

    This is the physical-action half of Archetype E. The hub decides *whether* an
    action is safe (the R7 comfort guardrail gates it there); this class only carries
    the command to the hardware and reports honestly whether it landed.

    Preference order: a real Kasa device -> the MCU over serial -> simulated.
    """

    def __init__(self, pub: "Publisher", serial_handle=None, kasa_bank=None):
        self.pub = pub
        self.serial = serial_handle
        self.kasa = kasa_bank
        self.pending: "queue.Queue" = queue.Queue()
        self.last_ack: Dict = {}

    def on_command(self, client, userdata, msg):
        """MQTT callback: home/command/<room>/<load>."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            parts = msg.topic.split("/")
            if len(parts) < 4:
                return
            room, load = parts[2], parts[3]
            if room != ROOM:
                return

            action = payload.get("action")
            if action not in ("on", "off"):
                print(f"[uno_q] ignoring bad action {action!r}", flush=True)
                return

            reco_id = payload.get("reco_id", "")
            print(f"[uno_q] COMMAND {load} -> {action} (reco {reco_id})", flush=True)
            self.execute(load, action, reco_id)
        except Exception as exc:
            print(f"[uno_q] bad command on {msg.topic}: {exc}", flush=True)

    def execute(self, load: str, action: str, reco_id: str) -> bool:
        """Carry the command to real hardware and publish an honest confirmation."""
        ok = False
        source = "none"
        measured_w = None

        # 1. A real Kasa device, if this load is bound to one. Preferred, because
        #    it is the only path that can report what the hardware actually did.
        if self.kasa is not None:
            k_ok, k_source, k_watts = self.kasa.switch(load, action)
            if k_source != "unbound":
                ok, source, measured_w = bool(k_ok), k_source, k_watts
                print(f"[uno_q] KASA {load} -> {action} ok={ok}"
                      + (f" measured={measured_w:.1f}W" if measured_w is not None else ""),
                      flush=True)

        # 2. The MCU over serial. Note this only ever proves bytes were flushed —
        #    the MCU's ack comes back asynchronously and is not awaited here.
        if source == "none" and self.serial is not None:
            try:
                line = f"CMD {load} {action}\n".encode("ascii")
                self.serial.write(line)
                self.serial.flush()
                ok, source = True, "mcu_serial"
                print(f"[uno_q] sent to MCU: {line!r}", flush=True)
            except Exception as exc:
                print(f"[uno_q] serial write failed: {exc}", flush=True)
                source = "serial_error"

        # 3. Nothing attached. Be explicit rather than silently claiming a
        #    physical action occurred.
        if source == "none":
            ok, source = True, "simulated"
            print(f"[uno_q] SIMULATED actuation {load} -> {action} "
                  f"(no Kasa device bound, no MCU attached)", flush=True)

        # Reflect the new load state so the hub's power figures follow reality.
        # A metered device (HS110 plug, KL120 bulb) reports what it is ACTUALLY
        # drawing; only fall back to the modelled table value when it cannot.
        loads = read_loads()
        if load in loads:
            loads[load]["state"] = action
            try:
                with open(LOADS_FILE, "w") as f:
                    json.dump(loads, f)
            except Exception:
                pass
            if measured_w is not None:
                watts = measured_w
            else:
                watts = float(loads[load].get("watts", 0)) if action == "on" else 0.0
            self.pub.load(load, action, watts, metered=measured_w is not None)

        self.pub.actuator(load, action, reco_id, ok, source)
        return ok


def fake_lines():
    """Generate MCU-shaped lines so this can be built before hardware works.

    Every line DECLARES itself synthetic. This used to emit bare values with no
    provenance keys at all, which was worse than it sounds: the hub merges room
    state with {**prev, **payload}, so a `temp_src: "knob_sim"` left over from an
    earlier real run SURVIVED while these invented values overwrote temp_c. The
    dashboard then showed a made-up temperature attributed to the physical knob
    — the exact confusion the provenance keys were added to prevent. A generated
    value must never be able to inherit a measured value's label.
    """
    occ, lux, t, h = True, 200, 23.0, 45.0
    while RUNNING:
        if random.random() < 0.15:
            occ = not occ
        lux = max(0, min(900, lux + random.randint(-50, 50)))
        t = max(18.0, min(30.0, t + random.uniform(-0.25, 0.25)))
        h = max(30.0, min(70.0, h + random.uniform(-1.0, 1.0)))
        yield json.dumps({"occupancy": occ, "lux": lux, "temp_c": round(t, 1),
                          "humidity": round(h, 1), "raw_pir": 1 if occ else 0,
                          "temp_src": "synthetic", "hum_src": "synthetic",
                          "lux_src": "synthetic", "occ_src": "synthetic",
                          "nodes": "none"})
        time.sleep(1)


def serial_lines(handle):
    """Yield telemetry lines from an already-open serial handle.

    The handle is opened by the caller and shared with the Actuator, so commands
    are written on the same link the telemetry arrives on.
    """
    buf = b""
    while RUNNING:
        try:
            chunk = handle.read(256)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", errors="ignore").strip()
        except Exception as exc:
            print(f"[uno_q] serial error: {exc}", flush=True)
            time.sleep(1)


def read_loads() -> Dict:
    """Load state from a local file so we can flip loads mid-demo."""
    try:
        with open(LOADS_FILE, "r") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return dict(LOADS_DEFAULT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake-serial", action="store_true",
                    help="generate data without an MCU attached")
    ap.add_argument("--broker", default=MQTT_HOST)
    ap.add_argument("--port", type=int, default=MQTT_PORT)
    args = ap.parse_args()

    print(f"[uno_q] room={ROOM} broker={args.broker}:{args.port} "
          f"fake={args.fake_serial}", flush=True)

    # Telemetry source, in order of preference:
    #   1. --fake-serial            explicit, no hardware needed
    #   2. Arduino Router monitor   the UNO Q path (MCU Serial over RPMsg -> tcp/7500)
    #   3. /dev/ttyACM*             classic Arduino over USB CDC
    #   4. fake                     honest fallback so the demo still runs
    #
    # `handle` is the *writable* serial link. It stays None on the UNO Q path
    # because the router monitor is read-only from here, which is why actuation
    # lives on the network (Kasa) rather than behind a CMD write to the MCU.
    handle = None
    if args.fake_serial:
        source = fake_lines()
    elif wait_for_mcu():
        source = tcp_lines()
    else:
        port = find_port()
        if port is None:
            # Loud on purpose. Silently substituting invented readings for a
            # sensor the operator believes is live is the worst failure this
            # program has; it stays available as a fallback, but never quietly.
            print("[uno_q] " + "=" * 62, flush=True)
            print("[uno_q] NO MCU FOUND — PUBLISHING SYNTHETIC DATA", flush=True)
            print(f"[uno_q]   no router socket on tcp://{MCU_TCP_HOST}:{MCU_TCP_PORT}"
                  f" after {MCU_WAIT_S:.0f}s, and no /dev/ttyACM*", flush=True)
            print("[uno_q]   every sensor value below is INVENTED; the knob is "
                  "not being read", flush=True)
            print("[uno_q]   marked on the wire as *_src=\"synthetic\"", flush=True)
            print("[uno_q] " + "=" * 62, flush=True)
            source = fake_lines()
        else:
            handle = serial.Serial(port, BAUD, timeout=2)
            source = serial_lines(handle)

    # Bind logical loads to real Kasa devices before announcing readiness, so the
    # startup line tells the truth about what can actually be switched.
    bank = KasaBank()
    bank.resolve()

    pub = Publisher(args.broker, args.port)

    # Modulino buttons A/B toggle the two Kasa loads directly, bypassing the hub.
    buttons = ButtonWatch(bank, pub)

    # Subscribe to hub commands so the loop can close: reco -> approve -> actuate.
    actuator = Actuator(pub, handle, bank)
    pub.subscribe("home/command/#", actuator.on_command)

    if bank.devices:
        bound = ", ".join(f"{k}={v.alias}" for k, v in sorted(bank.devices.items()))
        print(f"[uno_q] command listener ready (actuator: kasa -> {bound})", flush=True)
    else:
        print(f"[uno_q] command listener ready "
              f"(actuator source: {'mcu_serial' if handle else 'simulated'})", flush=True)

    lux_s, temp_s, hum_s = Smoother(), Smoother(), Smoother()
    occ_fsm = OccupancyFSM()

    last_pub = 0.0
    last: Dict[str, float] = {}
    parsed_n = dropped_n = 0
    last_loads: Dict[str, str] = {}
    last_kasa_poll = 0.0
    # Most recent Kasa read-back, so the edge scorer can see what is actually
    # drawing power. Sensors alone cannot tell an anomaly from a quiet evening.
    last_kasa_info: Dict[str, Dict] = {}

    try:
        for line in source:
            if not RUNNING:
                break
            if not line:
                continue

            try:
                raw = json.loads(line)
                parsed_n += 1
            except Exception:
                dropped_n += 1
                if dropped_n % 20 == 1:
                    print(f"[uno_q] dropped {dropped_n} malformed lines "
                          f"({dropped_n/(parsed_n+dropped_n)*100:.1f}%)", flush=True)
                continue

            # The MCU echoes an ack after every command on the same link. It is valid
            # JSON but not telemetry, so record it and move on.
            if "ack" in raw:
                actuator.last_ack = raw
                print(f"[uno_q] MCU ack: {raw}", flush=True)
                continue

            # --- Modulino button presses (hardware debug path) ---
            # Deliberately BEFORE the MCU_SIGNALS gate below: that gate governs
            # which SENSOR signals the MCU may own, and a button press is a
            # command, not a sensor reading.
            buttons.observe(raw)

            # --- edge processing ---
            # Only signals the MCU actually reported. The Modulino sketch emits
            # a PARTIAL payload (just what it has a source for) so the phone
            # simulator can own the rest; the hub merges room state with
            # {**prev, **payload}. Defaulting a missing key to 0 here would
            # republish that zero at 1 Hz and stomp on the phone's values.
            # MCU_SIGNALS gates which of these the MCU is allowed to own — see the
            # comment on that constant. Without it, the 1 Hz stream silently wins
            # every race against a value POSTed to /api/sensor.
            present: Dict[str, object] = {}
            if "lux" in raw and "lux" in MCU_SIGNALS:
                present["lux"] = int(lux_s.push(float(raw["lux"])))
            if "temp_c" in raw and "temp_c" in MCU_SIGNALS:
                present["temp_c"] = round(temp_s.push(float(raw["temp_c"])), 1)
            if "humidity" in raw and "humidity" in MCU_SIGNALS:
                present["humidity"] = round(hum_s.push(float(raw["humidity"])), 1)
            if "occupancy" in raw and "occupancy" in MCU_SIGNALS:
                present["occupancy"] = occ_fsm.push(bool(raw["occupancy"]))

            now = time.time()

            # NOTE: sensor publishing is guarded by `if present`, but load
            # publishing below it must NOT be. Loads come from the Kasa devices,
            # not from this telemetry line — an early `continue` here (e.g. when
            # MCU_SIGNALS is empty and every line is provenance-only) silently
            # stopped every load from ever reaching the hub, which left the rules
            # engine with nothing to fire on. Keep the two independent.
            material = bool(present) and not last
            for k, v in present.items():
                if k not in last:
                    material = True                       # first sight of this signal
                elif k == "occupancy":
                    material = material or v != last[k]
                elif k == "temp_c":
                    material = material or abs(v - last[k]) >= TEMP_CHANGE_C
                elif k == "lux":
                    material = material or abs(v - last[k]) >= max(
                        LUX_CHANGE_PCT * max(last[k], 1), 10)
                # humidity alone is deliberately not a publish trigger; it rides
                # along on the next material change or the heartbeat.
            heartbeat = (now - last_pub) >= HEARTBEAT_S

            if present and (material or heartbeat):
                payload = dict(present)
                payload["ts"] = now
                # Carry provenance through so the dashboard and any audit can
                # tell a measurement from a declared simulation.
                for pk in ("temp_src", "hum_src", "lux_src", "occ_src"):
                    if pk in raw:
                        payload[pk] = raw[pk]
                # TIER 1: score this sample on the board's own A53 before it
                # leaves. Pure-Python logistic regression, microseconds, no
                # numpy — see hub/anomaly.py. Flagged off by default.
                if AI_ANOMALY:
                    sc = edge_anomaly_score(payload, last_kasa_info)
                    if sc is not None:
                        payload["anomaly_score"], payload["anomaly_top"] = sc
                pub.sensors(payload)
                last.update(present)
                last_pub = now
                why = "change" if material else "heartbeat"
                shown = " ".join(f"{k}={present[k]}" for k in sorted(present))
                print(f"[uno_q] pub ({why}) {shown}", flush=True)

            # --- real load state + MEASURED power from the Kasa devices ---
            # Ground truth beats the modelled table: if someone switches the lamp
            # by hand, the hub still sees it, and metered loads report the watts
            # they are genuinely drawing.
            if bank.devices and (now - last_kasa_poll) >= KASA_POLL_S:
                last_kasa_poll = now
                last_kasa_info = bank.poll()
                for name, info in last_kasa_info.items():
                    state = info["state"]
                    watts = info.get("watts")
                    changed = last_loads.get(name) != state
                    if changed or info.get("metered"):
                        if watts is None:
                            spec = read_loads().get(name, {})
                            watts = float(spec.get("watts", 0)) if state == "on" else 0.0
                        pub.load(name, state, round(float(watts), 1),
                                 metered=bool(info.get("metered")))
                        if changed:
                            tag = "metered" if info.get("metered") else "modelled"
                            print(f"[uno_q] load {name} -> {state} "
                                  f"({watts:.1f}W {tag})", flush=True)
                        last_loads[name] = state

            # --- loads with no Kasa binding, driven by the local control file ---
            loads = read_loads()
            for name, spec in loads.items():
                if name in bank.devices:
                    continue          # a real device owns this one
                state = spec.get("state", "off")
                if last_loads.get(name) != state:
                    pub.load(name, state, float(spec.get("watts", 0)) if state == "on" else 0.0)
                    last_loads[name] = state
                    print(f"[uno_q] load {name} -> {state} (modelled)", flush=True)
    finally:
        print(f"[uno_q] parsed={parsed_n} dropped={dropped_n}", flush=True)
        pub.close()


if __name__ == "__main__":
    main()
