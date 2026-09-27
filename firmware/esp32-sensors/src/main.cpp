// IMM-OS ESP32 sensor board firmware.
//
// Reads the board's sensors and sends one JSON line per second over USB serial
// (115200 baud) to the Raspberry Pi, where sensor_drivers/esp32_bridge.py publishes
// each sensor to IMM-OS. The Pi handles TLS, login and the offline blackbox, so this
// board needs no Wi-Fi.
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
//    "scd40":{"co2_ppm":612,"temp":25.10,"hum":40.30},             (every 5 s, when new)
//    "bno055":{"heading_deg":..,"roll_deg":..,"pitch_deg":..,"lin_acc_ms2":..,"imu_calib":3,
//              "grav_ms2":9.80,"mag_ut":42.1,"gyro_dps":0.1,"temp":26,
//              "calib_gyro":3,"calib_acc":3,"calib_mag":3},
//    "o2":{"o2_pct":20.87},
//    "mq4":{"vout_mv":1240,"rs_rl":3.03,"rs_r0":1.03,"ch4_ppm":4.1,"warming":0,"calibrated":1}}
// Lines starting with '#' are diagnostics.
//
// Commands (a line sent to the board):
//   STATUS   which sensors were found, MQ-4 calibration
//   CAL_MQ4  in clean air, after warm-up: store the MQ-4's R0 (flash, survives power-off)
//   CAL_O2   in fresh outdoor air: tell the SEN0322 that it reads 20.9 %
#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>
#include <math.h>

static const int PIN_SDA = 21, PIN_SCL = 22, PIN_MQ4 = 32;
static const uint32_t PERIOD_MS = 1000, REPROBE_MS = 30000;

// MQ-4: 5 V heater and load circuit on the module, AO divided down to the ESP32's range
#ifndef MQ4_DIVIDER
#define MQ4_DIVIDER 2.0f              // (R_top + R_bottom) / R_bottom: 10 kΩ AO→GPIO32, 10 kΩ GPIO32→GND
#endif
static const float MQ4_VC_MV = 5000.0f;          // module supply
static const float MQ4_CLEAN_AIR_RATIO = 4.4f;   // Rs/R0 in clean air (datasheet sensitivity curve)
static const float MQ4_A = 1012.7f, MQ4_B = -2.786f;   // CH4: ppm = A * (Rs/R0)^B (curve fit)
static const uint32_t MQ4_WARMUP_MS = 180000;    // after power-up; 24-48 h burn-in before calibrating
static const int MQ4_CAL_SAMPLES = 10;

// ── I2C helpers ──────────────────────────────────────────────────────
static bool probe(uint8_t addr) { Wire.beginTransmission(addr); return Wire.endTransmission() == 0; }

static bool writeBytes(uint8_t addr, const uint8_t* data, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(data, n);
  return Wire.endTransmission() == 0;
}

static bool writeReg(uint8_t addr, uint8_t reg, uint8_t value) {
  const uint8_t b[2] = {reg, value};
  return writeBytes(addr, b, 2);
}

static bool readBytes(uint8_t addr, uint8_t* out, size_t n) {
  if (Wire.requestFrom(addr, (uint8_t)n) != n) return false;
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
    ok = writeRegVerified(a, 0xF4, 0x27, 0xFF) && ok;           // temp x1, pressure x1, normal mode
    if (!ok) diag("bme280: settings did not stick (ctrl registers read back differently)");
    bme.addr = a;
    return true;
  }
  return false;
}

static bool bmeRead(float& temp, float& hum, float& pres) {
  uint8_t d[8];
  if (!bme.addr) { bmeWhy = "not initialised"; return false; }
  if (!readRegs(bme.addr, 0xF7, d, 8)) { bmeWhy = "data read failed (I2C)"; return false; }
  const int32_t adcP = (int32_t)d[0] << 12 | d[1] << 4 | d[2] >> 4;
  const int32_t adcT = (int32_t)d[3] << 12 | d[4] << 4 | d[5] >> 4;
  const int32_t adcH = (int32_t)d[6] << 8 | d[7];
  if (adcT == 0x80000) {                                        // measurement skipped / not started
    uint8_t cm = 0, st = 0;
    readRegs(bme.addr, 0xF4, &cm, 1);
    readRegs(bme.addr, 0xF3, &st, 1);
    snprintf(bmeWhyBuf, sizeof bmeWhyBuf, "no measurement (ctrl_meas=0x%02X status=0x%02X, want 0x27)", cm, st);
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

static bool scdReadWords(uint16_t* words, int n) {
  uint8_t buf[9];
  if (!readBytes(SCD40_ADDR, buf, n * 3)) return false;
  for (int i = 0; i < n; i++) {
    if (crc8(&buf[i * 3], 2) != buf[i * 3 + 2]) return false;
    words[i] = (uint16_t)(buf[i * 3] << 8 | buf[i * 3 + 1]);
  }
  return true;
}

static bool scdInit() {
  scdFound = false;
  if (!probe(SCD40_ADDR)) return false;
  scdCommand(0x3F86);                                           // stop_periodic_measurement (may still run after an ESP32 reset)
  delay(500);
  if (!scdCommand(0x21B1)) return false;                        // start_periodic_measurement: one reading every 5 s
  scdFound = true;
  return true;
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

static bool o2Read(float& pct) {
  uint8_t k, d[3];
  if (!o2Addr || !readRegs(o2Addr, 0x0A, &k, 1) || !readRegs(o2Addr, 0x03, d, 3)) return false;
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
static uint32_t lastSample = 0, lastProbe = 0;
static int bmeFails = 0, scdZeroes = 0;
static char cmd[32];
static int cmdLen = 0;

static void status() {
  char b[200];
  snprintf(b, sizeof b, "sensors: bme280=%s scd40=%s bno055=%s o2=%s mq4_r0=%.3f divider=%.2f",
           bme.addr ? (bme.addr == 0x76 ? "0x76" : "0x77") : "none", scdFound ? "0x62" : "none",
           bnoAddr ? (bnoAddr == 0x28 ? "0x28" : "0x29") : "none", o2Addr ? "found" : "none", mq4R0, (double)MQ4_DIVIDER);
  diag(b);
  if (bnoAddr) {
    snprintf(b, sizeof b, "bno055 self-test: accel=%s mag=%s gyro=%s mcu=%s",
             bnoSelfTest & 1 ? "pass" : "FAIL", bnoSelfTest & 2 ? "pass" : "FAIL",
             bnoSelfTest & 4 ? "pass" : "FAIL", bnoSelfTest & 8 ? "pass" : "FAIL");
    diag(b);
  }
}

static void findSensors() {
  if (!bme.addr) bmeInit();
  if (!scdFound) scdInit();
  if (!bnoAddr) bnoInit();
  if (!o2Addr) o2Init();
}

static void handleCommand() {
  cmd[cmdLen] = 0;
  if (strcmp(cmd, "STATUS") == 0) {
    status();
  } else if (strcmp(cmd, "CAL_MQ4") == 0) {
    if (millis() < MQ4_WARMUP_MS) diag("CAL_MQ4: still warming up; try again after 3 min");
    else { mq4CalLeft = MQ4_CAL_SAMPLES; mq4CalSum = 0; diag("CAL_MQ4: sampling clean air for 10 s"); }
  } else if (strcmp(cmd, "CAL_O2") == 0) {
    diag(o2Calibrate() ? "CAL_O2: SEN0322 set to 20.9 % (fresh air)" : "CAL_O2: no SEN0322 found");
  } else if (cmdLen) {
    diag("unknown command (STATUS, CAL_MQ4, CAL_O2)");
  }
  cmdLen = 0;
}

void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);                // BNO055 stretches the clock; 100 kHz keeps every device happy
  Wire.setTimeOut(50);
  prefs.begin("imm-mq4", false);
  mq4R0 = prefs.getFloat("r0", 0);
  delay(800);                           // BNO055 boot time after power-up
  diag("IMM-OS ESP32 sensor board started");
  findSensors();
  status();
  lastProbe = millis();
}

void loop() {
  while (Serial.available()) {
    const int c = Serial.read();
    if (c == '\n' || c == '\r') handleCommand();
    else if (cmdLen < (int)sizeof(cmd) - 1) cmd[cmdLen++] = (char)toupper(c);
  }

  const uint32_t now = millis();
  if (now - lastProbe >= REPROBE_MS) {
    lastProbe = now;
    if (!bme.addr || !scdFound || !bnoAddr || !o2Addr) findSensors();
  }
  if (now - lastSample < PERIOD_MS) return;
  lastSample = now;

  char line[768];
  int n = snprintf(line, sizeof line, "{\"ms\":%lu", (unsigned long)now);
  float a, b, c;
  BnoReading bno;
  if (bmeRead(a, b, c)) {
    bmeFails = 0;
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
    // CO2 = 0 is impossible in air (the SCD40's range starts at 400 ppm): leave it out rather than publish it
    if (a > 0) n += snprintf(line + n, sizeof line - n, ",\"scd40\":{\"co2_ppm\":%.0f,\"temp\":%.2f,\"hum\":%.2f}", a, b, c);
    else {
      n += snprintf(line + n, sizeof line - n, ",\"scd40\":{\"temp\":%.2f,\"hum\":%.2f}", b, c);
      if (scdZeroes++ % 12 == 0) diag("scd40: CO2 reads 0, left out (normal for the first readings after start)");
    }
  }
  if (bnoRead(bno))
    n += snprintf(line + n, sizeof line - n,
                  ",\"bno055\":{\"heading_deg\":%.2f,\"roll_deg\":%.2f,\"pitch_deg\":%.2f,\"lin_acc_ms2\":%.2f,\"imu_calib\":%d,"
                  "\"grav_ms2\":%.2f,\"mag_ut\":%.1f,\"gyro_dps\":%.2f,\"temp\":%d,"
                  "\"calib_gyro\":%d,\"calib_acc\":%d,\"calib_mag\":%d}",
                  bno.heading, bno.roll, bno.pitch, bno.linAcc, bno.calSys, bno.grav, bno.mag, bno.gyro, bno.temp,
                  bno.calGyro, bno.calAcc, bno.calMag);
  if (o2Read(a))
    n += snprintf(line + n, sizeof line - n, ",\"o2\":{\"o2_pct\":%.2f}", a);

  const Mq4Reading mq = mq4Read();
  const bool warming = now < MQ4_WARMUP_MS;
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
    n += snprintf(line + n, sizeof line - n, ",\"warming\":%d,\"calibrated\":%d}", warming ? 1 : 0, mq4R0 > 0 ? 1 : 0);
  }
  snprintf(line + n, sizeof line - n, "}");
  Serial.println(line);
}
