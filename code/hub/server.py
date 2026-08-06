"""Hub orchestrator — the Copilot+ PC side.

Subscribes to MQTT, fuses device state into a snapshot, runs the deterministic
rules, narrates findings via the local LLM, and pushes everything to the dashboard
and phone over WebSocket.

Degrades gracefully by design: a dead broker, a bad payload, or a dead LLM must
never take the dashboard down. Every handler is wrapped.

Run:  python hub/server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import rules
from llm import LLMClient, Recommendation
from recorder import RECORDER
from occupancy_model import MODEL as OCCUPANCY, annotate_room

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))

EVAL_INTERVAL_S = 5
RECO_COOLDOWN_S = 600      # do not re-narrate the same finding for 10 min — anti-spam
POWER_HISTORY_LEN = 60     # 5 min of 5 s samples for the sparkline

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
PHONE_DIR = ROOT / "phone"
SIMULATOR_DIR = ROOT / "simulator"


class StateStore:
    """In-memory fused state. Thread-safe enough for our single writer."""

    def __init__(self):
        self.rooms: Dict[str, Dict] = {}
        self.loads: Dict[str, Dict] = {}
        self.user: Dict = {"presence": "home", "distance_m": 0, "battery": 100, "ts": time.time()}
        self.power_history: deque = deque(maxlen=POWER_HISTORY_LEN)
        self.recos: List[Recommendation] = []
        self.last_narrated: Dict[str, float] = {}
        # Finding ids the rules are firing on RIGHT NOW. Used to expire
        # anticipated cards — see latest_recos().
        self.active_finding_ids: set = set()
        self.mqtt_connected = False
        self.sim_clock_offset = 0.0    # simulator can drive a virtual clock
        # Actuation bookkeeping: what the user approved and what physically happened.
        self.realized: List[Dict] = []
        self.applied_ids: set = set()
        self.actuations: List[Dict] = []
        # Findings a guardrail vetoed this tick — computed, costed, not offered.
        self.suppressed: List[Dict] = []
        self.suppressed_ids: set = set()
        self.considered = 0          # offered + suppressed, for the "considered N" line
        self.lock = threading.Lock()

    # -- actuation ---------------------------------------------------------

    def book_realized(self, rec) -> None:
        """Record an approved action's saving as REALIZED rather than avoidable.

        The distinction matters on stage: "you could save $X" becomes "you saved $X".
        """
        with self.lock:
            if rec.id in self.applied_ids:
                return
            self.applied_ids.add(rec.id)
            self.realized.append({
                "reco_id": rec.id, "rule_name": rec.rule_name, "room": rec.room,
                "usd": rec.usd, "kwh": rec.kwh, "co2_kg": rec.co2_kg,
                "title": rec.title, "ts": time.time(),
                # An anticipated finding books a PROJECTION — money not spent
                # rather than money recovered. Both are real savings, but only
                # one of them was measured, so the two are counted separately
                # and the dashboard labels them differently.
                "kind": getattr(rec, "kind", "detected"),
            })

    def record_actuation(self, load_key: str, payload: Dict) -> None:
        """Store a physical-action confirmation from the UNO Q.

        If the confirmation says the action FAILED, un-book the saving. api_apply
        books optimistically the moment the command is published, which is what
        keeps the dashboard responsive — but a command that was published and then
        refused by the hardware has saved nothing. Leaving it booked would show
        "you saved $0.02" for a lamp still burning, which is precisely the kind of
        claim this project refuses to make elsewhere.

        `ok=False` means a real device was addressed and did not comply
        (source `kasa_error`). A declared simulation (`source="simulated"`,
        `ok=True`) is a legitimate labelled path and stays booked.
        """
        with self.lock:
            self.actuations.append({"load_key": load_key, **payload})
            self.actuations = self.actuations[-40:]

            if payload.get("ok") is not False:
                return
            reco_id = payload.get("reco_id")
            if not reco_id or reco_id not in self.applied_ids:
                return
            before = len(self.realized)
            self.realized = [r for r in self.realized if r["reco_id"] != reco_id]
            self.applied_ids.discard(reco_id)
            if before != len(self.realized):
                print(f"[actuation] UN-BOOKED {reco_id}: {load_key} reported "
                      f"ok=false via {payload.get('source')} — saving not realized")

    def set_suppressed(self, vetoed: List, offered_rooms: Dict[str, set]) -> List[str]:
        """Replace the vetoed list. Returns ids newly suppressed since last tick.

        Only NEW suppressions are returned, because a guardrail that keeps
        vetoing the same finding every 5 s is one decision, not seven hundred —
        logging each tick would bury the demo's audit trail in repeats.
        """
        rows = []
        for f in vetoed:
            load = f.load_key.split("/")[-1]
            rows.append({
                "id": f.id, "rule_name": f.rule_name, "severity": f.severity,
                "room": f.room, "load": load,
                "usd": f.usd, "kwh": f.estimate.kwh if f.estimate else 0.0,
                "co2_kg": f.estimate.co2_kg if f.estimate else 0.0,
                "headline": f.headline,
                "gate": f.suppressed_by, "reason": f.suppressed_reason,
                "formula": f.estimate.formula if f.estimate else "",
                # When the same room still has advice the system IS willing to
                # give, name it. That turns two independent cards into one
                # visible trade-off: comfort protected here, saving taken there.
                "traded_for": sorted(offered_rooms.get(f.room, set()) - {load}),
            })
        with self.lock:
            self.suppressed = rows
            now_ids = {r["id"] for r in rows}
            fresh = sorted(now_ids - self.suppressed_ids)
            self.suppressed_ids = now_ids
            return fresh

    def realized_totals(self) -> Dict:
        with self.lock:
            anticipated = [r for r in self.realized
                           if r.get("kind") == "anticipated"]
            return {
                "usd": sum(r["usd"] for r in self.realized),
                "kwh": sum(r["kwh"] for r in self.realized),
                "co2_kg": sum(r["co2_kg"] for r in self.realized),
                "count": len(self.realized),
                # Split out so a projection is never presented as a measurement.
                "avoided_usd": sum(r["usd"] for r in anticipated),
                "avoided_count": len(anticipated),
            }

    # -- ingest ------------------------------------------------------------

    def update_room(self, room: str, payload: Dict) -> None:
        with self.lock:
            prev = self.rooms.get(room, {})
            now = payload.get("ts", time.time())

            # Track when the room was last occupied — R1 needs this.
            last_occ = prev.get("last_occupied_ts", now)
            if payload.get("occupancy"):
                last_occ = now

            # Track how long lux has been above the daylight threshold — R3.
            # A backwards or large forwards clock jump (the simulator moves the
            # virtual clock between beats) resets the window rather than billing
            # the gap as waste.
            lux_high_since = prev.get("lux_high_since", now)
            prev_ts = prev.get("ts", now)
            if payload.get("lux", 0) <= rules.DAYLIGHT_LUX_THRESHOLD or abs(now - prev_ts) > 900:
                lux_high_since = now

            # Track temperature movement for the R4 open-window heuristic.
            temp_drop = prev.get("temp_drop_c", 0.0)
            if "temp_c" in payload and "temp_c" in prev:
                temp_drop = prev["temp_c"] - payload["temp_c"]

            merged = {**prev, **payload,
                      "last_occupied_ts": last_occ,
                      "lux_high_since": lux_high_since,
                      "temp_drop_c": temp_drop}
            # Learned occupancy estimate. In the default shadow mode this only
            # ATTACHES a prediction — no rule reads it, so a wrong call cannot
            # change a recommendation or an actuation. See occupancy_model.py.
            self.rooms[room] = annotate_room(merged)

    def update_load(self, key: str, payload: Dict) -> None:
        with self.lock:
            prev = self.loads.get(key, {})
            now = payload.get("ts", time.time())
            on_since = prev.get("on_since", now)
            if prev.get("state") != "on" and payload.get("state") == "on":
                on_since = now       # rising edge
            # Re-anchor across a clock discontinuity so runtime stays plausible.
            if on_since > now or (now - on_since) > 12 * 3600:
                on_since = now
            self.loads[key] = {**prev, **payload, "on_since": on_since}

    def update_user(self, payload: Dict) -> None:
        with self.lock:
            prev = self.user
            # Only advance ts on an actual presence transition, so "how long away"
            # measures the transition, not the last heartbeat.
            ts = prev.get("ts", time.time())
            if payload.get("presence") != prev.get("presence"):
                ts = payload.get("ts", time.time())
            self.user = {**prev, **payload, "ts": ts}

    # -- derive ------------------------------------------------------------

    def total_watts(self) -> float:
        return sum(l.get("watts", 0.0) for l in self.loads.values() if l.get("state") == "on")

    def sample_power(self) -> None:
        with self.lock:
            self.power_history.append({"ts": time.time(), "watts": self.total_watts()})

    def snapshot(self) -> Dict:
        with self.lock:
            return {
                "rooms": json.loads(json.dumps(self.rooms)),
                "loads": json.loads(json.dumps(self.loads)),
                "user": dict(self.user),
                "now": time.time() + self.sim_clock_offset,
            }

    def latest_recos(self, limit: int = 12) -> List["Recommendation"]:
        """Newest-first, one card per finding id.

        STORE.recos is an append-only history: RECO_COOLDOWN_S throttles how often
        a finding is re-narrated, but once it lapses the SAME id is appended again.
        Rendering the raw tail therefore showed the same recommendation as three
        separate cards. Keep the history intact for the audit trail; collapse it
        here so each distinct finding surfaces once, with its most recent wording.

        An ANTICIPATED card is additionally dropped once its rule stops firing,
        unless it was acted on. "The dryer will hit the peak rate in 12 minutes —
        you can still shift it" is useful at 15:48 and a lie at 17:30: by then
        the money is spent and R6 is the card that applies. Detected findings are
        left alone here on purpose; their staleness is a broader open question
        (see the stale-data indicator in the session log) and this is not the
        change to settle it in.
        """
        with self.lock:
            seen, out = set(), []
            for rec in reversed(self.recos):
                if rec.id in seen:
                    continue
                seen.add(rec.id)
                if (getattr(rec, "kind", "detected") == "anticipated"
                        and rec.id not in self.active_finding_ids
                        and rec.id not in self.applied_ids):
                    continue          # the moment to act on this has passed
                out.append(rec)
                if len(out) >= limit:
                    break
            return out

    def public_state(self) -> Dict:
        snap = self.snapshot()
        now_dt = datetime.fromtimestamp(snap["now"])
        rate, period = _rate(now_dt)
        return {
            **snap,
            "total_watts": self.total_watts(),
            "power_history": list(self.power_history),
            "tariff": {"rate": rate, "period": period,
                       "clock": now_dt.strftime("%H:%M")},
            "mqtt_connected": self.mqtt_connected,
            "recos": [r.to_dict() for r in self.latest_recos(12)],
            "applied_ids": sorted(self.applied_ids),
            "suppressed": list(self.suppressed),
            "considered": self.considered,
            "realized": self.realized_totals(),
            "actuations": self.actuations[-8:][::-1],
            # Live dataset counters. Surfaced so data collection is visible while
            # it happens — a flywheel the user can watch is worth more than a
            # silent log file nobody knows is running.
            "dataset": RECORDER.stats(),
            "occupancy_model": OCCUPANCY.info(),
        }


def _rate(dt: datetime):
    from energy_model import rate_at
    return rate_at(dt)


STORE = StateStore()
LLM = LLMClient()
app = FastAPI(title="AI Home Energy Concierge")
CLIENTS: List[WebSocket] = []
LOOP: Optional[asyncio.AbstractEventLoop] = None


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    STORE.mqtt_connected = (rc == 0)
    print(f"[mqtt] connected rc={rc}")
    for topic in ("home/sensors/#", "home/loads/#", "home/context/#", "home/actuator/#"):
        client.subscribe(topic)
        print(f"[mqtt] subscribed {topic}")


def on_disconnect(client, userdata, rc, properties=None, reason=None):
    STORE.mqtt_connected = False
    print(f"[mqtt] disconnected rc={rc}")


def on_message(client, userdata, msg):
    """Never crash on a bad payload — log and continue."""
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        parts = msg.topic.split("/")

        if msg.topic.startswith("home/sensors/") and len(parts) >= 3:
            STORE.update_room(parts[2], payload)
        elif msg.topic.startswith("home/loads/") and len(parts) >= 4:
            STORE.update_load(f"{parts[2]}/{parts[3]}", payload)
        elif msg.topic.startswith("home/actuator/") and len(parts) >= 4:
            # Physical-action confirmation from the UNO Q. This is the ground
            # truth for whether an approved action actually happened, so it is
            # recorded as its own row rather than inferred from the apply.
            STORE.record_actuation(f"{parts[2]}/{parts[3]}", payload)
            RECORDER.actuation(f"{parts[2]}/{parts[3]}", payload)
            print(f"[mqtt] actuator confirmed {parts[2]}/{parts[3]} -> "
                  f"{payload.get('state')} via {payload.get('source')} "
                  f"ok={payload.get('ok')}")
        elif msg.topic == "home/context/user":
            STORE.update_user(payload)
        elif msg.topic == "home/context/clock":
            STORE.sim_clock_offset = payload.get("offset_s", 0.0)
    except Exception as exc:
        print(f"[mqtt] bad payload on {msg.topic}: {exc}")


def start_mqtt() -> Optional["mqtt.Client"]:
    if mqtt is None:
        print("[mqtt] paho-mqtt not installed — running dashboard-only")
        return None
    try:
        client = mqtt.Client(client_id="hub-orchestrator", clean_session=True)
    except Exception:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hub-orchestrator")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    try:
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    except Exception as exc:
        print(f"[mqtt] connect failed ({exc}) — will retry in background")
    client.loop_start()
    return client


MQTT_CLIENT = None


# --------------------------------------------------------------------------
# Evaluation loop
# --------------------------------------------------------------------------

def evaluation_tick() -> List[Recommendation]:
    """Run rules, narrate genuinely new findings, publish. Returns new recos."""
    STORE.sample_power()
    snap = STORE.snapshot()
    now_dt = datetime.fromtimestamp(snap["now"])

    try:
        findings, vetoed = rules.evaluate_all(snap, now_dt)
    except Exception as exc:
        print(f"[eval] rules failed: {exc}")
        return []

    # Vetoed findings are shown, never narrated: they are not advice, so they do
    # not get an LLM call, and the deterministic reason string is what the UI
    # renders. Cheaper, and it cannot drift from what the guardrail actually did.
    offered_by_room: Dict[str, set] = {}
    for f in findings:
        offered_by_room.setdefault(f.room, set()).add(f.load_key.split("/")[-1])
    STORE.considered = len(findings) + len(vetoed)
    STORE.active_finding_ids = {f.id for f in findings}
    for reco_id in STORE.set_suppressed(vetoed, offered_by_room):
        row = next((r for r in STORE.suppressed if r["id"] == reco_id), {})
        RECORDER.veto(reco_id, f"{row.get('room')}/{row.get('load')}",
                      row.get("reason", ""), row.get("gate", "comfort_guardrail"),
                      row.get("usd", 0.0))
        print(f"[veto] {reco_id}: {row.get('reason', '')}")

    fresh: List[Recommendation] = []
    now = time.time()
    for f in findings:
        # Already acted on — do not re-narrate a finding the user has resolved.
        if f.id in STORE.applied_ids:
            continue
        last = STORE.last_narrated.get(f.id, 0)
        if now - last < RECO_COOLDOWN_S:
            continue          # cooldown — this is what stops the demo flooding
        STORE.last_narrated[f.id] = now

        try:
            rec = LLM.narrate(f)
        except Exception as exc:
            print(f"[eval] narration failed hard: {exc}")
            continue

        STORE.recos.append(rec)
        fresh.append(rec)
        RECORDER.finding(rec)
        print(f"[reco] ({rec.narrated_by}) [{rec.severity}] {rec.title}  ${rec.usd:.2f}")

        if MQTT_CLIENT is not None:
            try:
                MQTT_CLIENT.publish("home/reco", json.dumps(rec.to_dict()))
            except Exception as exc:
                print(f"[mqtt] publish failed: {exc}")

    return fresh


async def broadcast(message: Dict) -> None:
    dead = []
    for ws in list(CLIENTS):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CLIENTS:
            CLIENTS.remove(ws)


async def eval_loop() -> None:
    while True:
        try:
            fresh = await asyncio.to_thread(evaluation_tick)
            # One public_state() per tick, shared by the recorder and the
            # broadcast, so the row on disk is exactly what the clients saw.
            state = STORE.public_state()
            RECORDER.tick(state)
            await broadcast({"type": "state", "data": state})
            for rec in fresh:
                await broadcast({"type": "reco", "data": rec.to_dict()})
        except Exception as exc:
            print(f"[loop] {exc}")
        await asyncio.sleep(EVAL_INTERVAL_S)


# --------------------------------------------------------------------------
# HTTP / WS
# --------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global MQTT_CLIENT, LOOP
    LOOP = asyncio.get_running_loop()
    MQTT_CLIENT = start_mqtt()
    asyncio.create_task(eval_loop())
    print(_banner())


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _banner() -> str:
    ip = _lan_ip()
    return f"""
{'=' * 66}
  AI HOME ENERGY CONCIERGE — hub running
{'=' * 66}
  Dashboard : http://{ip}:{HTTP_PORT}/
  Phone     : http://{ip}:{HTTP_PORT}/phone      <- open this on the S25
  Broker    : {MQTT_HOST}:{MQTT_PORT}
  LLM       : {os.environ.get('LLM_BASE_URL', 'http://localhost:8080/v1')}
              (LLM_ENABLED={os.environ.get('LLM_ENABLED', '1')})
  Recording : {RECORDER.path.name if RECORDER.enabled else 'OFF (RECORD_ENABLED=0)'}
  Occupancy : {_occupancy_banner()}
{'=' * 66}
"""


def _occupancy_banner() -> str:
    """One line on whether the learned tier is live, and on what terms."""
    info = OCCUPANCY.info()
    if not info["ok"]:
        return f"unavailable — {info['reason']}"
    if not info["enabled"]:
        return "loaded but OFF (OCCUPANCY_MODEL=0)"
    mode = {1: "shadow — predicts, drives nothing",
            2: "fill — supplies occupancy only when nobody else does"}
    return (f"{info['variant']} [{mode.get(info['mode'], info['mode'])}], "
            f"held-out acc {info['accuracy']}")


@app.get("/")
async def dashboard():
    return FileResponse(DASHBOARD_DIR / "index.html")


@app.get("/phone")
async def phone():
    return FileResponse(PHONE_DIR / "index.html")


@app.get("/simulator")
async def simulator():
    """Sensor simulator — stands in for hardware we do not have.

    Served from the hub on purpose: same-origin means no CORS, and the page
    falls back to location.origin, so it needs no configuration at all when
    opened at http://<hub>:8000/simulator from a phone or laptop.

    It drives the SAME endpoints a real sensor would (/api/sensor, /api/load,
    /api/presence) rather than a private back door, so the rules engine cannot
    tell the difference — which is exactly why the values it sends must stay
    labelled as simulated wherever they surface.
    """
    return FileResponse(SIMULATOR_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return JSONResponse(STORE.public_state())


@app.get("/api/recos")
async def api_recos():
    return JSONResponse([r.to_dict() for r in STORE.latest_recos(25)])


@app.post("/api/presence")
async def api_presence(body: Dict):
    """Manual presence override — the demo-safe path when geolocation is blocked."""
    presence = body.get("presence")
    if presence not in ("home", "away"):
        return JSONResponse({"error": "presence must be 'home' or 'away'"}, status_code=400)
    STORE.update_user({"presence": presence,
                       "distance_m": int(body.get("distance_m", 0 if presence == "home" else 2400)),
                       "battery": int(body.get("battery", STORE.user.get("battery", 100))),
                       "ts": time.time()})
    print(f"[api] presence -> {presence}")
    return JSONResponse({"ok": True, "user": STORE.user})


@app.post("/api/load")
async def api_load(body: Dict):
    """Toggle a load by hand — lets us drive the demo with no hardware."""
    key = body.get("key")
    state = body.get("state")
    if not key or state not in ("on", "off"):
        return JSONResponse({"error": "need key and state"}, status_code=400)
    watts = float(body.get("watts", STORE.loads.get(key, {}).get("watts", 100)))
    STORE.update_load(key, {"state": state, "watts": watts, "ts": time.time()})
    return JSONResponse({"ok": True, "loads": STORE.loads})


@app.post("/api/sensor")
async def api_sensor(body: Dict):
    """Inject a sensor reading over REST.

    Lets the whole pipeline be driven with no broker and no hardware at all —
    the last-resort demo fallback, and how the smoke test exercises the rules.
    """
    room = body.get("room", "living")
    payload = {k: v for k, v in body.items() if k != "room"}
    payload.setdefault("ts", time.time())
    STORE.update_room(room, payload)
    return JSONResponse({"ok": True, "rooms": STORE.rooms})


@app.post("/api/apply")
async def api_apply(body: Dict):
    """Approve a recommendation and command the physical actuator.

    This is the closing half of the loop (Archetype E): sense -> reason -> recommend
    -> USER APPROVES -> physically act -> confirm -> book the saving as realized.

    The comfort guardrail (R7) is enforced here as a PRE-FLIGHT GATE, not merely as
    advice. A command the guardrail would suppress is refused outright, so the system
    will not execute its own recommendation when doing so would make the home
    uncomfortable.
    """
    reco_id = body.get("reco_id")
    if not reco_id:
        return JSONResponse({"error": "reco_id required"}, status_code=400)

    rec = next((r for r in reversed(STORE.recos) if r.id == reco_id), None)
    if rec is None:
        return JSONResponse({"error": f"unknown reco_id {reco_id!r}"}, status_code=404)

    load_key = f"{rec.room}/{_load_from_rule(rec)}"
    action = body.get("action", "off")
    if action not in ("on", "off"):
        return JSONResponse({"error": "action must be 'on' or 'off'"}, status_code=400)

    # --- R7 pre-flight safety gate -------------------------------------------
    snap = STORE.snapshot()
    now_dt = datetime.fromtimestamp(snap["now"])
    allowed, reason = _guardrail_allows(snap, rec, load_key, action, now_dt)
    if not allowed:
        print(f"[apply] REFUSED {load_key} -> {action}: {reason}")
        # A refusal is a HARD NEGATIVE for the dataset: a human asked for this
        # and the deterministic gate said no. Worth more than an ignored card.
        RECORDER.refusal(reco_id, load_key, action, reason, "comfort_guardrail")
        return JSONResponse({"ok": False, "refused": True, "reason": reason,
                             "gate": "comfort_guardrail"}, status_code=409)

    # --- publish the command --------------------------------------------------
    command = {"action": action, "reco_id": reco_id,
               "approved_by": body.get("approved_by", "user"), "ts": time.time()}
    published = False
    if MQTT_CLIENT is not None:
        try:
            MQTT_CLIENT.publish(f"home/command/{load_key}", json.dumps(command))
            published = True
        except Exception as exc:
            print(f"[apply] publish failed: {exc}")

    # Reflect intent locally so the UI responds even with no board attached.
    watts = STORE.loads.get(load_key, {}).get("watts", 0.0)
    STORE.update_load(load_key, {"state": action,
                                 "watts": watts if action == "on" else 0.0,
                                 "ts": time.time()})

    STORE.book_realized(rec)
    # THE POSITIVE LABEL. A human saw this advice and acted on it — the signal
    # tools/build_dataset.py trains the relevance model against.
    RECORDER.apply(reco_id, load_key, action, body.get("approved_by", "user"),
                   round(rec.usd, 4), published)
    print(f"[apply] {load_key} -> {action} (reco {reco_id}), "
          f"realized ${rec.usd:.3f}, published={published}")

    await broadcast({"type": "state", "data": STORE.public_state()})
    return JSONResponse({"ok": True, "command": command, "load_key": load_key,
                         "published": published,
                         "realized_usd": round(rec.usd, 4)})


def _load_from_rule(rec) -> str:
    """Map a recommendation back to the load name it concerns."""
    by_rule = {
        "unoccupied_lights_on": "lights",
        "daylight_waste": "lights",
        "away_with_hvac_on": "ac",
        "hvac_with_window_open": "ac",
        "phantom_standby": "standby",
        "peak_hour_heavy_load": "dryer",
        "peak_window_imminent": "dryer",
    }
    return by_rule.get(rec.rule_name, "lights")


def _guardrail_allows(snap: Dict, rec, load_key: str, action: str, now_dt):
    """Return (allowed, reason). Refuses actions R7 would have suppressed."""
    if action == "on":
        return True, ""          # turning something on never harms comfort

    load_name = load_key.split("/")[-1]
    room = snap.get("rooms", {}).get(rec.room, {})
    temp = room.get("temp_c")

    if temp is None:
        return True, ""

    if load_name == "ac" and temp > rules.COMFORT_MAX_C:
        return False, (f"Refused: {rec.room} is {temp:.1f}°C, above the "
                       f"{rules.COMFORT_MAX_C:.0f}°C comfort limit — turning the "
                       f"A/C off would make the room uncomfortable.")
    if load_name == "heater" and temp < rules.COMFORT_MIN_C:
        return False, (f"Refused: {rec.room} is {temp:.1f}°C, below the "
                       f"{rules.COMFORT_MIN_C:.0f}°C comfort limit — turning the "
                       f"heater off would make the room uncomfortable.")
    return True, ""


@app.post("/api/clock")
async def api_clock(body: Dict):
    """Move the virtual clock, so tariff-dependent rules can be demoed on demand.

    R8 only speaks in the 30 minutes before the on-peak boundary, and R6 only
    inside the window. Without this, showing either one means waiting for the
    actual wall clock to reach 15:30 — fine for a rehearsal, useless at a demo
    table with a five-minute slot.

    The offset already existed but was reachable only by publishing
    `home/context/clock` over MQTT, which means the one rule that most needs
    driving was the one rule the browser simulator could not drive. Same
    reasoning as /api/sensor and /api/load: the no-broker path has to be able to
    exercise the whole engine.

        {"time": "15:48"}   set the virtual clock to this time today
        {"offset_s": 3600}  shift it by this many seconds
        {"reset": true}     back to real time
    """
    if body.get("reset"):
        STORE.sim_clock_offset = 0.0
    elif "offset_s" in body:
        try:
            STORE.sim_clock_offset = float(body["offset_s"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "offset_s must be a number"}, status_code=400)
    elif "time" in body:
        try:
            hh, mm = str(body["time"]).split(":")
            target = datetime.now().replace(hour=int(hh), minute=int(mm),
                                            second=0, microsecond=0)
        except Exception:
            return JSONResponse({"error": "time must be 'HH:MM'"}, status_code=400)
        STORE.sim_clock_offset = target.timestamp() - time.time()
    else:
        return JSONResponse({"error": "need time, offset_s or reset"}, status_code=400)

    now_dt = datetime.fromtimestamp(time.time() + STORE.sim_clock_offset)
    rate, period = _rate(now_dt)
    print(f"[api] virtual clock -> {now_dt.strftime('%H:%M')} ({period})")
    return JSONResponse({"ok": True, "clock": now_dt.strftime("%H:%M"),
                         "period": period, "rate": rate,
                         "offset_s": round(STORE.sim_clock_offset, 1)})


@app.post("/api/feedback")
async def api_feedback(body: Dict):
    """Explicit thumbs on a recommendation — a deliberate label.

    An approval is a positive and a guardrail refusal is a hard negative, but
    the common case is a card that is simply never touched, and 'ignored' is a
    weak signal: it could mean wrong, or badly timed, or the user was not
    looking. This endpoint lets someone say which, in one tap.

    Deliberately permissive about `reco_id` — it records a judgement about a
    finding that may already have aged out of STORE.recos, and losing the label
    because the card scrolled away would defeat the point.
    """
    reco_id = body.get("reco_id")
    if not reco_id:
        return JSONResponse({"error": "reco_id required"}, status_code=400)
    if "useful" not in body:
        return JSONResponse({"error": "useful must be true or false"}, status_code=400)

    useful = bool(body["useful"])
    RECORDER.feedback(reco_id, useful, str(body.get("note", "")),
                      str(body.get("source", "dashboard")))
    print(f"[feedback] {reco_id} -> {'useful' if useful else 'not useful'}")
    return JSONResponse({"ok": True, "dataset": RECORDER.stats()})


@app.get("/api/dataset")
async def api_dataset():
    """What has been collected this session. Read by the dashboard tile."""
    return JSONResponse(RECORDER.stats())


@app.post("/api/deep_report")
async def api_deep_report():
    try:
        from cloud_report import generate_report
        report = await asyncio.to_thread(generate_report, STORE.public_state())
        return JSONResponse(report)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.append(ws)
    try:
        await ws.send_json({"type": "state", "data": STORE.public_state()})
        while True:
            await ws.receive_text()   # keepalive; we do not expect real input
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in CLIENTS:
            CLIENTS.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="warning")
