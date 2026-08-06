/*
 * AI Home Energy Concierge - MCU firmware for the Arduino UNO Q (STM32U585).
 * MODULINO / QWIIC EDITION.
 *
 * Replaces the original breadboard build (PIR on D2, photoresistor on A0,
 * DHT22 on D4, servo on D9). All sensing now comes from Modulino nodes on the
 * Qwiic connector, which on this board is I2C4 == Wire1 (MCU pins PD12/PD13).
 * Verified empirically with code/arduino/scanner/scanner.ino.
 *
 * ---------------------------------------------------------------------------
 * WIRE CONTRACT - code/arduino/uno_q_publisher.py depends on this.
 *
 *   115200 baud, one compact JSON object per '\n'-terminated line, ~1 Hz.
 *   NO BANNER on Serial. The Linux-side parser expects JSON lines only.
 *
 *   MCU -> host (telemetry, 1 Hz):
 *     {"temp_c":21.9,"occ_src":"none","lux_src":"none","temp_src":"knob_sim",...}
 *
 * IMPORTANT - PARTIAL PAYLOADS ARE DELIBERATE.
 * This sketch emits ONLY the signals it genuinely has a source for. It does
 * NOT pad the line with placeholder occupancy/lux/humidity values.
 *
 * Why: the hub merges room state with {**prev, **payload} (hub/server.py
 * update_room), so a partial payload preserves whatever another source last
 * supplied. With only a Knob attached, the phone simulator owns
 * occupancy/lux/humidity and the MCU owns temp_c - and the two never fight.
 * If this sketch padded the missing keys with zeros at 1 Hz it would
 * continuously stomp on the phone's values.
 *
 * The original firmware had the opposite bug: with USE_DHT 0 it emitted
 * 23.5 C / 45.0 % indistinguishable on the wire from real readings, which
 * silently disabled rule R4 AND the R7 comfort guardrail. Hence the *_src
 * provenance keys below - a value always says where it came from.
 * ---------------------------------------------------------------------------
 *
 * HONESTY NOTE - READ BEFORE DEMOING.
 * The Modulino Knob is a DECLARED SIMULATED thermostat dial. It is not a
 * temperature sensor. Every line carries "temp_src":"knob_sim" to say so. If a
 * Modulino Thermo node is attached it is preferred automatically and temp_src
 * becomes "hs3003" - a real measurement. Never describe the knob value as a
 * measured temperature.
 *
 * SIGNAL OWNERSHIP (auto-detected at boot; missing nodes are simply omitted):
 *   temp_c    Thermo (real) -> else Knob (declared simulation)
 *   humidity  Thermo (real) -> else omitted
 *   lux       Light  (real) -> else Knob, if the Knob is not driving temp_c
 *   occupancy Distance (real) -> else Buttons override -> else omitted
 */

/* ------------------------------------------------------------------ *
 *  Feature flags. Compiling a node IN does not require it to be present -
 *  each is auto-detected via its begin() return value, and a missing node
 *  degrades one signal without ever preventing boot.
 * ------------------------------------------------------------------ */
#define USE_KNOB      1
#define USE_THERMO    1   // real temp + humidity, preferred over the knob
#define USE_DISTANCE  1   // ToF presence
#define USE_LIGHT     1   // real lux (LTR-381RGB); not in the 7-node kit
#define USE_BUTTONS   1   // manual demo scenario driving
#define USE_BUZZER    1   // audible feedback on button actions
#define USE_PIXELS    1   // local load indicator, if a Pixels node is attached
#define USE_MOVEMENT  0   // an IMU senses the NODE being jostled, not room
                          // occupancy. Deliberately off - see the README note.

/* Actuation still lives on the network (Kasa smart bulb/plug, switched from the
 * Linux side) — the MCU cannot reach a Kasa device itself. What this flag now
 * enables is the RETURN channel: after the publisher has switched a device it
 * sends `CMD <load> <on|off> <ok|fail>` back, and the MCU lights the matching
 * button LED. Without it the LEDs could only show what was *asked for*, which
 * for a debug tool is worse than useless — it would show a lit LED for a bulb
 * that never came on.
 *
 * This is the host->MCU direction that was never verified (Risk V-2). If it
 * does not work, every press ends in the ACK_TIMEOUT_MS buzz below and the
 * LEDs stay dark — a clean negative result rather than a silent lie. */
#define MCU_ACCEPTS_COMMANDS 1

/* Buttons A and B directly toggle the two Kasa loads, as a hardware debug path
 * independent of the browser UI: press -> counter in telemetry -> publisher
 * switches the real device -> CMD comes back -> LED. */
#define BUTTON_ACTUATION 1

#include <Arduino_Modulino.h>

/* ------------------------------------------------------------------ *
 *  Timing
 * ------------------------------------------------------------------ */
const unsigned long SAMPLE_MS = 1000;   // one telemetry line per second
const unsigned long POLL_MS   = 20;     // sensor/button poll cadence

/* Occupancy hold. The original 30 s window existed because a PIR emits PULSES
 * and the window reconstructed a continuous state. A ToF reading is already
 * continuous, so this now only bridges frames where the target is out of
 * range. Note uno_q_publisher.py's OccupancyFSM applies its own 30 s grace on
 * top, so the two compose - drop this to ~4000 for a snappier demo. */
const unsigned long OCC_WINDOW_MS = 30000;

/* ------------------------------------------------------------------ *
 *  Tuning
 * ------------------------------------------------------------------ */
/* Keep consistent with hub/rules.py: COMFORT_MAX_C 27.0, COMFORT_MIN_C 16.0 */
const float   TEMP_MIN_C       = 16.0;
const float   TEMP_MAX_C       = 32.0;
const int16_t KNOB_COUNTS_FULL = 100;

/* Knob press snaps between these, straddling R7's 27 C limit so the safety
 * gate is one click away on stage. */
const float PRESET_COMFY_C = 22.0;
const float PRESET_HOT_C   = 29.5;

/* Used when the Knob drives lux instead of temp (i.e. a Thermo is attached). */
const int LUX_MAX = 2000;

const float DIST_PRESENT_MM = 1000.0;   // nearer than this => someone is there
const float DIST_MIN_MM     = 20.0;     // below this is almost certainly noise

/* ------------------------------------------------------------------ *
 *  Nodes
 * ------------------------------------------------------------------ */
#if USE_KNOB
  ModulinoKnob     knob;      bool haveKnob = false;
#endif
#if USE_THERMO
  ModulinoThermo   thermo;    bool haveThermo = false;
#endif
#if USE_DISTANCE
  ModulinoDistance distance;  bool haveDistance = false;
#endif
#if USE_LIGHT
  ModulinoLight    light;     bool haveLight = false;
#endif
#if USE_BUTTONS
  ModulinoButtons  buttons;   bool haveButtons = false;
#endif
#if USE_BUZZER
  ModulinoBuzzer   buzzer;    bool haveBuzzer = false;
#endif
#if USE_PIXELS
  ModulinoPixels   pixels;    bool havePixels = false;
#endif

/* ------------------------------------------------------------------ *
 *  State
 * ------------------------------------------------------------------ */
unsigned long lastSample = 0, lastPoll = 0, lastPresence = 0;

bool  haveTemp = false, haveHum = false, haveLux = false, haveOcc = false;
float tempC = 0.0, humidity = 0.0;
int   luxVal = 0;
bool  occupied = false;
bool  occForce = false;          // button A pins occupancy true
float lastDistMm = -1.0;

const char *tempSrc = "none";
const char *humSrc  = "none";
const char *luxSrc  = "none";
const char *occSrc  = "none";

/* Local load mirror. Driven ONLY by a CMD confirmation from the Linux side, so
 * it reflects what the Kasa device actually reported — never what was asked. */
bool lightsOn = false, acOn = false;

#if BUTTON_ACTUATION
/* Monotonic press counters, published in every telemetry line. The publisher
 * acts on the DIFFERENCE since the line it last saw, which makes the path
 * immune to a dropped line (the count still climbs) and to a repeated one (no
 * change, no action) — neither of which an edge flag would survive. They wrap
 * at 65535 harmlessly: the consumer compares for inequality, not magnitude. */
uint16_t btnLights = 0, btnAc = 0;

/* millis() of a press still awaiting its CMD confirmation; 0 = nothing pending. */
unsigned long pendLights = 0, pendAc = 0;
bool lastActionFailed = false;

/* How long to wait for the Linux side to confirm before calling it a failure.
 *
 * Generous on purpose. A toggle is: read current state (retried once) + switch
 * + settle + read back, each of which can stall on a busy hotspot — restoring
 * the bulb during bring-up took three discovery attempts before one answered.
 * Too short and the LED reports a failure for a switch that then succeeds, and
 * this exists to be TRUSTED. A late CMD still clears the error LED, so the only
 * cost of waiting is a slower verdict on a genuinely dead path. */
const unsigned long ACK_TIMEOUT_MS = 12000;
#endif

#if MCU_ACCEPTS_COMMANDS
  char    cmdBuf[64];
  uint8_t cmdLen = 0;
#endif

/* ------------------------------------------------------------------ *
 *  Discovery. Safe to call repeatedly - only retries nodes not yet found, so
 *  hot-plugging works (button C) and a present node is never re-begun.
 * ------------------------------------------------------------------ */
void discoverNodes() {
#if USE_KNOB
  if (!haveKnob) {
    haveKnob = knob.begin();
    if (haveKnob) knob.set((int16_t)(((PRESET_COMFY_C - TEMP_MIN_C) * KNOB_COUNTS_FULL)
                                     / (TEMP_MAX_C - TEMP_MIN_C)));
  }
#endif
#if USE_THERMO
  if (!haveThermo)   haveThermo   = thermo.begin();
#endif
#if USE_DISTANCE
  if (!haveDistance) haveDistance = distance.begin();
#endif
#if USE_LIGHT
  if (!haveLight)    haveLight    = light.begin();
#endif
#if USE_BUTTONS
  if (!haveButtons)  haveButtons  = buttons.begin();
#endif
#if USE_BUZZER
  if (!haveBuzzer)   haveBuzzer   = buzzer.begin();
#endif
#if USE_PIXELS
  if (!havePixels)   havePixels   = pixels.begin();
#endif
}

/* One-letter map of live nodes, for field debugging. */
void printNodes() {
  Serial.print('"');
#if USE_KNOB
  if (haveKnob)     Serial.print('K');
#endif
#if USE_THERMO
  if (haveThermo)   Serial.print('T');
#endif
#if USE_DISTANCE
  if (haveDistance) Serial.print('D');
#endif
#if USE_LIGHT
  if (haveLight)    Serial.print('L');
#endif
#if USE_BUTTONS
  if (haveButtons)  Serial.print('B');
#endif
#if USE_BUZZER
  if (haveBuzzer)   Serial.print('Z');
#endif
#if USE_PIXELS
  if (havePixels)   Serial.print('P');
#endif
  Serial.print('"');
}

void chirp(int hz) {
#if USE_BUZZER
  if (haveBuzzer) buzzer.tone(hz, 90);   // fire-and-forget; the node times it
#endif
}

/* Is the Knob free to drive lux? Only when something real owns temp_c. */
bool knobDrivesLux() {
#if USE_THERMO
  return haveThermo;
#else
  return false;
#endif
}

/* ------------------------------------------------------------------ *
 *  Sensor polling
 * ------------------------------------------------------------------ */
void pollPresence(unsigned long now) {
  bool present = false;
  haveOcc = false;
  occSrc  = "none";

#if USE_DISTANCE
  if (haveDistance) {
    haveOcc = true;
    occSrc  = "tof";
    /* available() latches the newest valid result and returns false when the
       range status is bad (get() is NAN then). Poll it, never assume. */
    if (distance.available()) {
      float mm = distance.get();
      lastDistMm = mm;
      if (!isnan(mm) && mm > DIST_MIN_MM && mm < DIST_PRESENT_MM) present = true;
    } else {
      lastDistMm = -1.0;
    }
    if (present) lastPresence = now;
    occupied = (lastPresence != 0) && ((now - lastPresence) < OCC_WINDOW_MS);
  }
#endif

  if (occForce) {          // button A override wins, and says so
    occupied = true;
    haveOcc  = true;
    occSrc   = "button_override";
  }
}

void pollTempHumidity() {
  haveTemp = false;
  haveHum  = false;
  tempSrc  = "none";
  humSrc   = "none";

#if USE_THERMO
  if (haveThermo) {
    float t = thermo.getTemperature();
    float h = thermo.getHumidity();
    /* getTemperature()/getHumidity() return 0 - NOT NaN - when the node is
       uninitialised, so a plain isnan() check would ship 0.0 C as a real
       reading. Gate on begin() plus a sanity bound. */
    if (!isnan(t) && !isnan(h) && t > -40.0f && t < 125.0f && h >= 0.0f && h <= 100.0f) {
      tempC = t;    haveTemp = true;  tempSrc = "hs3003";
      humidity = h; haveHum  = true;  humSrc  = "hs3003";
      return;
    }
    tempSrc = "hs3003_bad_read";      // node present, reading rejected
    humSrc  = "hs3003_bad_read";
    return;
  }
#endif

#if USE_KNOB
  if (haveKnob && !knobDrivesLux()) {
    int16_t c = knob.get();
    /* Clamp and write back so the physical dial cannot drift away from the
       value being reported. */
    if (c < 0)                { c = 0;                knob.set(c); }
    if (c > KNOB_COUNTS_FULL) { c = KNOB_COUNTS_FULL; knob.set(c); }
    tempC = TEMP_MIN_C + ((TEMP_MAX_C - TEMP_MIN_C) * (float)c) / (float)KNOB_COUNTS_FULL;
    haveTemp = true;
    tempSrc  = "knob_sim";            // DECLARED SIMULATION. Never claim otherwise.
  }
#endif
  /* humidity stays absent without a Thermo - the phone simulator supplies it */
}

void pollLux() {
  haveLux = false;
  luxSrc  = "none";

#if USE_LIGHT
  if (haveLight && light.update()) {
    int v = light.getLux();
    if (v < 0) v = 0;
    if (v > LUX_MAX) v = LUX_MAX;
    luxVal  = v;
    haveLux = true;
    luxSrc  = "ltr381";
    return;
  }
#endif

#if USE_KNOB
  if (haveKnob && knobDrivesLux()) {
    int16_t c = knob.get();
    if (c < 0)                { c = 0;                knob.set(c); }
    if (c > KNOB_COUNTS_FULL) { c = KNOB_COUNTS_FULL; knob.set(c); }
    luxVal  = (int)(((long)c * LUX_MAX) / KNOB_COUNTS_FULL);
    haveLux = true;
    luxSrc  = "knob_sim";
    return;
  }
#endif
  /* lux stays absent - the phone simulator supplies it */
}

/* ------------------------------------------------------------------ *
 *  Buttons.
 *
 *    A = toggle the Kasa smart bulb  ("lights")
 *    B = toggle the Kasa smart plug  ("ac" / space heater)
 *    C = rescan the Qwiic bus (hot-plug a node without rebooting)
 *
 *  A and B do NOT switch anything here — the MCU has no network. They bump a
 *  counter that rides out in the telemetry line; uno_q_publisher.py does the
 *  Kasa call and sends the result back as CMD, which is what lights the LED.
 *
 *  LED0 = bulb confirmed on     LED1 = plug confirmed on
 *  LED2 = last action failed or was never confirmed
 *
 *  Occupancy override used to live on A. It moves to the simulator UI, which
 *  is where the other declared-simulation inputs already are.
 *
 *  ModulinoButtons::update() copies its I2C read buffer into last_status[]
 *  EVEN WHEN THE READ FAILED, so a flaky or absent node can emit phantom
 *  presses. Require the same level on two consecutive polls before acting.
 * ------------------------------------------------------------------ */
void pollButtons(unsigned long now) {
#if USE_BUTTONS
  if (!haveButtons) return;

  static bool stable[3]  = { false, false, false };
  static bool pending[3] = { false, false, false };

  buttons.update();
  bool now3[3] = { buttons.isPressed(0) == HIGH,
                   buttons.isPressed(1) == HIGH,
                   buttons.isPressed(2) == HIGH };

  for (int i = 0; i < 3; i++) {
    if (now3[i] != stable[i] && now3[i] == pending[i]) {
      stable[i] = now3[i];
      if (stable[i]) {                       // confirmed rising edge
#if BUTTON_ACTUATION
        if (i == 0) {
          btnLights++; pendLights = now ? now : 1;
          lastActionFailed = false; chirp(660);   // "press taken, awaiting device"
        } else if (i == 1) {
          btnAc++;     pendAc     = now ? now : 1;
          lastActionFailed = false; chirp(660);
        } else {
          discoverNodes(); chirp(880);
        }
#else
        if (i == 0)      { occForce = !occForce; chirp(occForce ? 880 : 440); }
        else if (i == 1) { lastPresence = 0; occForce = false; chirp(440); }
        else             { discoverNodes(); chirp(880); }
#endif
      }
    }
    pending[i] = now3[i];
  }

#if BUTTON_ACTUATION
  /* Confirmed device state, not requested state — see handleCommand(). */
  buttons.setLeds(lightsOn, acOn, lastActionFailed);
#else
  buttons.setLeds(occForce, false, false);
#endif
#endif
}

void pollKnobButton() {
#if USE_KNOB
  if (!haveKnob || knobDrivesLux()) return;
  static bool prev = false;
  bool pressed = knob.isPressed();
  if (pressed && !prev) {
    /* Snap to whichever preset we are not already near - one click to cross
       R7's 27 C threshold during a demo. */
    float target = (tempC > (PRESET_COMFY_C + PRESET_HOT_C) / 2.0)
                     ? PRESET_COMFY_C : PRESET_HOT_C;
    knob.set((int16_t)(((target - TEMP_MIN_C) * KNOB_COUNTS_FULL)
                       / (TEMP_MAX_C - TEMP_MIN_C)));
    chirp(880);
  }
  prev = pressed;
#endif
}

/* ------------------------------------------------------------------ *
 *  Optional command path (compiled out by default - actuation is Kasa)
 * ------------------------------------------------------------------ */
#if MCU_ACCEPTS_COMMANDS
void renderPixels() {
#if USE_PIXELS
  if (!havePixels) return;
  for (int i = 0; i <= 3; i++) { if (lightsOn) pixels.set(i, YELLOW, 25); else pixels.clear(i); }
  for (int i = 4; i <= 7; i++) { if (acOn)     pixels.set(i, CYAN,   25); else pixels.clear(i); }
  pixels.show();
#endif
}

/* `CMD <load> <on|off> [ok|fail]`
 *
 * The optional fourth token is the honest bit. `fail` means the publisher
 * addressed a real device and it did not comply — so the mirror is deliberately
 * NOT updated and the error LED goes on. Updating it would light the bulb LED
 * for a bulb that is still dark, which is the exact failure this debug path
 * exists to catch. A CMD with no fourth token is treated as ok, so the older
 * three-token form still works. */
void handleCommand(char *line) {
  char *verb = strtok(line, " \t");
  if (verb == NULL) return;

#if BUTTON_ACTUATION
  /* `SIMBTN <a|b>` — a simulated press, for the autonomous demo script.
   *
   * Deliberately bumps the SAME counter a physical press bumps, rather than
   * shortcutting to the Kasa call. Everything downstream is then identical:
   * the counter rides out in the next telemetry line, uno_q_publisher.py sees
   * the delta, switches the real device, and writes the confirmation back which
   * lights the LED. So the autopilot exercises the real path end to end and can
   * genuinely fail if that path is broken — a script that called Kasa directly
   * would still "pass" with the button wiring dead, which would make it useless
   * as a rehearsal tool.
   *
   * The only thing it cannot reproduce is the physical switch contact and the
   * two-poll debounce above it. */
  if (strcmp(verb, "SIMBTN") == 0) {
    char *which = strtok(NULL, " \t");
    if (which == NULL) return;
    unsigned long now = millis();
    if (which[0] == 'a' || which[0] == 'A') {
      btnLights++; pendLights = now ? now : 1;
      lastActionFailed = false; chirp(660);
      Serial.println(F("{\"ack\":\"simbtn\",\"button\":\"a\",\"load\":\"lights\"}"));
    } else if (which[0] == 'b' || which[0] == 'B') {
      btnAc++;     pendAc     = now ? now : 1;
      lastActionFailed = false; chirp(660);
      Serial.println(F("{\"ack\":\"simbtn\",\"button\":\"b\",\"load\":\"ac\"}"));
    }
    return;
  }
#endif

  if (strcmp(verb, "CMD") != 0) return;
  char *load  = strtok(NULL, " \t");
  char *state = strtok(NULL, " \t");
  if (load == NULL || state == NULL) return;
  char *okTok = strtok(NULL, " \t");

  bool turnOn = (strcmp(state, "on") == 0);
  bool failed = (okTok != NULL && strcmp(okTok, "fail") == 0);
  bool known  = true;

  if (strcmp(load, "lights") == 0) {
    if (!failed) lightsOn = turnOn;
#if BUTTON_ACTUATION
    pendLights = 0;
#endif
  } else if (strcmp(load, "ac") == 0) {
    if (!failed) acOn = turnOn;
#if BUTTON_ACTUATION
    pendAc = 0;
#endif
  } else {
    known = false;
  }

#if BUTTON_ACTUATION
  if (known) lastActionFailed = failed;
#endif

  renderPixels();
  chirp(!known ? 196 : (failed ? 196 : (turnOn ? 880 : 440)));

  Serial.print(F("{\"ack\":\""));      Serial.print(load);
  Serial.print(F("\",\"state\":\""));  Serial.print(turnOn ? F("on") : F("off"));
  Serial.print(F("\",\"ok\":"));       Serial.print(known ? F("true") : F("false"));
  Serial.println(F(",\"via\":\"pixels\"}"));
}

void pollCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) { cmdBuf[cmdLen] = '\0'; handleCommand(cmdBuf); cmdLen = 0; }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdLen = 0;                       // overlong line: drop, never overflow
    }
  }
}
#endif

/* ------------------------------------------------------------------ *
 *  setup / loop
 * ------------------------------------------------------------------ */
void setup() {
  Serial.begin(115200);

  /* ARDUINO_UNO_Q makes this default to Wire1 (the Qwiic bus), but pass it
     explicitly so the sketch stays correct on a platform build where that
     define is missing. */
  Modulino.begin(Wire1);
  discoverNodes();

  /* Prime the first readings so the very first telemetry line is meaningful. */
  pollTempHumidity();
  pollLux();

  /* NO BANNER. The Linux parser expects JSON lines only. Node presence is
     reported inside each telemetry line via "nodes". */
}

void loop() {
  unsigned long now = millis();

#if MCU_ACCEPTS_COMMANDS
  pollCommands();
#endif

  /* Modulino reads are real I2C transactions at 100 kHz, not free digitalReads.
     Polling every loop pass (as the PIR build did) would saturate the bus and
     starve Serial. 20 ms beats the ToF's own 20 ms budget and any human press. */
  if (now - lastPoll >= POLL_MS) {
    lastPoll = now;
    pollPresence(now);
    pollButtons(now);
    pollKnobButton();
  }

#if BUTTON_ACTUATION
  /* A press the Linux side never answered for. Either the publisher is not
     running, or host->MCU serial does not reach us (Risk V-2). Say so with the
     error LED rather than leaving the operator watching a dead button. */
  if (pendLights && (now - pendLights) > ACK_TIMEOUT_MS) {
    pendLights = 0; lastActionFailed = true; chirp(196);
  }
  if (pendAc && (now - pendAc) > ACK_TIMEOUT_MS) {
    pendAc = 0; lastActionFailed = true; chirp(196);
  }
#endif

  if (now - lastSample < SAMPLE_MS) return;   // non-blocking; no delay() anywhere
  lastSample = now;

  pollTempHumidity();
  pollLux();

  /* One compact JSON object per line. Only signals with a real source are
     emitted - see the PARTIAL PAYLOADS note at the top of this file. */
  Serial.print(F("{"));

  bool first = true;
  if (haveOcc) {
    Serial.print(F("\"occupancy\":")); Serial.print(occupied ? F("true") : F("false"));
    first = false;
  }
  if (haveLux) {
    if (!first) Serial.print(F(","));
    Serial.print(F("\"lux\":")); Serial.print(luxVal);
    first = false;
  }
  if (haveTemp) {
    if (!first) Serial.print(F(","));
    Serial.print(F("\"temp_c\":")); Serial.print(tempC, 1);
    first = false;
  }
  if (haveHum) {
    if (!first) Serial.print(F(","));
    Serial.print(F("\"humidity\":")); Serial.print(humidity, 1);
    first = false;
  }

  /* Provenance always present, so a consumer can tell measurement from
     simulation without guessing. */
  if (!first) Serial.print(F(","));
  Serial.print(F("\"occ_src\":\""));     Serial.print(occSrc);
  Serial.print(F("\",\"lux_src\":\""));  Serial.print(luxSrc);
  Serial.print(F("\",\"temp_src\":\"")); Serial.print(tempSrc);
  Serial.print(F("\",\"hum_src\":\""));  Serial.print(humSrc);
  Serial.print(F("\",\"dist_mm\":"));    Serial.print(lastDistMm, 0);
#if BUTTON_ACTUATION
  /* Press counters, not events: the publisher acts on the delta since the last
     line it saw, so a dropped line loses nothing and a duplicate does nothing. */
  Serial.print(F(",\"bl\":"));           Serial.print(btnLights);
  Serial.print(F(",\"ba\":"));           Serial.print(btnAc);
#endif
  Serial.print(F(",\"nodes\":"));        printNodes();
  Serial.println(F("}"));
}
