// Runs the ESP32 sensor-board firmware on a PC against simulated I2C devices, ADC and USB serial.
//   sim <script>   script lines:
//     run SECONDS                 advance time, calling loop()
//     send TEXT                   a line from the Pi
//     mq4 MILLIVOLTS              voltage on GPIO32
//     bme ADC_T ADC_P ADC_H       raw BME280 readings (datasheet calibration below)
//     scd CO2 T_RAW RH_RAW        raw SCD40 words (a new measurement every 5 s while running)
//     bno H R P AX AY AZ CALIB    BNO055 register values (1/16 deg, 1/100 m/s², CALIB_STAT)
//     bnox MX MY MZ GX GY GZ VX VY VZ TEMP   BNO055 magnetometer (1/16 µT), gyro (1/16 °/s),
//                                 gravity (1/100 m/s²), temperature (°C)
//     bnost BITS                  BNO055 self-test result (ST_RESULT) after its next reset
//     o2 KEY D0 D1 D2             SEN0322 key register and oxygen data registers
//     remove DEV | add DEV        take a device off / put it back on the bus (bme scd bno o2)
//     bmp                         the BME280 answers as a BMP280 (chip ID 0x58)
//     bmeignore N                 the BME280 ignores its next N writes to ctrl_meas
//     bmereset                    the BME280 power-on resets (a supply dip): settings and data cleared
//     reset                       reboot the ESP32 (flash contents kept)
//     o2user                      print what CAL_O2 wrote to the SEN0322
// Everything the firmware prints goes to stdout.
#include <fstream>
#include <iostream>
#include <sstream>
#include "Arduino.h"
#include "Preferences.h"
#include "Wire.h"

namespace sim {
uint32_t now_ms = 0, mq4_pin_mv = 0;
std::string rx, tx;
std::map<std::string, float> nvs;
std::map<uint8_t, I2CDevice*> bus;
}
HardwareSerial Serial;
TwoWire Wire;

// Register-file device: first written byte selects the register; further bytes write
// consecutive registers; reads auto-increment. (BME280, BNO055 and SEN0322 all work this way.)
struct RegDevice : I2CDevice {
  uint8_t reg[256] = {0};
  uint8_t ptr = 0;
  std::map<uint8_t, int> writes;                               // last value written per register
  void write(const std::vector<uint8_t>& b) override {
    ptr = b[0];
    for (size_t i = 1; i < b.size(); i++) {
      writes[ptr] = b[i];
      if (accept(ptr, b[i])) { reg[ptr] = b[i]; stored(ptr, b[i]); }
      ptr++;
    }
  }
  virtual bool accept(uint8_t, uint8_t) { return true; }   // false: the chip ignores this write
  virtual void stored(uint8_t, uint8_t) {}                  // after a write took effect
  std::vector<uint8_t> read(size_t n) override {
    std::vector<uint8_t> out;
    for (size_t i = 0; i < n; i++) out.push_back(reg[(uint8_t)(ptr + i)]);
    ptr = (uint8_t)(ptr + n);
    return out;
  }
  void put16le(uint8_t r, int v) { reg[r] = v & 0xFF; reg[r + 1] = (v >> 8) & 0xFF; }
};

struct Bme280Dev : RegDevice {
  int ignoreCtrl = 0;                                          // ignore this many writes to ctrl_meas (0xF4)
  bool accept(uint8_t r, uint8_t) override {
    if (r == 0xE0) return false;                               // reset command, not a stored register
    if (r == 0xF4 && ignoreCtrl > 0) { ignoreCtrl--; return false; }
    return true;
  }
  int rawT = 0x80000, rawP = 0x80000, rawH = 0x8000;           // what the next measurement reads
  // ctrl_meas mode bits: 01/10 forced (one measurement, back to sleep), 11 normal (continuous)
  void stored(uint8_t r, uint8_t v) override {
    if (r != 0xF4 || !(v & 3)) return;
    setData(rawT, rawP, rawH);
    if ((v & 3) != 3) reg[0xF4] = v & 0xFC;
  }
  void powerOnReset() {
    reg[0xF2] = reg[0xF4] = reg[0xF5] = 0;
    setData(0x80000, 0x80000, 0x8000);
  }
  Bme280Dev() {
    reg[0xD0] = 0x60;
    // BME280 datasheet example calibration (T, P) and typical humidity trimming values
    const int c[] = {27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000};
    for (int i = 0; i < 12; i++) put16le(0x88 + 2 * i, c[i]);
    reg[0xA1] = 75;                                            // H1
    put16le(0xE1, 362);                                        // H2
    reg[0xE3] = 0;                                             // H3
    const int h4 = 313, h5 = 50;
    reg[0xE4] = (h4 >> 4) & 0xFF; reg[0xE5] = (h4 & 0x0F) | ((h5 & 0x0F) << 4); reg[0xE6] = (h5 >> 4) & 0xFF;
    reg[0xE7] = 30;                                            // H6
    setData(0x80000, 0x80000, 0x8000);                         // "no measurement" until one is made
  }
  void setRaw(int t, int p, int h) {
    rawT = t; rawP = p; rawH = h;
    if ((reg[0xF4] & 3) == 3) setData(t, p, h);                  // normal mode: measuring all the time
  }
  void setData(int t, int p, int h) {
    reg[0xF7] = p >> 12; reg[0xF8] = (p >> 4) & 0xFF; reg[0xF9] = (p & 0x0F) << 4;
    reg[0xFA] = t >> 12; reg[0xFB] = (t >> 4) & 0xFF; reg[0xFC] = (t & 0x0F) << 4;
    reg[0xFD] = h >> 8; reg[0xFE] = h & 0xFF;
  }
};

struct Scd40Dev : I2CDevice {
  bool running = false;
  uint32_t nextReady = 0;
  uint16_t words[3] = {0, 0, 0};
  std::vector<uint8_t> reply;
  static uint8_t crc(uint8_t a, uint8_t b) {
    uint8_t c = 0xFF, d[2] = {a, b};
    for (uint8_t x : d) { c ^= x; for (int i = 0; i < 8; i++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1); }
    return c;
  }
  void pushWord(uint16_t w) { reply.push_back(w >> 8); reply.push_back(w & 0xFF); reply.push_back(crc(w >> 8, w & 0xFF)); }
  bool ready() const { return running && sim::now_ms >= nextReady; }
  void write(const std::vector<uint8_t>& b) override {
    const uint16_t cmd = (uint16_t)(b[0] << 8 | b[1]);
    reply.clear();
    if (cmd == 0x21B1 && !running) { running = true; nextReady = sim::now_ms + 5000; }
    else if (cmd == 0x3F86) running = false;
    else if (cmd == 0xE4B8) pushWord(ready() ? 0x8006 : 0x8000);
    else if (cmd == 0xEC05 && ready()) { for (uint16_t w : words) pushWord(w); nextReady = sim::now_ms + 5000; }
  }
  std::vector<uint8_t> read(size_t n) override {
    std::vector<uint8_t> out(reply.begin(), reply.begin() + std::min(n, reply.size()));
    return out;
  }
};

struct Bno055Dev : RegDevice {
  Bno055Dev() { reg[0x00] = 0xA0; reg[0x36] = 0x0F; }          // chip ID; self-test all passed
};

struct Sen0322Dev : RegDevice {};

static Bme280Dev bmeDev;
static Scd40Dev scdDev;
static Bno055Dev bnoDev;
static Sen0322Dev o2Dev;

#include "../src/main.cpp"

static uint8_t bmeAddr = 0x76;
static void attach(const std::string& d, bool on) {
  const std::map<std::string, std::pair<uint8_t, I2CDevice*>> devs = {
      {"bme", {bmeAddr, &bmeDev}}, {"scd", {0x62, &scdDev}}, {"bno", {0x28, &bnoDev}}, {"o2", {0x73, &o2Dev}}};
  const auto& e = devs.at(d);
  if (on) sim::bus[e.first] = e.second; else sim::bus.erase(e.first);
}

static void flush() { std::cout << sim::tx; sim::tx.clear(); }

int main(int, char** argv) {
  for (const char* d : {"bme", "scd", "bno", "o2"}) attach(d, true);
  std::ifstream in(argv[1]);
  std::string line;
  bool started = false;
  while (std::getline(in, line)) {
    std::istringstream ss(line);
    std::string op; ss >> op;
    if (!started && op != "remove" && op != "bmp" && op != "o2" && op != "mq4" && op != "bmeignore" && op != "bnost") { setup(); started = true; flush(); }
    if (op == "run") {
      double s; ss >> s;
      const uint32_t end = sim::now_ms + (uint32_t)(s * 1000);
      while (sim::now_ms < end) { const uint32_t t = sim::now_ms; loop(); if (sim::now_ms == t) sim::now_ms += 10; }
    } else if (op == "send") { std::string rest; std::getline(ss, rest); sim::rx += rest.substr(1) + "\n"; }
    else if (op == "mq4") ss >> sim::mq4_pin_mv;
    else if (op == "bme") { int t, p, h; ss >> t >> p >> h; bmeDev.setRaw(t, p, h); }
    else if (op == "scd") { ss >> scdDev.words[0] >> scdDev.words[1] >> scdDev.words[2]; }
    else if (op == "bno") {
      int v[6], c; for (int& x : v) ss >> x; ss >> c;
      for (int i = 0; i < 3; i++) bnoDev.put16le(0x1A + 2 * i, v[i]);
      for (int i = 0; i < 3; i++) bnoDev.put16le(0x28 + 2 * i, v[3 + i]);
      bnoDev.reg[0x35] = (uint8_t)c;
    } else if (op == "bnox") {
      int v[9], t; for (int& x : v) ss >> x; ss >> t;
      for (int i = 0; i < 3; i++) bnoDev.put16le(0x0E + 2 * i, v[i]);
      for (int i = 0; i < 3; i++) bnoDev.put16le(0x14 + 2 * i, v[3 + i]);
      for (int i = 0; i < 3; i++) bnoDev.put16le(0x2E + 2 * i, v[6 + i]);
      bnoDev.reg[0x34] = (uint8_t)(int8_t)t;
    } else if (op == "bnost") { int b; ss >> b; bnoDev.reg[0x36] = (uint8_t)b;
    } else if (op == "o2") {
      int k, a, b, c; ss >> k >> a >> b >> c;
      o2Dev.reg[0x0A] = k; o2Dev.reg[0x03] = a; o2Dev.reg[0x04] = b; o2Dev.reg[0x05] = c;
    } else if (op == "remove" || op == "add") { std::string d; ss >> d; attach(d, op == "add"); }
    else if (op == "bmp") bmeDev.reg[0xD0] = 0x58;
    else if (op == "bmeignore") ss >> bmeDev.ignoreCtrl;
    else if (op == "bmereset") bmeDev.powerOnReset();
    else if (op == "o2user") std::cout << "O2USER " << o2Dev.writes[0x08] << "\n";
    else if (op == "reset") {
      bme.addr = 0; bmeFails = 0; bmeResets = 0; bmeSeenOk = false; scdZeroes = 0; scdFound = false; bnoAddr = 0; o2Addr = 0; mq4R0 = 0; mq4CalLeft = 0; mq4CalSum = 0;
      lastSample = lastProbe = 0; cmdLen = 0; sim::now_ms = 0; scdDev.running = scdDev.running;   // the SCD40 keeps measuring
      setup();
    }
    flush();
  }
  return 0;
}
