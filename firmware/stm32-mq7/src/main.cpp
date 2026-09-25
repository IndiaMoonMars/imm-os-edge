/*
 * IMM-OS MQ-7 CO sensor controller — STM32F103C8 "Blue Pill" (STM32duino core).
 *
 * The MQ-7 only gives a meaningful CO reading after a heater cycle:
 *   60 s at 5.0 V (burns off contaminants)  →  90 s at 1.4 V (sensing)  →  read at the end.
 * The Raspberry Pi can't do this timing and has no ADC, so this board does it and sends
 * one line per 150 s cycle to the Pi's UART (sensor_drivers/mq7_uart_bridge.py):
 *
 *   CO:12.4                                  ← the reading the bridge publishes
 *   # vout=1.234 rs=30512 r0=1105 ratio=27.6 ← diagnostics (ignored by the bridge)
 *
 * Commands from the Pi (115200 8N1, newline-terminated):
 *   CAL      next cycle: treat the air as clean and store R0 = Rs / 27.5 in flash
 *   STATUS   print the current phase, R0 and cycle count
 *
 * Wiring (see firmware/stm32-mq7/README.md):
 *   PA8  → gate of a logic-level N-MOSFET switching the heater's low side (H− to drain)
 *   PA0  ← sensor output node via a 100k/200k divider + 100 nF to GND (sensing circuit at 5 V)
 *   PA9  → Pi RX (pin 10)     PA10 ← Pi TX (pin 8)     GND ↔ Pi GND
 *   PC13   on-board LED: on during the 5 V phase
 */
#include <Arduino.h>
#include <EEPROM.h>
#include <math.h>

// ── Hardware ──────────────────────────────────────────────────────
static const uint32_t PIN_HEATER = PA8;
static const uint32_t PIN_SENSE = PA0;
static const uint32_t PIN_LED = PC13;              // active low on the Blue Pill
HardwareSerial PiSerial(PA10, PA9);                // USART1 (RX, TX)

static const float SUPPLY_V = 5.0f;                // heater + sensing circuit supply
static const float ADC_REF_V = 3.3f;
static const float DIV_TOP_OHMS = 100000.0f;      // output node → PA0
static const float DIV_BOTTOM_OHMS = 200000.0f;   // PA0 → GND   (5 V → 3.33 V at PA0)
static const float DIVIDER = (DIV_TOP_OHMS + DIV_BOTTOM_OHMS) / DIV_BOTTOM_OHMS;
static const float RL_OHMS = 10000.0f;             // load resistor from the sensor output node to GND
// The divider is in parallel with RL, so the sensor really works into RL ∥ divider.
static const float LOAD_OHMS = RL_OHMS * (DIV_TOP_OHMS + DIV_BOTTOM_OHMS) / (RL_OHMS + DIV_TOP_OHMS + DIV_BOTTOM_OHMS);

// Heater: the 1.4 V phase is made by PWM from 5 V. Heater power goes with V², so the
// duty for the same average power is (1.4 / 5.0)² ≈ 7.8 % (not 28 %).
static const uint32_t PWM_HZ = 1000;
static const float LOW_PHASE_V = 1.4f;

// ── Timing ────────────────────────────────────────────────────────
static const uint32_t HIGH_MS = 60000;
static const uint32_t LOW_MS = 90000;
static const uint32_t SAMPLE_WINDOW_MS = 5000;      // average the last 5 s of the low phase

// ── CO curve (datasheet fit, as used by the MQUnifiedsensor library) ──
// ppm = A · (Rs/R0)^B ; clean air has Rs/R0 ≈ 27.5. Treat values as indicative:
// MQ-7 accuracy is poor without a calibration gas, and it needs 24–48 h burn-in.
static const float CURVE_A = 99.042f;
static const float CURVE_B = -1.518f;
static const float CLEAN_AIR_RATIO = 27.5f;

// ── R0 in emulated EEPROM (flash) ─────────────────────────────────
static const uint32_t R0_MAGIC = 0x4D513737;        // "MQ77"
struct Stored { uint32_t magic; float r0; };
static float r0 = 0.0f;                              // 0 = not calibrated yet

static bool heaterHigh = true;
static uint32_t phaseStart = 0;
static uint32_t cycles = 0;
static bool calRequested = false;
static double sampleSum = 0;
static uint32_t sampleCount = 0;
static char cmd[16];
static uint8_t cmdLen = 0;

static void setHeater(bool high) {
  heaterHigh = high;
  const uint32_t full = (1u << 12) - 1;
  const float lowDuty = (LOW_PHASE_V / SUPPLY_V) * (LOW_PHASE_V / SUPPLY_V);
  analogWrite(PIN_HEATER, high ? full : (uint32_t)(lowDuty * full + 0.5f));
  digitalWrite(PIN_LED, high ? LOW : HIGH);
  phaseStart = millis();
  sampleSum = 0;
  sampleCount = 0;
}

static float sensorOhms(float adcAvg) {
  const float vout = adcAvg / 4095.0f * ADC_REF_V * DIVIDER;
  if (vout < 0.01f) return INFINITY;                  // open circuit / no sensor
  return LOAD_OHMS * (SUPPLY_V - vout) / vout;
}

static void loadR0() {
  Stored s;
  EEPROM.get(0, s);
  if (s.magic == R0_MAGIC && s.r0 > 0 && isfinite(s.r0)) r0 = s.r0;
}

static void saveR0(float value) {
  Stored s = {R0_MAGIC, value};
  EEPROM.put(0, s);
  r0 = value;
}

static void finishCycle() {
  cycles++;
  if (sampleCount == 0) return;
  const float adcAvg = (float)(sampleSum / sampleCount);
  const float vout = adcAvg / 4095.0f * ADC_REF_V * DIVIDER;
  const float rs = sensorOhms(adcAvg);
  if (!isfinite(rs)) {
    PiSerial.println("# error: sensor output ~0 V (wiring?)");
    return;
  }
  if (calRequested) {
    saveR0(rs / CLEAN_AIR_RATIO);
    calRequested = false;
    PiSerial.print("# R0=");
    PiSerial.println(r0, 0);
  }
  PiSerial.print("# vout=");
  PiSerial.print(vout, 3);
  PiSerial.print(" rs=");
  PiSerial.print(rs, 0);
  PiSerial.print(" r0=");
  PiSerial.print(r0, 0);
  if (r0 > 0) {
    PiSerial.print(" ratio=");
    PiSerial.print(rs / r0, 2);
  }
  PiSerial.print(" cycle=");
  PiSerial.println(cycles);
  if (r0 <= 0) {
    PiSerial.println("# not calibrated: send CAL in clean air");
    return;
  }
  float ppm = CURVE_A * powf(rs / r0, CURVE_B);
  if (ppm < 0) ppm = 0;
  if (ppm > 4000) ppm = 4000;                         // beyond the MQ-7's range
  PiSerial.print("CO:");
  PiSerial.println(ppm, 1);
}

static void handleCommand() {
  cmd[cmdLen] = 0;
  if (strcmp(cmd, "CAL") == 0) {
    calRequested = true;
    PiSerial.println("# CAL: R0 is set at the end of this cycle (keep the sensor in clean air)");
  } else if (strcmp(cmd, "STATUS") == 0) {
    PiSerial.print("# phase=");
    PiSerial.print(heaterHigh ? "5.0V" : "1.4V");
    PiSerial.print(" elapsed_s=");
    PiSerial.print((millis() - phaseStart) / 1000);
    PiSerial.print(" r0=");
    PiSerial.print(r0, 0);
    PiSerial.print(" cycles=");
    PiSerial.println(cycles);
  } else if (cmdLen) {
    PiSerial.println("# unknown command (CAL, STATUS)");
  }
  cmdLen = 0;
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  analogReadResolution(12);
  analogWriteResolution(12);
  analogWriteFrequency(PWM_HZ);
  PiSerial.begin(115200);
  loadR0();
  setHeater(true);
  PiSerial.println("# IMM-OS MQ-7 controller started (60 s @ 5 V, 90 s @ 1.4 V)");
}

void loop() {
  while (PiSerial.available()) {
    const char c = (char)PiSerial.read();
    if (c == '\n' || c == '\r') {
      handleCommand();
    } else if (cmdLen < sizeof(cmd) - 1) {
      cmd[cmdLen++] = (char)toupper(c);
    }
  }

  const uint32_t elapsed = millis() - phaseStart;
  if (heaterHigh) {
    if (elapsed >= HIGH_MS) setHeater(false);
    return;
  }
  if (elapsed >= LOW_MS - SAMPLE_WINDOW_MS) {
    sampleSum += analogRead(PIN_SENSE);
    sampleCount++;
    delay(20);
  }
  if (elapsed >= LOW_MS) {
    finishCycle();
    setHeater(true);
  }
}
