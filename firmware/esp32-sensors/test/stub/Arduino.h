// Minimal Arduino-ESP32 stand-in so src/main.cpp runs on a PC (test/sim.cpp).
#pragma once
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

namespace sim {
extern uint32_t now_ms;
extern uint32_t mq4_pin_mv;   // what analogReadMilliVolts(32) returns
extern std::string rx, tx;    // bytes from / to the Pi
extern int sda_stuck_clocks;  // > 0: a device holds SDA low until SCL is clocked this many more times
extern int scl_pulses;
}

inline uint32_t millis() { return sim::now_ms; }
inline void delay(uint32_t ms) { sim::now_ms += ms; }
inline void delayMicroseconds(uint32_t) {}
inline uint32_t analogReadMilliVolts(uint8_t) { return sim::mq4_pin_mv; }

#define LOW 0
#define HIGH 1
#define INPUT_PULLUP 0x05
#define OUTPUT_OPEN_DRAIN 0x12
inline void pinMode(int, int) {}
inline int digitalRead(int pin) { return pin == 21 && sim::sda_stuck_clocks > 0 ? LOW : HIGH; }
inline void digitalWrite(int pin, int v) {        // a rising edge on SCL (GPIO22) clocks the stuck device on
  static int scl = HIGH;
  if (pin == 22) {
    if (scl == LOW && v == HIGH) { sim::scl_pulses++; if (sim::sda_stuck_clocks > 0) sim::sda_stuck_clocks--; }
    scl = v;
  }
}

class HardwareSerial {
 public:
  void begin(uint32_t) {}
  void flush() {}
  int available() { return (int)sim::rx.size(); }
  int read() { int c = (unsigned char)sim::rx[0]; sim::rx.erase(0, 1); return c; }
  void print(const char* s) { sim::tx += s; }
  void println(const char* s) { sim::tx += s; sim::tx += "\n"; }
};
extern HardwareSerial Serial;

class String {
  std::string s_;
 public:
  String(const char* s = "") : s_(s) {}
  String(const std::string& s) : s_(s) {}
  const char* c_str() const { return s_.c_str(); }
};
