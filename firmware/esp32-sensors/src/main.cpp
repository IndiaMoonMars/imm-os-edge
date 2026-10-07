// IMM-OS ESP32 sensor board firmware.
//
// Reads the board's sensors and sends one JSON line per second over USB serial
// (115200 baud) to the Raspberry Pi, where sensor_drivers/esp32_bridge.py publishes
// each sensor to IMM-OS. The Pi handles TLS, login and the offline blackbox.
//
// Wi-Fi (optional): after WIFI_SSID / WIFI_PASS over USB (stored in flash, never in code)
// the board also serves the same line at http://<board-ip>/json and a live page at
// http://<board-ip>/, so the Pi can read it over Wi-Fi (ESP32_URL) instead of USB. USB output
// continues either way; the MQ-4 is on ADC1, which works with Wi-Fi on.
// The board announces itself over mDNS as "imm-sensors", so it is reachable as
// http://imm-sensors.local/json whatever IP the router gives it — no reserved IP, any router.
//
//   I2C (GPIO21 SDA, GPIO22 SCL, 3.3 V): BME280 (0x76/0x77), SCD40 (0x62),
//                                         BNO055 (0x28/0x29), DFRobot SEN0322 O2 (0x70-0x73)
//   MQ-4 methane: AO → divider → GPIO32 (ADC1, 0-3.1 V at the default 11 dB attenuation)
//
// Every sensor is driven at register level with only the Arduino core (no libraries).
// A sensor that is missing is left out of the output and looked for again every 30 s.
//
// Output, one line per second (sections only for sensors that answered):
//   {"ms":1000,"bme280":{"temp":24.51,"hum":41.20,"pres":1008.42},
//    "scd40":{"co2_ppm":612,"temp":25.10,"hum":40.30,"asc":0},     (every 5 s, when new;
//                                                                    asc: automatic self-calibration on/off)
//    "bno055":{"heading_deg":..,"roll_deg":..,"pitch_deg":..,"lin_acc_ms2":..,"imu_calib":3,
//              "grav_ms2":9.80,"mag_ut":42.1,"gyro_dps":0.1,"temp":26,
//              "calib_gyro":3,"calib_acc":3,"calib_mag":3,"cal_restored":1},
//                                                   (cal_restored: offsets written back from flash at start)
//    "o2":{"o2_pct":20.87,"calibrated":1},                          (calibrated: CAL_O2 was done)
//    "mq4":{"vout_mv":1240,"rs_rl":3.03,"rs_r0":1.03,"ch4_ppm":4.1,"warming":0,"calibrated":1},
//                                                   (while warming: "warm_left_s":123, no ch4_ppm)
//    "board":{"uptime_s":10,"reset_reason":1,"boot_count":7,"i2c_err":0,"bme_resets":0,
//             "heal_cause":0,"heal_reboots":0,"heap_free":201234,"heap_min":187654,
//             "wifi_drops":0,"wifi_reason":0,"net_restarts":0}}  (every 10 s)
//      heal_cause: why THIS boot happened when the firmware rebooted itself (0 it didn't,
//      1 I2C bus stall, 2 Wi-Fi lost, 3 nobody polling, 4 memory low); heal_reboots: total
//      self-heal reboots since flashing; wifi_reason: why the Wi-Fi link last dropped (ESP-IDF
//      wifi_err_reason_t: 8 left, 15 handshake timeout, 200 beacon timeout, 201 no AP found …)
// Lines starting with '#' are diagnostics.
//
// Fault tolerance:
//   - task watchdog (10 s): a loop that hangs (an I2C transaction that never ends, a bug)
//     resets the board; the reset reason and a boot counter (flash) are reported in "board",
//     so the MCC sees a crash, a watchdog reset or a brownout (supply dip) for what it is
//   - I2C bus recovery: when every transaction fails for a while (a device holding SDA low),
//     SCL is clocked 9 times to free the bus, a STOP is sent and the sensors are set up again
//   - network self-recovery (the task watchdog can't see it: the loop keeps running while the
//     board is unreachable). Without rebooting first, so the gap is only the outage itself:
//       Wi-Fi down 20 s          → rejoin the network (every 20 s)
//       Wi-Fi down 3 min         → reboot (once per outage: a router that is off is waited for)
//       connected, not polled 5 min  → restart the web server and mDNS name
//       not polled 15 min        → reboot (once, until the Pi polls again)
//       free memory < 16 KB      → reboot before the network stack runs out
//     Every self-heal reboot is remembered across it and reported as heal_cause, so the data
//     says when and why the board restarted.
//
// Warm-up, only where physics needs it:
//   - MQ-4: 3 min heater warm-up after a real power-on (or brownout). After a watchdog, crash,
//     software or EN-button reset the heater never lost its 5 V, so there is no warm-up.
//   - BNO055: once fully calibrated (3/3/3/3) its offsets are stored in flash, and written back
//     at every start: no figure-8s after a restart (the fusion still refines them as it runs).
//   - SCD40: CAL_CO2 (forced recalibration in fresh air) turns automatic self-calibration off:
//     it needs ~7 days of regular fresh air, as long as the mission, and would drift instead.
//
// Commands (a line sent to the board):
//   STATUS         which sensors were found, MQ-4 calibration
//   CAL_MQ4        in clean air, after warm-up: store the MQ-4's R0 (flash, survives power-off)
//   CAL_O2         in fresh outdoor air: tell the SEN0322 that it reads 20.9 %
//   CAL_CO2 [ppm]  after 3 min in fresh outdoor air: SCD40 forced recalibration to ppm (default 420),
//                  and automatic self-calibration off (stored in the sensor)
//   ASC_ON         automatic self-calibration back on (after the mission)
//   SCD_TEST       SCD40 self-test (10 s): tells a faulty sensor from a weak supply when CO2 reads 0
//   SCD_RESET      SCD40 factory reset: forgets forced recalibration and stored settings
//   SCD_OFF/SCD_ON stop/resume using the SCD40 (remembered): for a faulty SCD40 that hangs the I2C bus
//   CAL_BNO_CLEAR  forget the stored BNO055 calibration
//   WIFI_SSID <network name>   (the rest of the line: spaces allowed; case kept)
//   WIFI_PASS <password>       then it connects; WIFI_PASS alone for an open network
//   WIFI_OFF                   forget the Wi-Fi network
#include <Arduino.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_idf_version.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <math.h>

static const int PIN_SDA = 21, PIN_SCL = 22, PIN_MQ4 = 32;
static const uint32_t PERIOD_MS = 1000, REPROBE_MS = 30000, BOARD_MS = 10000;
// Self-heal: if every I2C sensor goes silent while the board keeps running (a dead device holding
// the shared bus — seen with a failed SCD40), recover the bus; if that doesn't bring them back,
// reboot to clear it. A reboot skips the MQ-4 warm-up (reset reason 2-8), so it costs a few seconds.
static const uint32_t STALL_RECOVER_MS = 45000, STALL_REBOOT_MS = 120000;
static uint32_t lastI2cOkMs = 0, lastStallRecover = 0;
static bool i2cEverOk = false, stallRebooted = false;
static bool scdEnabled = true;                   // SCD_OFF stops the firmware touching a faulty SCD40
// Network self-recovery: the Pi polls /json every second, so a board it can't reach is losing data
// while the loop (and the task watchdog) carry on as if all were well. Mend the link without a
// reboot where possible; reboot as the last resort, and record why (heal_cause) so the data shows it.
static const uint32_t WIFI_REJOIN_MS = 20000, WIFI_REBOOT_MS = 180000;
static const uint32_t POLL_RESTART_MS = 300000, POLL_REBOOT_MS = 900000;
static const uint32_t HEAP_REBOOT_BYTES = 16384, HEAP_CHECK_AFTER_MS = 600000;
enum HealCause { HEAL_NONE = 0, HEAL_I2C = 1, HEAL_WIFI = 2, HEAL_POLL = 3, HEAL_HEAP = 4 };
static int healCause = HEAL_NONE;                // why this boot happened, if the firmware rebooted itself
static uint32_t healReboots = 0;                 // self-heal reboots since flashing (flash)
static uint32_t wifiDownSince = 0, lastRejoin = 0, lastPollMs = 0;
static bool wifiWasUp = false, wifiWait = false, pollWait = false, pollRestarted = false, heapRebooted = false;
static uint32_t wifiDrops = 0, netRestarts = 0;
static volatile int wifiReason = 0;              // latest Wi-Fi disconnect reason (wifi_err_reason_t)
static int dropReason = 0;                       // the reason the link last went down (not our own rejoins)
static const uint32_t WDT_TIMEOUT_S = 10;
static const int I2C_STUCK_STREAK = 20;         // consecutive failed transactions before a bus recovery

// ── board health ─────────────────────────────────────────────────────
static int resetReason = 0;                      // esp_reset_reason(): 1 power-on, 4 crash, 6 task watchdog, 9 brownout …
static uint32_t bootCount = 0;                   // boots since flashing (flash)
static uint32_t i2cErr = 0;                      // failed I2C transactions with a sensor that was found
static int i2cStreak = 0, i2cRecoveries = 0;
static bool i2cQuiet = false;                    // while looking for sensors: a missing one is not an error
static uint32_t lastRecovery = 0;

static void i2cResult(bool ok) {
  if (ok) { i2cStreak = 0; return; }
  if (i2cQuiet) return;
  ++i2cErr;
  ++i2cStreak;
}

// MQ-4: 5 V heater and load circuit on the module, AO divided down to the ESP32's range
#ifndef MQ4_DIVIDER
#define MQ4_DIVIDER 2.0f              // (R_top + R_bottom) / R_bottom: 10 kΩ AO→GPIO32, 10 kΩ GPIO32→GND
#endif
static const float MQ4_VC_MV = 5000.0f;          // module supply
static const float MQ4_CLEAN_AIR_RATIO = 4.4f;   // Rs/R0 in clean air (datasheet sensitivity curve)
static const float MQ4_A = 1012.7f, MQ4_B = -2.786f;   // CH4: ppm = A * (Rs/R0)^B (curve fit)
static const uint32_t MQ4_WARMUP_MS = 180000;    // after power-up; 24-48 h burn-in before calibrating
static uint32_t mq4WarmupMs = MQ4_WARMUP_MS;     // 0 after a reset that left the heater powered (setup)
static const int MQ4_CAL_SAMPLES = 10;

// ── I2C helpers ──────────────────────────────────────────────────────
static bool probe(uint8_t addr) { Wire.beginTransmission(addr); return Wire.endTransmission() == 0; }

static bool writeBytes(uint8_t addr, const uint8_t* data, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(data, n);
  const bool ok = Wire.endTransmission() == 0;
  i2cResult(ok);
  return ok;
}

static bool writeReg(uint8_t addr, uint8_t reg, uint8_t value) {
  const uint8_t b[2] = {reg, value};
  return writeBytes(addr, b, 2);
}

static bool readBytes(uint8_t addr, uint8_t* out, size_t n) {
  const bool ok = Wire.requestFrom(addr, (uint8_t)n) == n;
  i2cResult(ok);
  if (!ok) return false;
  for (size_t i = 0; i < n; i++) out[i] = (uint8_t)Wire.read();
  return true;
}

static bool readRegs(uint8_t addr, uint8_t reg, uint8_t* out, size_t n) {
  if (!writeBytes(addr, &reg, 1)) return false;
  return readBytes(addr, out, n);
}

static void diag(const char* msg) { Serial.print("# "); Serial.println(msg); }

// ── BME280 (Bosch datasheet, integer compensation) ───────────────────
struct Bme280 {
  uint8_t addr = 0;
  uint16_t T1, P1;
  int16_t T2, T3, P2, P3, P4, P5, P6, P7, P8, P9, H2, H4, H5;
  uint8_t H1, H3;
  int8_t H6;
} bme;
static const char* bmeWhy = "";         // why the last bmeRead() failed (diagnostics)
static char bmeWhyBuf[96];
static int bmeResets = 0;               // times the chip had lost its settings (a power-on reset) since start
static bool bmeSeenOk = false;          // settings seen in place at least once (a later loss is a reset)

// Write a register and read it back: the BME280 ignores writes while it is still starting up
static bool writeRegVerified(uint8_t addr, uint8_t reg, uint8_t value, uint8_t mask) {
  for (int i = 0; i < 3; i++) {
    uint8_t got;
    if (writeReg(addr, reg, value) && readRegs(addr, reg, &got, 1) && (got & mask) == (value & mask)) return true;
    delay(10);
  }
  return false;
}

static bool bmeInit() {
  bme.addr = 0;
  for (uint8_t a : {(uint8_t)0x76, (uint8_t)0x77}) {
    uint8_t id;
    if (!readRegs(a, 0xD0, &id, 1)) continue;
    if (id == 0x58) { diag(a == 0x76 ? "bme280: 0x76 is a BMP280 (no humidity)" : "bme280: 0x77 is a BMP280 (no humidity)"); continue; }
    if (id != 0x60) continue;
    writeReg(a, 0xE0, 0xB6);                                    // soft reset
    delay(10);
    for (int i = 0; i < 50; i++) { uint8_t st; if (readRegs(a, 0xF3, &st, 1) && !(st & 0x01)) break; delay(2); }
    uint8_t c[26], e[7];
    if (!readRegs(a, 0x88, c, 26) || !readRegs(a, 0xE1, e, 7)) { diag("bme280: calibration read failed"); return false; }
    if ((c[0] | c[1] << 8) == 0 || (c[6] | c[7] << 8) == 0) { diag("bme280: calibration reads as zero"); return false; }
    bme.T1 = c[0] | c[1] << 8;  bme.T2 = (int16_t)(c[2] | c[3] << 8);  bme.T3 = (int16_t)(c[4] | c[5] << 8);
    bme.P1 = c[6] | c[7] << 8;  bme.P2 = (int16_t)(c[8] | c[9] << 8);  bme.P3 = (int16_t)(c[10] | c[11] << 8);
    bme.P4 = (int16_t)(c[12] | c[13] << 8); bme.P5 = (int16_t)(c[14] | c[15] << 8); bme.P6 = (int16_t)(c[16] | c[17] << 8);
    bme.P7 = (int16_t)(c[18] | c[19] << 8); bme.P8 = (int16_t)(c[20] | c[21] << 8); bme.P9 = (int16_t)(c[22] | c[23] << 8);
    bme.H1 = c[25];
    bme.H2 = (int16_t)(e[0] | e[1] << 8);
    bme.H3 = e[2];
    bme.H4 = (int16_t)((int8_t)e[3] * 16 + (e[4] & 0x0F));
    bme.H5 = (int16_t)((int8_t)e[5] * 16 + (e[4] >> 4));
    bme.H6 = (int8_t)e[6];
    bool ok = writeRegVerified(a, 0xF2, 0x01, 0x07);            // humidity x1 (before ctrl_meas)
    ok = writeRegVerified(a, 0xF5, 0xA0, 0xFC) && ok;           // 1 s standby, filter off
    ok = writeRegVerified(a, 0xF4, 0x24, 0xFF) && ok;           // temp x1, pressure x1, sleep (bmeRead forces each one)
    if (!ok) diag("bme280: settings did not stick (ctrl registers read back differently)");
    bme.addr = a;
    return true;
  }
  return false;
}

// Forced mode: every reading sends the settings and starts one measurement. A BME280 whose
// supply dips resets to sleep with its settings cleared; in normal mode it would then never
// measure again, in forced mode the next reading simply works. Such resets are counted.
static bool bmeRead(float& temp, float& hum, float& pres) {
  uint8_t d[8], cm;
  if (!bme.addr) { bmeWhy = "not initialised"; return false; }
  if (!readRegs(bme.addr, 0xF4, &cm, 1)) { bmeWhy = "settings read failed (I2C)"; return false; }
  if (bmeSeenOk && (cm & 0xFC) != 0x24) {                       // oversampling bits gone: the chip was reset
    ++bmeResets;
    if (bmeResets == 1 || bmeResets % 10 == 0) {
      char m[150];
      snprintf(m, sizeof m, "bme280: chip reset %d time(s) since start (settings lost: a dip in its 3.3 V? "
                            "check its VCC/GND wiring); settings sent again", bmeResets);
      diag(m);
    }
  }
  if (!writeReg(bme.addr, 0xF2, 0x01) || !writeReg(bme.addr, 0xF4, 0x25)) { bmeWhy = "settings write failed (I2C)"; return false; }
  delay(10);                                                    // one x1/x1/x1 measurement: 9.3 ms max
  for (int i = 0; i < 20; i++) { uint8_t st; if (readRegs(bme.addr, 0xF3, &st, 1) && !(st & 0x08)) break; delay(2); }
  if (!readRegs(bme.addr, 0xF7, d, 8)) { bmeWhy = "data read failed (I2C)"; return false; }
  const int32_t adcP = (int32_t)d[0] << 12 | d[1] << 4 | d[2] >> 4;
  const int32_t adcT = (int32_t)d[3] << 12 | d[4] << 4 | d[5] >> 4;
  const int32_t adcH = (int32_t)d[6] << 8 | d[7];
  if (adcT == 0x80000) {                                        // measurement skipped / not started
    uint8_t st = 0;
    readRegs(bme.addr, 0xF4, &cm, 1);
    readRegs(bme.addr, 0xF3, &st, 1);
    snprintf(bmeWhyBuf, sizeof bmeWhyBuf, "no measurement (ctrl_meas=0x%02X status=0x%02X, want 0x24)", cm, st);
    bmeWhy = bmeWhyBuf;
    return false;
  }

  int32_t v1 = ((((adcT >> 3) - ((int32_t)bme.T1 << 1))) * (int32_t)bme.T2) >> 11;
  int32_t v2 = (((((adcT >> 4) - (int32_t)bme.T1) * ((adcT >> 4) - (int32_t)bme.T1)) >> 12) * (int32_t)bme.T3) >> 14;
  const int32_t tFine = v1 + v2;
  temp = ((tFine * 5 + 128) >> 8) / 100.0f;

  int64_t p1 = (int64_t)tFine - 128000;
  int64_t p2 = p1 * p1 * (int64_t)bme.P6;
  p2 += (p1 * (int64_t)bme.P5) << 17;
  p2 += (int64_t)bme.P4 << 35;
  p1 = ((p1 * p1 * (int64_t)bme.P3) >> 8) + ((p1 * (int64_t)bme.P2) << 12);
  p1 = ((((int64_t)1) << 47) + p1) * (int64_t)bme.P1 >> 33;
  if (p1 == 0) { bmeWhy = "pressure calibration P1 is 0"; return false; }
  int64_t p = 1048576 - adcP;
  p = (((p << 31) - p2) * 3125) / p1;
  p1 = ((int64_t)bme.P9 * (p >> 13) * (p >> 13)) >> 25;
  p2 = ((int64_t)bme.P8 * p) >> 19;
  p = ((p + p1 + p2) >> 8) + ((int64_t)bme.P7 << 4);
  pres = (float)(p / 256.0 / 100.0);                            // Pa (Q24.8) → hPa

  int32_t h = tFine - 76800;
  h = (((((adcH << 14) - ((int32_t)bme.H4 << 20) - ((int32_t)bme.H5 * h)) + 16384) >> 15) *
       (((((((h * (int32_t)bme.H6) >> 10) * (((h * (int32_t)bme.H3) >> 11) + 32768)) >> 10) + 2097152) *
             (int32_t)bme.H2 + 8192) >> 14));
  h = h - (((((h >> 15) * (h >> 15)) >> 7) * (int32_t)bme.H1) >> 4);
  h = h < 0 ? 0 : (h > 419430400 ? 419430400 : h);
  hum = (h >> 12) / 1024.0f;
  bmeSeenOk = true;
  return true;
}

// ── SCD40 (Sensirion: 16-bit commands, CRC-8 per word) ───────────────
static const uint8_t SCD40_ADDR = 0x62;
static bool scdFound = false;

static uint8_t crc8(const uint8_t* d, int n) {
  uint8_t c = 0xFF;
  for (int i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1);
  }
  return c;
}

static bool scdCommand(uint16_t cmd) {
  const uint8_t b[2] = {(uint8_t)(cmd >> 8), (uint8_t)cmd};
  return writeBytes(SCD40_ADDR, b, 2);
}

static bool scdCommandArg(uint16_t cmd, uint16_t arg) {
  uint8_t b[5] = {(uint8_t)(cmd >> 8), (uint8_t)cmd, (uint8_t)(arg >> 8), (uint8_t)arg, 0};
  b[4] = crc8(&b[2], 2);
  return writeBytes(SCD40_ADDR, b, 5);
}

static bool scdReadWords(uint16_t* words, int n) {
  uint8_t buf[9];
  if (!readBytes(SCD40_ADDR, buf, n * 3)) return false;
  for (int i = 0; i < n; i++) {
    if (crc8(&buf[i * 3], 2) != buf[i * 3 + 2]) return false;
    words[i] = (uint16_t)(buf[i * 3] << 8 | buf[i * 3 + 1]);
  }
  return true;
}

static int scdAsc = -1;                                         // automatic self-calibration: 1 on, 0 off, -1 unknown
static uint32_t scdStartedMs = 0;
static int scdZeroes = 0;                                       // CO2 = 0 readings in a row (explained, not published)

static bool scdInit() {
  scdFound = false;
  if (!scdEnabled) return false;                 // SCD_OFF: never address 0x62, so a faulty one can't hang the bus
  if (!probe(SCD40_ADDR)) return false;
  scdCommand(0x3F86);                                           // stop_periodic_measurement (may still run after an ESP32 reset)
  delay(500);
  uint16_t w;
  if (scdCommand(0x2313)) {                                     // get_automatic_self_calibration_enabled (idle only)
    delay(1);
    if (scdReadWords(&w, 1)) scdAsc = w ? 1 : 0;
  }
  if (!scdCommand(0x21B1)) return false;                        // start_periodic_measurement: one reading every 5 s
  scdStartedMs = millis();
  scdFound = true;
  return true;
}

// Forced recalibration (datasheet 3.7.1): measured ≥3 min in air of known CO2, then idle.
// Automatic self-calibration goes off and both are stored in the sensor (persist_settings).
static void scdForcedCal(int ppm) {
  char m[140];
  if (!scdFound) { diag("CAL_CO2: no SCD40 found"); return; }
  if (millis() - scdStartedMs < 180000) { diag("CAL_CO2: let the SCD40 run 3 min in fresh air first"); return; }
  scdCommand(0x3F86);
  delay(500);
  uint16_t w = 0xFFFF;
  if (scdCommandArg(0x362F, (uint16_t)ppm)) {                   // perform_forced_recalibration
    delay(400);
    if (!scdReadWords(&w, 1)) w = 0xFFFF;
  }
  if (w == 0xFFFF) {
    diag("CAL_CO2: the SCD40 refused (it must have measured for 3 min just before)");
  } else {
    scdCommandArg(0x2416, 0);                                   // automatic self-calibration off
    delay(1);
    scdCommand(0x3615);                                         // persist_settings
    delay(800);
    scdAsc = 0;
    snprintf(m, sizeof m, "CAL_CO2: SCD40 set to %d ppm (correction %+d ppm); automatic self-calibration off",
             ppm, (int)w - 0x8000);
    diag(m);
  }
  scdCommand(0x21B1);
  scdStartedMs = millis();
}

static void scdAscOn() {
  if (!scdFound) { diag("ASC_ON: no SCD40 found"); return; }
  scdCommand(0x3F86);
  delay(500);
  scdCommandArg(0x2416, 1);
  delay(1);
  scdCommand(0x3615);
  delay(800);
  scdAsc = 1;
  scdCommand(0x21B1);
  scdStartedMs = millis();
  diag("ASC_ON: SCD40 automatic self-calibration on");
}

// Waits that are longer than the task watchdog allows: check in while waiting.
static void scdWait(uint32_t ms) {
  for (uint32_t t = 0; t < ms; t += 100) { delay(100); esp_task_wdt_reset(); }
}

// perform_self_test (datasheet 3.9.3): 10 s, the sensor checks itself; 0 = no malfunction.
static void scdSelfTest() {
  if (!scdFound) { diag("SCD_TEST: no SCD40 found"); return; }
  diag("SCD_TEST: self-test running (10 s, no readings meanwhile)");
  scdCommand(0x3F86);
  scdWait(500);
  uint16_t w = 0xFFFF;
  if (scdCommand(0x3639)) {
    scdWait(10000);
    if (!scdReadWords(&w, 1)) w = 0xFFFF;
  }
  char m[160];
  if (w == 0) snprintf(m, sizeof m, "SCD_TEST: passed. If CO2 still reads 0, the 3.3 V supply sags during the "
                                    "SCD40's lamp pulses: give it its own 3.3 V or 5 V supply and short wires");
  else if (w == 0xFFFF) snprintf(m, sizeof m, "SCD_TEST: no answer from the SCD40");
  else snprintf(m, sizeof m, "SCD_TEST: FAILED (code 0x%04X): the sensor reports a malfunction; try SCD_RESET, "
                             "else replace the SCD40", w);
  diag(m);
  scdCommand(0x21B1);
  scdStartedMs = millis();
  scdZeroes = 0;
}

// perform_factory_reset (datasheet 3.9.4): forgets forced recalibration and stored settings.
static void scdFactoryReset() {
  if (!scdFound) { diag("SCD_RESET: no SCD40 found"); return; }
  scdCommand(0x3F86);
  scdWait(500);
  const bool ok = scdCommand(0x3632);
  scdWait(1200);
  uint16_t w;
  if (scdCommand(0x2313)) {
    delay(1);
    if (scdReadWords(&w, 1)) scdAsc = w ? 1 : 0;
  }
  scdCommand(0x21B1);
  scdStartedMs = millis();
  scdZeroes = 0;
  diag(ok ? "SCD_RESET: SCD40 back to factory settings (calibration forgotten, self-calibration on); "
            "first CO2 reading in 5 s" : "SCD_RESET: the SCD40 did not accept the command");
}

// true and fills the values only when a new measurement is ready
static bool scdRead(float& co2, float& temp, float& hum) {
  uint16_t w[3];
  if (!scdFound || !scdCommand(0xE4B8)) return false;           // get_data_ready_status
  delay(1);
  if (!scdReadWords(w, 1) || (w[0] & 0x07FF) == 0) return false;
  if (!scdCommand(0xEC05)) return false;                        // read_measurement
  delay(1);
  if (!scdReadWords(w, 3)) return false;
  co2 = w[0];
  temp = -45.0f + 175.0f * w[1] / 65535.0f;
  hum = 100.0f * w[2] / 65535.0f;
  return true;
}

// ── BNO055 (Bosch: 9-axis fusion in NDOF mode) ───────────────────────
static uint8_t bnoAddr = 0, bnoSelfTest = 0;                    // ST_RESULT: bit 0 acc, 1 mag, 2 gyro, 3 MCU (1 = passed)
static const uint8_t BNO_OFFSETS = 0x55;                        // ACC_OFFSET_X_LSB … MAG_RADIUS_MSB
static const int BNO_OFFSETS_LEN = 22;
Preferences bnoPrefs;                                           // "imm-bno": the calibration offsets
static bool bnoCalRestored = false, bnoCalSaved = false;

static bool bnoInit() {
  bnoAddr = 0;
  for (uint8_t a : {(uint8_t)0x28, (uint8_t)0x29}) {
    uint8_t id;
    if (!readRegs(a, 0x00, &id, 1) || id != 0xA0) continue;
    writeReg(a, 0x07, 0x00);                                    // register page 0
    writeReg(a, 0x3D, 0x00);                                    // CONFIG mode
    delay(25);
    writeReg(a, 0x3F, 0x20);                                    // system reset
    delay(700);
    for (int i = 0; i < 50 && !(readRegs(a, 0x00, &id, 1) && id == 0xA0); i++) delay(10);
    if (!readRegs(a, 0x36, &bnoSelfTest, 1)) bnoSelfTest = 0;  // power-on self-test of the reset just done
    writeReg(a, 0x3E, 0x00);                                    // normal power
    delay(10);
    writeReg(a, 0x07, 0x00);
    writeReg(a, 0x3F, 0x00);
    writeReg(a, 0x3B, 0x00);                                    // units: m/s², degrees, °C
    uint8_t off[1 + BNO_OFFSETS_LEN];                           // calibration from an earlier run (CONFIG mode only)
    off[0] = BNO_OFFSETS;
    bnoCalRestored = bnoPrefs.getBytesLength("off") == BNO_OFFSETS_LEN &&
                     bnoPrefs.getBytes("off", off + 1, BNO_OFFSETS_LEN) == BNO_OFFSETS_LEN &&
                     writeBytes(a, off, sizeof off);
    writeReg(a, 0x3D, 0x0C);                                    // NDOF fusion
    delay(20);
    bnoAddr = a;
    return true;
  }
  return false;
}

static int16_t le16(const uint8_t* b) { return (int16_t)(b[0] | b[1] << 8); }

struct BnoReading {
  float heading, roll, pitch;   // degrees (fusion)
  float linAcc;                 // m/s², acceleration without gravity (magnitude)
  float grav;                   // m/s², gravity vector (magnitude): 9.8 when the accelerometer is right
  float mag;                    // µT, magnetic field (magnitude): 25-65 µT is the Earth's field
  float gyro;                   // °/s, rotation rate (magnitude)
  int temp;                     // °C, chip temperature
  int calSys, calGyro, calAcc, calMag;   // 0-3 each
};

static float norm3(const uint8_t* b, float lsb) {
  const float x = le16(&b[0]) / lsb, y = le16(&b[2]) / lsb, z = le16(&b[4]) / lsb;
  return sqrtf(x * x + y * y + z * z);
}

// Fully calibrated: store the offsets (once per start; read in CONFIG mode, then back to NDOF)
static void bnoSaveCalibration() {
  uint8_t off[BNO_OFFSETS_LEN];
  writeReg(bnoAddr, 0x3D, 0x00);
  delay(25);
  const bool ok = readRegs(bnoAddr, BNO_OFFSETS, off, sizeof off);
  writeReg(bnoAddr, 0x3D, 0x0C);
  delay(20);
  if (ok && bnoPrefs.putBytes("off", off, sizeof off) == sizeof off) {
    bnoCalSaved = true;
    diag("bno055: fully calibrated: calibration stored, restored at every start");
  }
}

static bool bnoRead(BnoReading& r) {
  uint8_t d[40];                                                // 0x0E MAG … 0x35 CALIB_STAT in one burst
  if (!bnoAddr || !readRegs(bnoAddr, 0x0E, d, sizeof d)) return false;
  r.mag = norm3(&d[0x0E - 0x0E], 16.0f);
  r.gyro = norm3(&d[0x14 - 0x0E], 16.0f);
  r.heading = le16(&d[0x1A - 0x0E]) / 16.0f;
  r.roll = le16(&d[0x1C - 0x0E]) / 16.0f;
  r.pitch = le16(&d[0x1E - 0x0E]) / 16.0f;
  r.linAcc = norm3(&d[0x28 - 0x0E], 100.0f);
  r.grav = norm3(&d[0x2E - 0x0E], 100.0f);
  r.temp = (int8_t)d[0x34 - 0x0E];
  const uint8_t c = d[0x35 - 0x0E];
  r.calSys = c >> 6 & 3; r.calGyro = c >> 4 & 3; r.calAcc = c >> 2 & 3; r.calMag = c & 3;
  return true;
}

// ── DFRobot SEN0322 oxygen (as in DFRobot_OxygenSensor) ──────────────
static uint8_t o2Addr = 0;

static bool o2Init() {
  o2Addr = 0;
  for (uint8_t a : {(uint8_t)0x73, (uint8_t)0x72, (uint8_t)0x71, (uint8_t)0x70})
    if (probe(a)) { o2Addr = a; return true; }
  return false;
}

static bool o2Read(float& pct, bool& calibrated) {
  uint8_t k, d[3];
  if (!o2Addr || !readRegs(o2Addr, 0x0A, &k, 1) || !readRegs(o2Addr, 0x03, d, 3)) return false;
  calibrated = k != 0;                                          // 0: never calibrated (factory default key)
  const float key = k ? k / 1000.0f : 20.9f / 120.0f;           // calibration key stored in the sensor
  pct = key * (d[0] + d[1] / 10.0f + d[2] / 100.0f);
  return true;
}

static bool o2Calibrate() { return o2Addr && writeReg(o2Addr, 0x08, 209); }   // 20.9 % × 10

// ── MQ-4 methane ─────────────────────────────────────────────────────
Preferences prefs;
static float mq4R0 = 0;                // Rs/RL in clean air ÷ MQ4_CLEAN_AIR_RATIO; 0 = not calibrated
static int mq4CalLeft = 0;
static float mq4CalSum = 0;

struct Mq4Reading { float voutMv, rsRl; bool ok; };

static Mq4Reading mq4Read() {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(PIN_MQ4);
  const float pinMv = sum / 16.0f;
  Mq4Reading r;
  r.voutMv = pinMv * MQ4_DIVIDER;
  r.ok = pinMv > 20.0f && pinMv < 3100.0f && r.voutMv < MQ4_VC_MV;
  r.rsRl = r.ok ? (MQ4_VC_MV - r.voutMv) / r.voutMv : 0;
  return r;
}

// ── Main loop ────────────────────────────────────────────────────────
static uint32_t lastSample = 0, lastProbe = 0, lastBoard = 0;
static int bmeFails = 0;
static char cmd[112];
static int cmdLen = 0;

static void status() {
  char b[200];
  snprintf(b, sizeof b, "sensors: bme280=%s scd40=%s bno055=%s o2=%s mq4_r0=%.3f divider=%.2f",
           bme.addr ? (bme.addr == 0x76 ? "0x76" : "0x77") : "none", scdFound ? "0x62" : "none",
           bnoAddr ? (bnoAddr == 0x28 ? "0x28" : "0x29") : "none", o2Addr ? "found" : "none", mq4R0, (double)MQ4_DIVIDER);
  diag(b);
  if (bmeResets) {
    snprintf(b, sizeof b, "bme280: chip reset %d time(s) since start", bmeResets);
    diag(b);
  }
  snprintf(b, sizeof b, "board: boot %lu, last reset reason %d%s, %lu I2C error(s), %d bus recover(ies)",
           (unsigned long)bootCount, resetReason,
           resetReason == 9 ? " (BROWNOUT: the supply dipped)" : resetReason == 6 || resetReason == 7 ? " (WATCHDOG)" :
           resetReason == 4 ? " (CRASH)" : "", (unsigned long)i2cErr, i2cRecoveries);
  diag(b);
  if (bnoAddr) {
    snprintf(b, sizeof b, "bno055 self-test: accel=%s mag=%s gyro=%s mcu=%s",
             bnoSelfTest & 1 ? "pass" : "FAIL", bnoSelfTest & 2 ? "pass" : "FAIL",
             bnoSelfTest & 4 ? "pass" : "FAIL", bnoSelfTest & 8 ? "pass" : "FAIL");
    diag(b);
  }
}

static void findSensors() {
  i2cQuiet = true;
  if (!bme.addr) bmeInit();
  if (!scdFound) scdInit();
  if (!bnoAddr) bnoInit();
  if (!o2Addr) o2Init();
  i2cQuiet = false;
}

// A device that lost its place mid-transfer can hold SDA low forever: every transaction then
// fails. Clock SCL until it lets go (at most 9 bits), send a STOP, restart the controller and
// set the sensors up again (the one that hung may have reset).
static void i2cRecover() {
  Wire.end();
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, OUTPUT_OPEN_DRAIN);
  for (int i = 0; i < 9 && digitalRead(PIN_SDA) == LOW; i++) {
    digitalWrite(PIN_SCL, LOW);  delayMicroseconds(5);
    digitalWrite(PIN_SCL, HIGH); delayMicroseconds(5);
  }
  pinMode(PIN_SDA, OUTPUT_OPEN_DRAIN);                          // STOP: SDA low → high while SCL is high
  digitalWrite(PIN_SDA, LOW);  delayMicroseconds(5);
  digitalWrite(PIN_SCL, HIGH); delayMicroseconds(5);
  digitalWrite(PIN_SDA, HIGH); delayMicroseconds(5);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  Wire.setTimeOut(50);
  ++i2cRecoveries;
  i2cStreak = 0;
  char m[96];
  snprintf(m, sizeof m, "i2c: every transaction failing: bus recovered (%d time(s) since start), sensors set up again",
           i2cRecoveries);
  diag(m);
  bme.addr = 0; scdFound = false; bnoAddr = 0; o2Addr = 0;
  findSensors();
}

static void watchdogStart() {
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = WDT_TIMEOUT_S * 1000;
  cfg.idle_core_mask = 0;
  cfg.trigger_panic = true;                                     // reset the board, don't just log
  if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) esp_task_wdt_init(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);                       // (re)configures it if the core already started it
#endif
  esp_task_wdt_add(NULL);                                       // this task (setup/loop) must check in
}

// ── Wi-Fi + page ─────────────────────────────────────────────────────
Preferences wifiPrefs;                                          // "imm-wifi": network name and password
WebServer web(80);
static char wifiSsid[33] = "", wifiPass[65] = "";
static char lastLine[1024] = "{}";

static const char PAGE[] =
    "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>IMM-OS sensor board</title><style>body{font-family:sans-serif;background:#0b1224;color:#e6ecff;margin:16px}"
    "h1{font-size:18px}.v{font-size:26px;font-weight:700}.c{display:inline-block;margin:8px 18px 8px 0;vertical-align:top}"
    "small{color:#8f9bb8}</style></head><body><h1>IMM-OS sensor board</h1><div id=d>…</div><script>"
    "const c=(l,v,u,s)=>`<div class=c><small>${l}</small><div class=v>${v?\?'–'} ${u}</div><small>${s||''}</small></div>`;"
    "async function t(){try{const j=await(await fetch('/json')).json(),b=j.bme280||{},s=j.scd40||{},o=j.o2||{},m=j.mq4||{},n=j.bno055||{};"
    "document.getElementById('d').innerHTML=c('Temperature',b.temp,'°C',`${b.hum?\?'–'} %RH · ${b.pres?\?'–'} hPa`)"
    "+c('CO₂',s.co2_ppm,'ppm',s.asc===0?'self-calibration off':'')+c('O₂',o.o2_pct,'%',o.calibrated?'calibrated':'not calibrated')"
    "+c('Methane',m.warming?'warming':m.ch4_ppm,m.warming?'':'ppm',m.warming?`${m.warm_left_s?\?''} s left`:'')"
    "+c('Heading',n.heading_deg,'°',`calibration ${n.imu_calib?\?'–'}/3`)}catch(e){}}"
    "t();setInterval(t,1000)</script></body></html>";

// The web server can only start once Wi-Fi has brought the network stack (lwIP) up: starting it
// earlier makes the ESP32 abort ("tcpip_api_call: Invalid mbox") and reboot, over and over.
static bool webStarted = false;
static void polled();

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
  WiFi.setHostname("imm-sensors");
  WiFi.begin(wifiSsid, wifiPass);
  webStart();
}

// Announce "imm-sensors" on the network once Wi-Fi is up, so the Pi reaches this board as
// http://imm-sensors.local/json whatever IP the router gives it — on this router or any future
// one, with nothing to reserve. Re-announced if Wi-Fi drops and comes back.
bool mdnsStarted = false;
static void mdnsTick() {
  const bool up = WiFi.status() == WL_CONNECTED;
  if (up && !mdnsStarted) {
    if (MDNS.begin("imm-sensors")) {
      MDNS.addService("http", "tcp", 80);
      mdnsStarted = true;
      diag("mdns: reachable as http://imm-sensors.local/json (the name works on any router)");
    }
  } else if (!up && mdnsStarted) {
    MDNS.end();
    mdnsStarted = false;
  }
}

// ── network self-recovery ───────────────────────────────────────────
// A reboot the firmware chose: say why on USB, remember the cause across it (NVS: the next boot
// reports it as heal_cause, so the data shows when and why), then restart. A reboot keeps the
// MQ-4 hot (reset reason 3: no warm-up) and BNO055 calibration, so it costs ~10 s of data.
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
    if (now - lastPollMs >= POLL_REBOOT_MS && !pollWait) {
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
    if (now - wifiDownSince >= WIFI_REBOOT_MS && !wifiWait) {
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

static void handleCommand() {
  cmd[cmdLen] = 0;
  for (char* p = cmd; *p && *p != ' '; p++) *p = (char)toupper(*p);   // the command word is case-insensitive;
                                                                       // Wi-Fi name and password keep their case
  if (strcmp(cmd, "STATUS") == 0) {
    status();
    char b[240];
    snprintf(b, sizeof b, "wifi: %s ip=%s", wifiSsid[0] ? (WiFi.status() == WL_CONNECTED ? "connected" : "connecting") : "off",
             WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "-");
    diag(b);
    snprintf(b, sizeof b, "network: %lu Wi-Fi drop(s) (reason %d, latest %d), %lu web/mDNS restart(s), %lu self-heal "
             "reboot(s) since flashing, this start: %d, free memory %lu B", (unsigned long)wifiDrops, dropReason, (int)wifiReason,
             (unsigned long)netRestarts, (unsigned long)healReboots, healCause, (unsigned long)esp_get_free_heap_size());
    diag(b);
  } else if (strncmp(cmd, "WIFI_SSID ", 10) == 0) {                  // rest of the line: spaces allowed
    snprintf(wifiSsid, sizeof wifiSsid, "%s", cmd + 10);
    wifiPrefs.putString("ssid", wifiSsid);
    diag("wifi: network name stored; now send WIFI_PASS <password> (WIFI_PASS alone for an open network)");
  } else if (strncmp(cmd, "WIFI_PASS", 9) == 0 && (cmd[9] == ' ' || cmd[9] == 0)) {
    snprintf(wifiPass, sizeof wifiPass, "%s", cmd[9] ? cmd + 10 : "");
    wifiPrefs.putString("pass", wifiPass);
    if (!wifiSsid[0]) diag("wifi: password stored; send WIFI_SSID <network name>");
    else { diag("wifi: stored, connecting (STATUS shows the IP)"); wifiStart(); }
  } else if (strcmp(cmd, "WIFI_OFF") == 0) {
    wifiSsid[0] = wifiPass[0] = 0;
    wifiPrefs.putString("ssid", "");
    wifiPrefs.putString("pass", "");
    WiFi.disconnect(true);
    diag("wifi: network forgotten; USB only");
  } else if (strcmp(cmd, "CAL_MQ4") == 0) {
    if (millis() < mq4WarmupMs) diag("CAL_MQ4: still warming up; try again after 3 min");
    else { mq4CalLeft = MQ4_CAL_SAMPLES; mq4CalSum = 0; diag("CAL_MQ4: sampling clean air for 10 s"); }
  } else if (strcmp(cmd, "CAL_O2") == 0) {
    diag(o2Calibrate() ? "CAL_O2: SEN0322 set to 20.9 % (fresh air)" : "CAL_O2: no SEN0322 found");
  } else if (strncmp(cmd, "CAL_CO2", 7) == 0 && (cmd[7] == 0 || cmd[7] == ' ')) {
    const int ppm = cmd[7] ? atoi(cmd + 8) : 420;
    if (ppm < 400 || ppm > 2000) diag("CAL_CO2: ppm must be 400-2000 (fresh outdoor air: 420)");
    else scdForcedCal(ppm);
  } else if (strcmp(cmd, "ASC_ON") == 0) {
    scdAscOn();
  } else if (strcmp(cmd, "SCD_TEST") == 0) {
    scdSelfTest();
  } else if (strcmp(cmd, "SCD_RESET") == 0) {
    scdFactoryReset();
  } else if (strcmp(cmd, "SCD_OFF") == 0) {
    scdEnabled = false; scdFound = false; prefs.putUInt("scd_off", 1);
    diag("scd40: disabled and remembered. The firmware will not touch it, so a faulty SCD40 can no "
         "longer hang the I2C bus; the other sensors keep working. Send SCD_ON to re-enable.");
  } else if (strcmp(cmd, "SCD_ON") == 0) {
    scdEnabled = true; prefs.putUInt("scd_off", 0);
    diag("scd40: enabled; it will be looked for again on the next probe");
  } else if (strcmp(cmd, "CAL_BNO_CLEAR") == 0) {
    bnoPrefs.remove("off");
    bnoCalRestored = bnoCalSaved = false;
    diag("CAL_BNO_CLEAR: stored BNO055 calibration forgotten");
  } else if (cmdLen) {
    diag("unknown command (STATUS, CAL_MQ4, CAL_O2, CAL_CO2, ASC_ON, SCD_TEST, SCD_RESET, SCD_OFF, SCD_ON, CAL_BNO_CLEAR, WIFI_SSID, WIFI_PASS, WIFI_OFF)");
  }
  cmdLen = 0;
}

void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);                // BNO055 stretches the clock; 100 kHz keeps every device happy
  Wire.setTimeOut(50);
  resetReason = (int)esp_reset_reason();
  {
    Preferences board;
    board.begin("imm-board", false);
    bootCount = board.getUInt("boots", 0) + 1;
    board.putUInt("boots", bootCount);
    board.end();
  }
  prefs.begin("imm-mq4", false);
  mq4R0 = prefs.isKey("r0") ? prefs.getFloat("r0", 0) : 0;   // isKey: no "NOT_FOUND" error log before CAL_MQ4
  scdEnabled = prefs.getUInt("scd_off", 0) == 0;            // remembered across restarts
  {
    // A self-heal reboot is a software reset (3); anything else (power cut, RESET button) wasn't ours
    const uint32_t cause = prefs.getUInt("heal", 0);
    healCause = resetReason == 3 ? (int)cause : HEAL_NONE;
    if (cause) prefs.putUInt("heal", 0);
    healReboots = prefs.getUInt("heals", 0);
    wifiWait = prefs.getUInt("wifi_wait", 0) != 0;
    pollWait = prefs.getUInt("poll_wait", 0) != 0;
  }
  bnoPrefs.begin("imm-bno", false);
  wifiPrefs.begin("imm-wifi", false);
  snprintf(wifiSsid, sizeof wifiSsid, "%s", wifiPrefs.getString("ssid", "").c_str());
  snprintf(wifiPass, sizeof wifiPass, "%s", wifiPrefs.getString("pass", "").c_str());
  // EN button (2), software (3), crash (4), watchdogs (5-7), deep sleep (8): the ESP32 restarted
  // but the MQ-4 heater kept its 5 V, so it is still hot. Power-on (1), brownout (9), unknown: warm up.
  mq4WarmupMs = resetReason >= 2 && resetReason <= 8 ? 0 : MQ4_WARMUP_MS;
  delay(800);                           // BNO055 boot time after power-up
  diag("IMM-OS ESP32 sensor board started");
  if (healCause) {
    static const char* const why[] = {"", "I2C bus stall", "Wi-Fi lost", "not polled", "memory low"};
    char m[80];
    snprintf(m, sizeof m, "self-heal: this start was a self-heal reboot (%s)", healCause <= 4 ? why[healCause] : "?");
    diag(m);
  }
  diag(mq4WarmupMs ? "mq4: heater warming up for 3 min" : "mq4: heater stayed powered through this reset: no warm-up");
  findSensors();
  status();
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) { wifiReason = info.wifi_sta_disconnected.reason; },
               ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  wifiStart();
  if (wifiSsid[0]) diag("wifi: connecting (STATUS shows the IP)");
  lastProbe = millis();
  watchdogStart();
}

void loop() {
  esp_task_wdt_reset();
  if (webStarted) { web.handleClient(); mdnsTick(); networkTick(millis()); }
  while (Serial.available()) {
    const int c = Serial.read();
    if (c == '\n' || c == '\r') handleCommand();
    else if (cmdLen < (int)sizeof(cmd) - 1) cmd[cmdLen++] = (char)c;
  }

  const uint32_t now = millis();
  if (now - lastProbe >= REPROBE_MS) {
    lastProbe = now;
    if (!bme.addr || !scdFound || !bnoAddr || !o2Addr) findSensors();
  }
  if (i2cStreak >= I2C_STUCK_STREAK && now - lastRecovery >= 10000) {
    lastRecovery = now;
    i2cRecover();
  }
  if (i2cEverOk && now - lastI2cOkMs > STALL_RECOVER_MS) {        // every I2C sensor has gone silent
    if (now - lastStallRecover >= STALL_RECOVER_MS) {
      lastStallRecover = now;
      diag("self-heal: no I2C sensor data; recovering the bus");
      i2cRecover();
    }
    if (now - lastI2cOkMs > STALL_REBOOT_MS && !stallRebooted) {
      stallRebooted = true;
      healReboot(HEAL_I2C, "self-heal: I2C sensors still silent; rebooting to clear the bus");
      return;
    }
  }
  if (now - lastSample < PERIOD_MS) return;
  lastSample = now;

  char line[1024];
  int n = snprintf(line, sizeof line, "{\"ms\":%lu", (unsigned long)now);
  float a, b, c;
  BnoReading bno;
  bool i2cOk = false;
  if (bmeRead(a, b, c)) {
    bmeFails = 0;
    i2cOk = true;
    n += snprintf(line + n, sizeof line - n, ",\"bme280\":{\"temp\":%.2f,\"hum\":%.2f,\"pres\":%.2f}", a, b, c);
  } else if (bme.addr) {
    ++bmeFails;
    if (bmeFails == 3 || bmeFails % 30 == 0) {         // explain, without flooding the output
      char m[140];
      snprintf(m, sizeof m, "bme280: no reading (%s)", bmeWhy);
      diag(m);
    }
    if (bmeFails % 10 == 0) { diag("bme280: re-initialising"); bmeInit(); }
  }
  if (scdRead(a, b, c)) {
    i2cOk = true;
    // CO2 = 0 is impossible in air (the SCD40's range starts at 400 ppm): leave it out rather than publish it
    if (a > 0) n += snprintf(line + n, sizeof line - n, ",\"scd40\":{\"co2_ppm\":%.0f,\"temp\":%.2f,\"hum\":%.2f", a, b, c);
    else {
      n += snprintf(line + n, sizeof line - n, ",\"scd40\":{\"temp\":%.2f,\"hum\":%.2f", b, c);
      if (scdZeroes++ % 12 == 0)
        diag(millis() - scdStartedMs < 60000 ? "scd40: CO2 reads 0, left out (normal for the first readings after start)"
             : "scd40: CO2 still reads 0 (temperature and humidity are fine): run SCD_TEST");
    }
    if (scdAsc >= 0) n += snprintf(line + n, sizeof line - n, ",\"asc\":%d", scdAsc);
    n += snprintf(line + n, sizeof line - n, "}");
  }
  if (bnoRead(bno)) {
    i2cOk = true;
    if (!bnoCalSaved && bno.calSys == 3 && bno.calGyro == 3 && bno.calAcc == 3 && bno.calMag == 3) bnoSaveCalibration();
    n += snprintf(line + n, sizeof line - n,
                  ",\"bno055\":{\"heading_deg\":%.2f,\"roll_deg\":%.2f,\"pitch_deg\":%.2f,\"lin_acc_ms2\":%.2f,\"imu_calib\":%d,"
                  "\"grav_ms2\":%.2f,\"mag_ut\":%.1f,\"gyro_dps\":%.2f,\"temp\":%d,"
                  "\"calib_gyro\":%d,\"calib_acc\":%d,\"calib_mag\":%d,\"cal_restored\":%d}",
                  bno.heading, bno.roll, bno.pitch, bno.linAcc, bno.calSys, bno.grav, bno.mag, bno.gyro, bno.temp,
                  bno.calGyro, bno.calAcc, bno.calMag, bnoCalRestored ? 1 : 0);
  }
  bool o2Cal = false;
  if (o2Read(a, o2Cal)) {
    i2cOk = true;
    n += snprintf(line + n, sizeof line - n, ",\"o2\":{\"o2_pct\":%.2f,\"calibrated\":%d}", a, o2Cal ? 1 : 0);
  }
  if (i2cOk) { lastI2cOkMs = now; i2cEverOk = true; stallRebooted = false; }

  const Mq4Reading mq = mq4Read();
  const bool warming = now < mq4WarmupMs;
  if (mq.ok) {
    if (mq4CalLeft > 0) {
      mq4CalSum += mq.rsRl;
      if (--mq4CalLeft == 0) {
        mq4R0 = mq4CalSum / MQ4_CAL_SAMPLES / MQ4_CLEAN_AIR_RATIO;
        prefs.putFloat("r0", mq4R0);
        char m[64];
        snprintf(m, sizeof m, "CAL_MQ4: R0 stored (Rs/RL=%.3f)", mq4R0);
        diag(m);
      }
    }
    n += snprintf(line + n, sizeof line - n, ",\"mq4\":{\"vout_mv\":%.0f,\"rs_rl\":%.3f", mq.voutMv, mq.rsRl);
    if (mq4R0 > 0) {
      const float ratio = mq.rsRl / mq4R0;
      n += snprintf(line + n, sizeof line - n, ",\"rs_r0\":%.3f", ratio);
      if (!warming) n += snprintf(line + n, sizeof line - n, ",\"ch4_ppm\":%.1f", MQ4_A * powf(ratio, MQ4_B));
    }
    n += snprintf(line + n, sizeof line - n, ",\"warming\":%d,\"calibrated\":%d", warming ? 1 : 0, mq4R0 > 0 ? 1 : 0);
    if (warming) n += snprintf(line + n, sizeof line - n, ",\"warm_left_s\":%lu", (unsigned long)((mq4WarmupMs - now + 999) / 1000));
    n += snprintf(line + n, sizeof line - n, "}");
  }
  if (now - lastBoard >= BOARD_MS || lastBoard == 0) {
    lastBoard = now;
    n += snprintf(line + n, sizeof line - n,
                  ",\"board\":{\"uptime_s\":%lu,\"reset_reason\":%d,\"boot_count\":%lu,\"i2c_err\":%lu,\"bme_resets\":%d",
                  (unsigned long)(now / 1000), resetReason, (unsigned long)bootCount, (unsigned long)i2cErr, bmeResets);
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
