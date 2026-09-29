// IMM-OS external sensor board firmware: GNSS (DFRobot TEL0157) + Geiger counter (DFRobot SEN0463).
//
//   I2C (GPIO21 SDA, GPIO22 SCL): TEL0157 GNSS at 0x20 (register map as in DFRobot_GNSS)
//   GPIO4: SEN0463 pulse output, one pulse per detected particle (M4011 tube)
//
// Once a second it produces one JSON line, sent over USB serial (115200) and served over Wi-Fi:
//   {"ms":61000,
//    "geiger":{"cpm":18,"usv_h":0.117,"counts":1203,"window_s":60,"warming":0},
//    "gnss":{"fix":1,"sats":9,"lat":19.0760,"lon":72.8777,"alt_m":14.2,"sog_kn":0.1,"cog_deg":0,"utc":"2026-09-29T08:30:01Z"},
//    "board":{"uptime_s":61,"reset_reason":1,"boot_count":3,"i2c_err":0,"rssi_dbm":-58}}   (board every 10 s)
//   Lines starting with '#' are diagnostics.
//
// Wi-Fi (station mode, credentials stored in flash, never in this file):
//   http://<board-ip>/       live dashboard page       http://<board-ip>/json   the latest line
//   The Pi's external_board_bridge.py reads /json (or the USB serial line) and publishes to IMM-OS.
//
// Commands over USB serial (a line):
//   WIFI_SSID <network name>   then   WIFI_PASS <password>   store Wi-Fi (flash) and connect
//   WIFI_OFF                   forget them
//   STATUS                   what was found, Wi-Fi state and address
//
// Radiation: µSv/h = CPM / 153.8 (M4011 tube, Cs-137 calibration, as DFRobot uses). CPM is a
// 60 s moving window; for the first minute it is extrapolated and marked warming.
//
// Fault tolerance (as the internal board): task watchdog (10 s), reset reason + boot counter,
// I2C error count and bus recovery, the GNSS looked for again every 30 s if missing.
#include <Arduino.h>
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
static double degrees(const uint8_t* b) {
  const uint32_t frac = ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 8) | b[4];
  return (double)b[0] + ((double)b[1] + frac / 100000.0) / 60.0;
}

struct Fix {
  bool ok, fix;
  int sats;
  double lat, lon;
  float alt, sog, cog;
  char utc[32];
};

static Fix gnssPoll() {
  Fix f = {};
  uint8_t t[7], la[6], lo[6], ld, od, st, al[3], sg[3], cg[3];
  if (!gnssFound || !gnssRead(G_YEAR_H, t, 7) || !gnssRead(G_LAT_1, la, 6) || !gnssRead(G_LAT_DIS, &ld, 1) ||
      !gnssRead(G_LON_1, lo, 6) || !gnssRead(G_LON_DIS, &od, 1) || !gnssRead(G_USE_STAR, &st, 1) ||
      !gnssRead(G_ALT_H, al, 3) || !gnssRead(G_SOG_H, sg, 3) || !gnssRead(G_COG_H, cg, 3))
    return f;
  f.ok = true;
  f.sats = st;
  f.lat = degrees(la) * (ld == 'S' ? -1 : 1);
  f.lon = degrees(lo) * (od == 'W' ? -1 : 1);
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
static void i2cRecover(bool quiet = false) {
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
static char lastLine[768] = "{}";

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

static void wifiStart() {
  if (!wifiSsid[0]) return;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setHostname("imm-external");
  WiFi.begin(wifiSsid, wifiPass);
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
    char b[200];
    snprintf(b, sizeof b, "gnss=%s wifi=%s ip=%s boot=%lu reset_reason=%d i2c_err=%lu",
             gnssFound ? "0x20" : "none", wifiSsid[0] ? (WiFi.status() == WL_CONNECTED ? "connected" : "connecting") : "off",
             WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "-", (unsigned long)bootCount, resetReason,
             (unsigned long)i2cErr);
    diag(b);
  } else if (cmd[0]) {
    diag("unknown command (WIFI_SSID <name>, WIFI_PASS <password>, WIFI_OFF, STATUS)");
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
  pinMode(PIN_GEIGER, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_GEIGER), onGeigerPulse, FALLING);
  diag("IMM-OS external board started (GNSS + Geiger)");
  if (!gnssInit()) diag("gnss: no TEL0157 at 0x20 (check SDA 21 / SCL 22 and its power)");
  wifiStart();
  web.on("/", [] { web.send(200, "text/html", PAGE); });
  web.on("/json", [] { web.sendHeader("Access-Control-Allow-Origin", "*"); web.send(200, "application/json", lastLine); });
  web.begin();
  lastProbe = lastSample = millis();
  watchdogStart();
}

void loop() {
  esp_task_wdt_reset();
  web.handleClient();
  while (Serial.available()) {
    const int c = Serial.read();
    if (c == '\n' || c == '\r') { cmd[cmdLen] = 0; handleCommand(cmd); cmdLen = 0; }
    else if (cmdLen < (int)sizeof(cmd) - 1) cmd[cmdLen++] = (char)c;   // SSID and password keep their case
  }
  const uint32_t now = millis();
  if (i2cStreak >= I2C_STUCK_STREAK && now - lastRecovery >= 10000) { lastRecovery = now; i2cRecover(); }
  if (!gnssFound && now - lastProbe >= REPROBE_MS) { lastProbe = now; if (!gnssInit()) i2cRecover(true); }
  if (now - lastSample < PERIOD_MS) return;
  lastSample += PERIOD_MS;
  if (now - lastSample > PERIOD_MS) lastSample = now;       // don't try to catch up after a long stall
  geigerSecond();

  char line[768];
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
    if (WiFi.status() == WL_CONNECTED) n += snprintf(line + n, sizeof line - n, ",\"rssi_dbm\":%d", (int)WiFi.RSSI());
    n += snprintf(line + n, sizeof line - n, "}");
  }
  snprintf(line + n, sizeof line - n, "}");
  Serial.println(line);
  snprintf(lastLine, sizeof lastLine, "%s", line);
}
