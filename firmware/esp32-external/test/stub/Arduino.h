// Minimal Arduino-ESP32 stand-in so src/main.cpp runs on a PC (test/sim.cpp).
#pragma once
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <strings.h>

namespace sim {
extern uint32_t now_ms;
extern uint64_t now_us;
extern std::string rx, tx;
extern int sda_stuck_clocks;
extern int scl_pulses;
extern void (*isr)();
}

inline uint32_t millis() { return sim::now_ms; }
inline uint32_t micros() { return (uint32_t)sim::now_us; }
inline void delay(uint32_t ms) { sim::now_ms += ms; sim::now_us += ms * 1000ull; }
inline void delayMicroseconds(uint32_t) {}

#define IRAM_ATTR
#define LOW 0
#define HIGH 1
#define INPUT 0x01
#define INPUT_PULLUP 0x05
#define OUTPUT_OPEN_DRAIN 0x12
#define FALLING 0x02
inline void pinMode(int, int) {}
inline int digitalRead(int pin) { return pin == 21 && sim::sda_stuck_clocks > 0 ? LOW : HIGH; }
inline void digitalWrite(int pin, int v) {
  static int scl = HIGH;
  if (pin == 22) {
    if (scl == LOW && v == HIGH) { sim::scl_pulses++; if (sim::sda_stuck_clocks > 0) sim::sda_stuck_clocks--; }
    scl = v;
  }
}
inline int digitalPinToInterrupt(int p) { return p; }
inline void attachInterrupt(int, void (*f)(), int) { sim::isr = f; }
inline void noInterrupts() {}
inline void interrupts() {}

class String {
  std::string s_;
 public:
  String(const char* s = "") : s_(s) {}
  String(const std::string& s) : s_(s) {}
  const char* c_str() const { return s_.c_str(); }
};

class HardwareSerial {
 public:
  void begin(uint32_t) {}
  int available() { return (int)sim::rx.size(); }
  int read() { int c = (unsigned char)sim::rx[0]; sim::rx.erase(0, 1); return c; }
  void print(const char* s) { sim::tx += s; }
  void println(const char* s) { sim::tx += s; sim::tx += "\n"; }
};
extern HardwareSerial Serial;
