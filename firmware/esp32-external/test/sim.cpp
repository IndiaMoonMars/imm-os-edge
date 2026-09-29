// Runs the external-board firmware on a PC against a simulated TEL0157, Geiger tube, Wi-Fi and serial.
//   sim <script>   script lines:
//     run SECONDS                 advance time, calling loop() (Geiger pulses arrive at the set rate)
//     send TEXT                   a line from the Pi (USB serial)
//     cpm N                       background radiation: N pulses per minute, evenly spaced
//     burst N                     N pulses 20 µs apart right now (ringing: counts as one)
//     gnss Y M D h m s LAT LON SATS ALT     a fix (decimal degrees, negative = S / W; ALT may be negative)
//     nofix SATS                  receiver on, no fix yet
//     remove | add                take the GNSS off / put it back on the bus
//     i2cstuck N | resetreason R | reset | wdt | http PATH | wifi
#include <fstream>
#include <iostream>
#include <sstream>
#include "Arduino.h"
#include "Preferences.h"
#include "WebServer.h"
#include "WiFi.h"
#include "Wire.h"
#include "esp_system.h"
#include "esp_task_wdt.h"

namespace sim {
uint32_t now_ms = 0; uint64_t now_us = 0;
std::string rx, tx, wifi_ssid, wifi_pass;
bool wifi_up = false, wifi_stack = false;
int reset_reason = 1, sda_stuck_clocks = 0, scl_pulses = 0, wire_restarts = 0;
uint32_t wdt_timeout_s = 0, wdt_last_feed = 0, wdt_max_gap = 0;
bool wdt_added = false;
void (*isr)() = nullptr;
std::map<std::string, std::string> nvs;
std::map<uint8_t, I2CDevice*> bus;
}
HardwareSerial Serial;
TwoWire Wire;

struct GnssDev : I2CDevice {
  uint8_t reg[64] = {0};
  uint8_t ptr = 0;
  void write(const std::vector<uint8_t>& b) override { ptr = b[0]; for (size_t i = 1; i < b.size(); i++) reg[ptr + i - 1] = b[i]; }
  std::vector<uint8_t> read(size_t n) override {
    std::vector<uint8_t> out;
    for (size_t i = 0; i < n; i++) out.push_back(reg[(ptr + i) & 63]);
    return out;
  }
  static void put3(uint8_t* r, double v) {        // 15-bit integer (bit 7 = negative) + hundredths
    const double a = std::fabs(v);
    const int i = (int)a, h = (int)std::lround((a - i) * 100);
    r[0] = (uint8_t)(((i >> 8) & 0x7F) | (v < 0 ? 0x80 : 0)); r[1] = i & 0xFF; r[2] = (uint8_t)h;
  }
  static void putDeg(uint8_t* r, double deg) {    // DD, MM, MMMMM (1/100000 minute)
    const double a = std::fabs(deg);
    const int dd = (int)a;
    const double min = (a - dd) * 60;
    const int mm = (int)min;
    const uint32_t f = (uint32_t)std::lround((min - mm) * 100000);
    r[0] = dd; r[1] = mm; r[2] = f >> 16; r[3] = (f >> 8) & 0xFF; r[4] = f & 0xFF;
  }
};
static GnssDev gnssDev;

#include "../src/main.cpp"

static double cpmRate = 0, pulseAcc = 0;
static void flush() { std::cout << sim::tx; sim::tx.clear(); }

int main(int, char** argv) {
  sim::bus[0x20] = &gnssDev;
  std::ifstream in(argv[1]);
  std::string line;
  bool started = false;
  while (std::getline(in, line)) {
    std::istringstream ss(line);
    std::string op; ss >> op;
    if (!started && op != "remove" && op != "resetreason" && op != "gnss" && op != "nofix" && op != "cpm" && op != "i2cstuck") { setup(); started = true; flush(); }
    if (op == "run") {
      double s; ss >> s;
      const uint32_t end = sim::now_ms + (uint32_t)(s * 1000);
      while (sim::now_ms < end) {
        loop();
        sim::now_ms += 10; sim::now_us += 10000;
        pulseAcc += cpmRate / 6000.0;                        // pulses per 10 ms
        while (pulseAcc >= 1 && sim::isr) { sim::now_us += 300; sim::isr(); pulseAcc -= 1; }
      }
    } else if (op == "send") { std::string rest; std::getline(ss, rest); sim::rx += rest.substr(1) + "\n"; }
    else if (op == "cpm") ss >> cpmRate;
    else if (op == "burst") { int n; ss >> n; for (int i = 0; i < n; i++) { sim::now_us += 20; if (sim::isr) sim::isr(); } }
    else if (op == "gnss") {
      int y, mo, d, h, mi, se, sats; double lat, lon, alt;
      ss >> y >> mo >> d >> h >> mi >> se >> lat >> lon >> sats >> alt;
      uint8_t* r = gnssDev.reg;
      r[0] = y >> 8; r[1] = y & 0xFF; r[2] = mo; r[3] = d; r[4] = h; r[5] = mi; r[6] = se;
      GnssDev::putDeg(&r[7], lat); r[18] = lat < 0 ? 'S' : 'N';
      GnssDev::putDeg(&r[13], lon); r[12] = lon < 0 ? 'W' : 'E';
      r[19] = sats; GnssDev::put3(&r[20], alt); GnssDev::put3(&r[23], 0.12); GnssDev::put3(&r[26], 87.5);
    } else if (op == "nofix") { int s; ss >> s; memset(gnssDev.reg, 0, 35); gnssDev.reg[19] = s; }
    else if (op == "remove") sim::bus.erase(0x20);
    else if (op == "add") sim::bus[0x20] = &gnssDev;
    else if (op == "i2cstuck") ss >> sim::sda_stuck_clocks;
    else if (op == "resetreason") ss >> sim::reset_reason;
    else if (op == "wdt") std::cout << "WDT " << sim::wdt_max_gap << " " << sim::wdt_timeout_s * 1000 << "\n";
    else if (op == "wifi") std::cout << "WIFI " << sim::wifi_ssid << " " << sim::wifi_pass << " " << sim::wifi_up << "\n";
    else if (op == "http") { std::string p; ss >> p; web.routes.at(p)(); std::cout << "HTTP " << web.code << " " << web.type << " " << web.body.substr(0, 400) << "\n"; }
    else if (op == "reset") {
      gnssFound = false; i2cErr = 0; i2cStreak = 0; i2cRecoveries = 0; lastSample = lastProbe = lastBoard = lastRecovery = 0;
      geigerPulses = geigerSeen = geigerTotal = 0; bucketPos = bucketsFilled = 0; memset(bucket, 0, sizeof bucket);
      wifiSsid[0] = wifiPass[0] = 0; sim::wifi_up = false; sim::now_ms = 0; sim::wdt_added = false; sim::wdt_max_gap = 0;
      setup();
    }
    flush();
  }
  return 0;
}
