// IMM-OS external sensor board firmware: GNSS (DFRobot TEL0157) + Geiger counter (DFRobot SEN0463).
//
//   I2C (GPIO21 SDA, GPIO22 SCL): TEL0157 GNSS at 0x20 (register map as in DFRobot_GNSS)
//   GPIO4: SEN0463 pulse output, one pulse per detected particle (M4011 tube)
//
// Once a second it produces one JSON line, sent over USB serial (115200) and served over Wi-Fi:
//   {"ms":61000,
//    "geiger":{"cpm":18,"usv_h":0.117,"counts":1203,"window_s":60,"warming":0},
//    "gnss":{"fix":1,"sats":9,"lat":19.0760,"lon":72.8777,"alt_m":14.2,"sog_kn":0.1,"cog_deg":0,"utc":"2026-09-29T08:30:01Z"},
//    "board":{"uptime_s":61,"reset_reason":1,"boot_count":3,"i2c_err":0,
//             "heal_cause":0,"heal_reboots":0,"heap_free":201234,"heap_min":187654,
//             "wifi_drops":0,"wifi_reason":0,"net_restarts":0,"rssi_dbm":-58}}   (board every 10 s)
//      Same board-health fields as the internal board (esp32-sensors), so the MCC shows both alike.
//      heal_cause: why THIS boot happened when the firmware rebooted itself (0 it didn't,
//      2 Wi-Fi lost, 3 nobody polling, 4 memory low); heal_reboots: total self-heal reboots since
//      flashing; wifi_reason: why the Wi-Fi link last dropped (ESP-IDF wifi_err_reason_t).
//   Lines starting with '#' are diagnostics.
//
// Wi-Fi (station mode, credentials stored in flash, never in this file):
//   http://<board-ip>/       live dashboard page       http://<board-ip>/json   the latest line
//   Announced over mDNS as "imm-external": http://imm-external.local/json works on any router.
//   The Pi's external_board_bridge.py reads /json (or the USB serial line) and publishes to IMM-OS.
//
// Commands over USB serial (a line):
//   WIFI_SSID <network name>   then   WIFI_PASS <password>   store Wi-Fi (flash) and connect
//   WIFI_OFF                   forget them
//   STATUS                   what was found, Wi-Fi state and address
//   USB_HOST                 (sent by the Pi every 20 s while it reads this USB cable; silent)
//
// Radiation: µSv/h = CPM / 153.8 (M4011 tube, Cs-137 calibration, as DFRobot uses). CPM is a
// 60 s moving window; for the first minute it is extrapolated and marked warming.
//
// Fault tolerance (as the internal board): task watchdog (10 s), reset reason + boot counter,
// I2C error count and bus recovery, the GNSS looked for again every 30 s if missing, and
// network self-recovery (the task watchdog can't see it: the loop runs on while the board is
// unreachable). Without rebooting first, so the gap is only the outage itself:
//   Wi-Fi down 20 s              → rejoin the network (every 20 s)
//   Wi-Fi down 3 min             → reboot (once per outage: a router that is off is waited for)
//   connected, not polled 5 min  → restart the web server and mDNS name
//   not polled 15 min            → reboot (once, until the Pi polls again)
//   free memory < 16 KB          → reboot before the network stack runs out
// Every self-heal reboot is remembered across it and reported as heal_cause. While the Pi reads
// the USB cable too (USB_HOST), the Wi-Fi and not-polled reboots are skipped.
//
// Arduino IDE: the same code is arduino/imm_external/imm_external.ino (board "ESP32 Dev Module",
// no libraries needed). Keep the two identical: tests/test_esp32_external.py checks it.
#include <Arduino.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_idf_version.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

static const int PIN_SDA = 21, PIN_SCL = 22, PIN_GEIGER = 4;
static const uint32_t PERIOD_MS = 1000, REPROBE_MS = 30000, BOARD_MS = 10000;
static const uint32_t WDT_TIMEOUT_S = 10;
static const int I2C_STUCK_STREAK = 5;         // one device on this bus: 5 failed polls in a row
static const float CPM_PER_USVH = 153.8f;        // M4011
static const uint32_t PULSE_DEADTIME_US = 150;   // shorter gaps are ringing, not a second particle
static const char* const HOSTNAME = "imm-external";

// One GNSS reading. Declared before any function: the Arduino IDE puts its generated function
// prototypes above the first function, and gnssPoll()'s must see this type.
struct Fix {
  bool ok, fix;
  int sats;
  double lat, lon;
  float alt, sog, cog;
  char utc[32];
};

// Network self-recovery (as esp32-sensors): the Pi polls /json every second, so a board it can't
// reach is losing data while the loop (and the task watchdog) carry on as if all were well.
static const uint32_t WIFI_REJOIN_MS = 20000, WIFI_REBOOT_MS = 180000;
static const uint32_t POLL_RESTART_MS = 300000, POLL_REBOOT_MS = 900000;
static const uint32_t HEAP_REBOOT_BYTES = 16384, HEAP_CHECK_AFTER_MS = 600000;
enum HealCause { HEAL_NONE = 0, HEAL_WIFI = 2, HEAL_POLL = 3, HEAL_HEAP = 4 };   // 1 (I2C stall) is the internal board's
static int healCause = HEAL_NONE;                // why this boot happened, if the firmware rebooted itself
static uint32_t healReboots = 0;                 // self-heal reboots since flashing (flash)
static uint32_t wifiDownSince = 0, lastRejoin = 0, lastPollMs = 0;
static bool wifiWasUp = false, wifiWait = false, pollWait = false, pollRestarted = false, heapRebooted = false;
static uint32_t wifiDrops = 0, netRestarts = 0;
static const uint32_t USB_HOST_TIMEOUT_MS = 60000;   // USB_HOST heard this recently: the Pi reads our USB
static uint32_t lastUsbHostMs = 0;
static bool usbHostSeen = false;
static bool usbHostReading(uint32_t now) { return usbHostSeen && now - lastUsbHostMs < USB_HOST_TIMEOUT_MS; }
static volatile int wifiReason = 0;              // latest Wi-Fi disconnect reason (wifi_err_reason_t)
static int dropReason = 0;                       // the reason the link last went down (not our own rejoins)

// ── board health (same scheme as esp32-sensors) ──────────────────────
static int resetReason = 0;
static uint32_t bootCount = 0, i2cErr = 0;
static int i2cStreak = 0, i2cRecoveries = 0;
static bool i2cQuiet = false;
static uint32_t lastRecovery = 0;

static void diag(const char* msg) { Serial.print("# "); Serial.println(msg); }

static void i2cResult(bool ok) {
  if (ok) { i2cStreak = 0; return; }
  if (i2cQuiet) return;
  ++i2cErr;
  ++i2cStreak;
}

// ── TEL0157 GNSS (DFRobot_GNSS register map) ─────────────────────────
static const uint8_t GNSS_ADDR = 0x20;
enum : uint8_t {
  G_YEAR_H = 0, G_LAT_1 = 7, G_LON_DIS = 12, G_LON_1 = 13, G_LAT_DIS = 18, G_USE_STAR = 19,
  G_ALT_H = 20, G_SOG_H = 23, G_COG_H = 26, G_GNSS_MODE = 34, G_SLEEP = 35,
};
static bool gnssFound = false;

static bool gnssRead(uint8_t reg, uint8_t* out, uint8_t n) {
  Wire.beginTransmission(GNSS_ADDR);
  Wire.write(reg);
  bool ok = Wire.endTransmission() == 0 && Wire.requestFrom(GNSS_ADDR, n) == n;
  if (ok) for (uint8_t i = 0; i < n; i++) out[i] = (uint8_t)Wire.read();
  i2cResult(ok);
  return ok;
}

static bool gnssWrite(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(GNSS_ADDR);
  Wire.write(reg);
  Wire.write(v);
  const bool ok = Wire.endTransmission() == 0;
  i2cResult(ok);
  return ok;
}

static bool gnssInit() {
  i2cQuiet = true;
  Wire.beginTransmission(GNSS_ADDR);
  gnssFound = Wire.endTransmission() == 0;
  if (gnssFound) {
    gnssWrite(G_SLEEP, 0);            // power on the receiver
    delay(50);
    gnssWrite(G_GNSS_MODE, 7);        // GPS + BeiDou + GLONASS
    delay(50);
  }
  i2cQuiet = false;
  return gnssFound;
}

// three bytes: 15-bit integer part (bit 7 of the first = negative), hundredths
static float signed3(const uint8_t* b) {
  const float v = (float)(((b[0] & 0x7F) << 8) | b[1]) + b[2] / 100.0f;
  return (b[0] & 0x80) ? -v : v;      // (DFRobot's library ignores the sign bit)
}

// DD, MM, MMMMM (3 bytes: 1/100000 minute) → decimal degrees
static double dmToDegrees(const uint8_t* b) {
  const uint32_t frac = ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 8) | b[4];
  return (double)b[0] + ((double)b[1] + frac / 100000.0) / 60.0;
}

static Fix gnssPoll() {
  Fix f = {};
  uint8_t t[7], la[6], lo[6], ld, od, st, al[3], sg[3], cg[3];
  if (!gnssFound || !gnssRead(G_YEAR_H, t, 7) || !gnssRead(G_LAT_1, la, 6) || !gnssRead(G_LAT_DIS, &ld, 1) ||
      !gnssRead(G_LON_1, lo, 6) || !gnssRead(G_LON_DIS, &od, 1) || !gnssRead(G_USE_STAR, &st, 1) ||
      !gnssRead(G_ALT_H, al, 3) || !gnssRead(G_SOG_H, sg, 3) || !gnssRead(G_COG_H, cg, 3))
    return f;
  f.ok = true;
  f.sats = st;
  f.lat = dmToDegrees(la) * (ld == 'S' ? -1 : 1);
  f.lon = dmToDegrees(lo) * (od == 'W' ? -1 : 1);
  f.fix = st > 0 && (ld == 'N' || ld == 'S') && (od == 'E' || od == 'W') && (f.lat != 0 || f.lon != 0);
  f.alt = signed3(al);
  f.sog = signed3(sg);
  f.cog = signed3(cg);
  const int year = t[0] << 8 | t[1];
  if (year >= 2020 && year < 2100 && t[2] >= 1 && t[2] <= 12 && t[3] >= 1 && t[3] <= 31)
    snprintf(f.utc, sizeof f.utc, "%04d-%02d-%02dT%02d:%02d:%02dZ", year, t[2], t[3], t[4], t[5], t[6]);
  return f;
}

// ── SEN0463 Geiger counter ───────────────────────────────────────────
static volatile uint32_t geigerPulses = 0;
static volatile uint32_t lastPulseUs = 0;
static uint16_t bucket[60] = {0};                 // pulses per second, last 60 s
static int bucketPos = 0, bucketsFilled = 0;
static uint32_t geigerTotal = 0, geigerSeen = 0;

void IRAM_ATTR onGeigerPulse() {
  const uint32_t now = micros();
  if (now - lastPulseUs >= PULSE_DEADTIME_US) geigerPulses = geigerPulses + 1;
  lastPulseUs = now;
}

static void geigerSecond() {                     // once per second: move the window on
  noInterrupts();
  const uint32_t total = geigerPulses;
  interrupts();
  const uint32_t n = total - geigerSeen;
  geigerSeen = total;
  geigerTotal += n;
  bucket[bucketPos] = (uint16_t)(n > 65535 ? 65535 : n);
  bucketPos = (bucketPos + 1) % 60;
  if (bucketsFilled < 60) bucketsFilled++;
}

static float geigerCpm() {
  uint32_t sum = 0;
  for (int i = 0; i < 60; i++) sum += bucket[i];
  return bucketsFilled ? sum * 60.0f / bucketsFilled : 0;
}

// ── I2C bus recovery (as esp32-sensors) ──────────────────────────────
// quiet = only act if SDA is actually held low (used when a missing GNSS is looked for again:
// a bus stuck since boot produces no failed reads, because nothing is read)
static void i2cRecover(bool quiet) {
  Wire.end();
  pinMode(PIN_SDA, INPUT_PULLUP);
  const bool held = digitalRead(PIN_SDA) == LOW;
  if (quiet && !held) { Wire.begin(PIN_SDA, PIN_SCL); Wire.setClock(100000); Wire.setTimeOut(50); return; }
  pinMode(PIN_SCL, OUTPUT_OPEN_DRAIN);
  for (int i = 0; i < 9 && digitalRead(PIN_SDA) == LOW; i++) {
    digitalWrite(PIN_SCL, LOW);  delayMicroseconds(5);
    digitalWrite(PIN_SCL, HIGH); delayMicroseconds(5);
  }
  pinMode(PIN_SDA, OUTPUT_OPEN_DRAIN);
  digitalWrite(PIN_SDA, LOW);  delayMicroseconds(5);
  digitalWrite(PIN_SCL, HIGH); delayMicroseconds(5);
  digitalWrite(PIN_SDA, HIGH); delayMicroseconds(5);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  Wire.setTimeOut(50);
  ++i2cRecoveries;
  i2cStreak = 0;
  diag("i2c: every transaction failing: bus recovered, GNSS set up again");
  gnssInit();
}

static void watchdogStart() {
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = WDT_TIMEOUT_S * 1000;
  cfg.idle_core_mask = 0;
  cfg.trigger_panic = true;
  if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) esp_task_wdt_init(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
}

// ── Wi-Fi + dashboard ────────────────────────────────────────────────
Preferences prefs;
WebServer web(80);
static char wifiSsid[33] = "", wifiPass[65] = "";
static char lastLine[1024] = "{}";

static const char PAGE[] =
    "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>IMM-OS external board</title><style>body{font-family:sans-serif;background:#0b1224;color:#e6ecff;margin:16px}"
    "h1{font-size:18px}.v{font-size:28px;font-weight:700}.c{display:inline-block;margin:8px 16px 8px 0;vertical-align:top}"
    "small{color:#8f9bb8}</style></head><body><h1>IMM-OS external board</h1><div id=d>…</div><script>"
    "async function t(){try{const j=await(await fetch('/json')).json(),g=j.geiger||{},n=j.gnss||{};"
    "document.getElementById('d').innerHTML="
    "`<div class=c><small>Radiation</small><div class=v>${g.usv_h?\?'–'} µSv/h</div><small>${g.cpm?\?'–'} CPM${g.warming?' (first minute)':''}</small></div>`+"
    "`<div class=c><small>Position</small><div class=v>${n.fix?n.lat.toFixed(5)+', '+n.lon.toFixed(5):'no fix'}</div>"
    "<small>${n.sats?\?0} satellites · alt ${n.alt_m?\?'–'} m · ${n.utc?\?''}</small></div>`}catch(e){}}"
    "t();setInterval(t,1000)</script></body></html>";

// A reboot the firmware chose: say why on USB, remember the cause across it (flash: the next boot
// reports it as heal_cause, so the data shows when and why), then restart. The Geiger window
// starts again (a minute marked warming); the GNSS keeps its fix (it stays powered).
static void healReboot(int cause, const char* why) {
  prefs.putUInt("heal", (uint32_t)cause);
  prefs.putUInt("heals", healReboots + 1);
  diag(why);
  Serial.flush();
  esp_restart();
}

// The Pi read /json: the board is reachable. Re-arm the poll recovery.
static void polled() {
  lastPollMs = millis();
  pollRestarted = false;
  if (pollWait) { pollWait = false; prefs.putUInt("poll_wait", 0); }
}

// The web server can only start once Wi-Fi has brought the network stack (lwIP) up: starting it
// earlier makes the ESP32 abort ("tcpip_api_call: Invalid mbox") and reboot, over and over.
static bool webStarted = false;

static void webStart() {
  if (webStarted) return;
  web.on("/", [] { web.send(200, "text/html", PAGE); });
  web.on("/json", [] {
    web.sendHeader("Access-Control-Allow-Origin", "*");
    web.send(200, "application/json", lastLine);
    polled();
  });
  web.begin();
  webStarted = true;
}

static void wifiStart() {
  if (!wifiSsid[0]) return;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);                 // no modem sleep: missed router beacons drop the link, and a
                                        // server that sleeps answers late; ~80 mA more, mains-powered
  WiFi.setHostname(HOSTNAME);
  WiFi.begin(wifiSsid, wifiPass);
  webStart();
}

// Announce "imm-external" on the network once Wi-Fi is up, so the Pi reaches this board as
// http://imm-external.local/json whatever IP the router gives it. Re-announced after a drop.
static bool mdnsStarted = false;
static void mdnsTick() {
  const bool up = WiFi.status() == WL_CONNECTED;
  if (up && !mdnsStarted) {
    if (MDNS.begin(HOSTNAME)) {
      MDNS.addService("http", "tcp", 80);
      mdnsStarted = true;
      diag("mdns: reachable as http://imm-external.local/json (the name works on any router)");
    }
  } else if (!up && mdnsStarted) {
    MDNS.end();
    mdnsStarted = false;
  }
}

static void networkTick(uint32_t now) {
  if (!wifiSsid[0]) return;                                    // USB only: nothing to mend
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasUp && wifiDownSince) {
      char m[96];
      snprintf(m, sizeof m, "wifi: back after %lu s", (unsigned long)((now - wifiDownSince) / 1000));
      diag(m);
    }
    wifiWasUp = true;
    wifiDownSince = 0;
    if (wifiWait) { wifiWait = false; prefs.putUInt("wifi_wait", 0); }
    // Connected, but the Pi hasn't read /json for minutes: the web server or the mDNS name
    // stopped answering (the board looks fine from here). Restart both, then reboot as a last resort.
    if (now - lastPollMs >= POLL_RESTART_MS && !pollRestarted) {
      pollRestarted = true;
      ++netRestarts;
      web.stop();
      web.begin();
      MDNS.end();
      mdnsStarted = false;                                     // mdnsTick announces the name again
      diag("self-heal: not polled for 5 min; web server and mDNS name restarted");
    }
    if (now - lastPollMs >= POLL_REBOOT_MS && !pollWait && !usbHostReading(now)) {
      pollWait = true;                                         // once, until the Pi polls again: a Pi
      prefs.putUInt("poll_wait", 1);                           // that is off must not cause a reboot loop
      healReboot(HEAL_POLL, "self-heal: still not polled after 15 min; rebooting");
      return;
    }
  } else {
    if (wifiWasUp) {
      wifiWasUp = false;
      ++wifiDrops;
      dropReason = wifiReason;
      char m[96];
      snprintf(m, sizeof m, "wifi: link lost (reason %d); rejoining", dropReason);
      diag(m);
    }
    if (!wifiDownSince) wifiDownSince = now ? now : 1;
    lastPollMs = now;                                          // not "unpolled" while the link is down
    if (now - wifiDownSince >= WIFI_REBOOT_MS && !wifiWait && !usbHostReading(now)) {
      wifiWait = true;                                         // once per outage: a router that is off
      prefs.putUInt("wifi_wait", 1);                           // is waited for, not rebooted against
      healReboot(HEAL_WIFI, "self-heal: Wi-Fi still down after 3 min; rebooting");
      return;
    }
    if (now - wifiDownSince >= WIFI_REJOIN_MS && now - lastRejoin >= WIFI_REJOIN_MS) {
      lastRejoin = now;
      WiFi.disconnect();                                       // the driver's own reconnect can give up
      WiFi.begin(wifiSsid, wifiPass);                          // after some disconnect reasons: start over
    }
  }
  if (now >= HEAP_CHECK_AFTER_MS && !heapRebooted && esp_get_free_heap_size() < HEAP_REBOOT_BYTES) {
    heapRebooted = true;
    healReboot(HEAL_HEAP, "self-heal: free memory below 16 KB; rebooting before the network stack fails");
  }
}

static void handleCommand(char* cmd) {
  for (char* p = cmd; *p && *p != ' '; p++) *p = (char)toupper(*p);   // the command word is case-insensitive
  if (strncmp(cmd, "WIFI_SSID ", 10) == 0) {                         // rest of the line: spaces allowed
    snprintf(wifiSsid, sizeof wifiSsid, "%s", cmd + 10);
    prefs.putString("ssid", wifiSsid);
    diag("wifi: network name stored; now send WIFI_PASS <password> (WIFI_PASS alone for an open network)");
  } else if (strncmp(cmd, "WIFI_PASS", 9) == 0 && (cmd[9] == ' ' || cmd[9] == 0)) {
    snprintf(wifiPass, sizeof wifiPass, "%s", cmd[9] ? cmd + 10 : "");
    prefs.putString("pass", wifiPass);
    if (!wifiSsid[0]) { diag("wifi: password stored; send WIFI_SSID <network name>"); return; }
    diag("wifi: stored, connecting");
    wifiStart();
  } else if (strcmp(cmd, "WIFI_OFF") == 0) {
    wifiSsid[0] = wifiPass[0] = 0;
    prefs.putString("ssid", "");
    prefs.putString("pass", "");
    WiFi.disconnect(true);
    diag("wifi: credentials removed");
  } else if (strcmp(cmd, "STATUS") == 0) {
    char b[240];
    snprintf(b, sizeof b, "gnss=%s wifi=%s ip=%s boot=%lu reset_reason=%d i2c_err=%lu",
             gnssFound ? "0x20" : "none", wifiSsid[0] ? (WiFi.status() == WL_CONNECTED ? "connected" : "connecting") : "off",
             WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "-", (unsigned long)bootCount, resetReason,
             (unsigned long)i2cErr);
    diag(b);
    snprintf(b, sizeof b, "network: %lu Wi-Fi drop(s) (reason %d, latest %d), %lu web/mDNS restart(s), %lu self-heal "
             "reboot(s) since flashing, this start: %d, free memory %lu B", (unsigned long)wifiDrops, dropReason, (int)wifiReason,
             (unsigned long)netRestarts, (unsigned long)healReboots, healCause, (unsigned long)esp_get_free_heap_size());
    diag(b);
    diag(usbHostReading(millis()) ? "usb: the Pi reads this board over USB too (no Wi-Fi reboots)"
                                  : "usb: the Pi isn't reading this board's USB");
  } else if (strcmp(cmd, "USB_HOST") == 0) {                         // the Pi reads this USB cable: silent
    lastUsbHostMs = millis();
    usbHostSeen = true;
  } else if (cmd[0]) {
    diag("unknown command (WIFI_SSID <name>, WIFI_PASS <password>, WIFI_OFF, STATUS, USB_HOST)");
  }
}

// ── main ─────────────────────────────────────────────────────────────
static uint32_t lastSample = 0, lastProbe = 0, lastBoard = 0;
static char cmd[112];
static int cmdLen = 0;

void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  Wire.setTimeOut(50);
  resetReason = (int)esp_reset_reason();
  prefs.begin("imm-ext", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  snprintf(wifiSsid, sizeof wifiSsid, "%s", prefs.getString("ssid", "").c_str());
  snprintf(wifiPass, sizeof wifiPass, "%s", prefs.getString("pass", "").c_str());
  {
    // A self-heal reboot is a software reset (3); anything else (power cut, RESET button) wasn't ours
    const uint32_t cause = prefs.getUInt("heal", 0);
    healCause = resetReason == 3 ? (int)cause : HEAL_NONE;
    if (cause) prefs.putUInt("heal", 0);
    healReboots = prefs.getUInt("heals", 0);
    wifiWait = prefs.getUInt("wifi_wait", 0) != 0;
    pollWait = prefs.getUInt("poll_wait", 0) != 0;
  }
  pinMode(PIN_GEIGER, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_GEIGER), onGeigerPulse, FALLING);
  diag("IMM-OS external board started (GNSS + Geiger)");
  if (healCause) {
    static const char* const why[] = {"", "I2C bus stall", "Wi-Fi lost", "not polled", "memory low"};
    char m[80];
    snprintf(m, sizeof m, "self-heal: this start was a self-heal reboot (%s)", healCause <= 4 ? why[healCause] : "?");
    diag(m);
  }
  if (!gnssInit()) diag("gnss: no TEL0157 at 0x20 (check SDA 21 / SCL 22 and its power)");
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) { wifiReason = info.wifi_sta_disconnected.reason; },
               ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  wifiStart();
  if (wifiSsid[0]) diag("wifi: connecting (STATUS shows the IP)");
  lastProbe = lastSample = millis();
  watchdogStart();
}

void loop() {
  esp_task_wdt_reset();
  if (webStarted) { web.handleClient(); mdnsTick(); networkTick(millis()); }
  while (Serial.available()) {
    const int c = Serial.read();
    if (c == '\n' || c == '\r') { cmd[cmdLen] = 0; handleCommand(cmd); cmdLen = 0; }
    else if (cmdLen < (int)sizeof(cmd) - 1) cmd[cmdLen++] = (char)c;   // SSID and password keep their case
  }
  const uint32_t now = millis();
  if (i2cStreak >= I2C_STUCK_STREAK && now - lastRecovery >= 10000) { lastRecovery = now; i2cRecover(false); }
  if (!gnssFound && now - lastProbe >= REPROBE_MS) { lastProbe = now; if (!gnssInit()) i2cRecover(true); }
  if (now - lastSample < PERIOD_MS) return;
  lastSample += PERIOD_MS;
  if (now - lastSample > PERIOD_MS) lastSample = now;       // don't try to catch up after a long stall
  geigerSecond();

  char line[1024];
  int n = snprintf(line, sizeof line, "{\"ms\":%lu", (unsigned long)now);
  const float cpm = geigerCpm();
  n += snprintf(line + n, sizeof line - n,
                ",\"geiger\":{\"cpm\":%.1f,\"usv_h\":%.3f,\"counts\":%lu,\"window_s\":%d,\"warming\":%d}",
                cpm, cpm / CPM_PER_USVH, (unsigned long)geigerTotal, bucketsFilled, bucketsFilled < 60 ? 1 : 0);
  const Fix f = gnssPoll();
  if (f.ok) {
    n += snprintf(line + n, sizeof line - n, ",\"gnss\":{\"fix\":%d,\"sats\":%d", f.fix ? 1 : 0, f.sats);
    if (f.fix)
      n += snprintf(line + n, sizeof line - n, ",\"lat\":%.6f,\"lon\":%.6f,\"alt_m\":%.2f,\"sog_kn\":%.2f,\"cog_deg\":%.2f",
                    f.lat, f.lon, f.alt, f.sog, f.cog);
    if (f.utc[0]) n += snprintf(line + n, sizeof line - n, ",\"utc\":\"%s\"", f.utc);
    n += snprintf(line + n, sizeof line - n, "}");
  }
  if (now - lastBoard >= BOARD_MS || lastBoard == 0) {
    lastBoard = now;
    n += snprintf(line + n, sizeof line - n,
                  ",\"board\":{\"uptime_s\":%lu,\"reset_reason\":%d,\"boot_count\":%lu,\"i2c_err\":%lu",
                  (unsigned long)(now / 1000), resetReason, (unsigned long)bootCount, (unsigned long)i2cErr);
    n += snprintf(line + n, sizeof line - n,
                  ",\"heal_cause\":%d,\"heal_reboots\":%lu,\"heap_free\":%lu,\"heap_min\":%lu,"
                  "\"wifi_drops\":%lu,\"wifi_reason\":%d,\"net_restarts\":%lu",
                  healCause, (unsigned long)healReboots, (unsigned long)esp_get_free_heap_size(),
                  (unsigned long)esp_get_minimum_free_heap_size(), (unsigned long)wifiDrops, dropReason,
                  (unsigned long)netRestarts);
    if (WiFi.status() == WL_CONNECTED) n += snprintf(line + n, sizeof line - n, ",\"rssi_dbm\":%d", (int)WiFi.RSSI());
    n += snprintf(line + n, sizeof line - n, "}");
  }
  snprintf(line + n, sizeof line - n, "}");
  Serial.println(line);
  snprintf(lastLine, sizeof lastLine, "%s", line);
}
