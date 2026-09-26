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
}

inline uint32_t millis() { return sim::now_ms; }
inline void delay(uint32_t ms) { sim::now_ms += ms; }
inline uint32_t analogReadMilliVolts(uint8_t) { return sim::mq4_pin_mv; }

class HardwareSerial {
 public:
  void begin(uint32_t) {}
  int available() { return (int)sim::rx.size(); }
  int read() { int c = (unsigned char)sim::rx[0]; sim::rx.erase(0, 1); return c; }
  void print(const char* s) { sim::tx += s; }
  void println(const char* s) { sim::tx += s; sim::tx += "\n"; }
};
extern HardwareSerial Serial;
