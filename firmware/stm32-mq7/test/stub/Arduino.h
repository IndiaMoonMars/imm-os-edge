// Minimal Arduino/STM32duino stand-in so src/main.cpp can run on a PC (test/sim.cpp).
#pragma once
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

enum : uint32_t { PA0 = 0, PA8 = 8, PA9 = 9, PA10 = 10, PC13 = 45 };
enum { LOW = 0, HIGH = 1, OUTPUT = 1 };

namespace sim {
extern uint32_t now_ms;
extern int adc;             // value analogRead(PA0) returns
extern uint32_t heater;     // last analogWrite(PA8) value
extern std::string rx;      // bytes the Pi sent
extern std::string tx;      // bytes the board sent
}

inline uint32_t millis() { return sim::now_ms; }
inline void delay(uint32_t ms) { sim::now_ms += ms; }
inline void pinMode(uint32_t, int) {}
inline void digitalWrite(uint32_t, int) {}
inline int analogRead(uint32_t) { return sim::adc; }
inline void analogWrite(uint32_t pin, uint32_t v) { if (pin == PA8) sim::heater = v; }
inline void analogReadResolution(int) {}
inline void analogWriteResolution(int) {}
inline void analogWriteFrequency(uint32_t) {}

class HardwareSerial {
 public:
  HardwareSerial(uint32_t, uint32_t) {}
  void begin(uint32_t) {}
  int available() { return (int)sim::rx.size(); }
  int read() { int c = (unsigned char)sim::rx[0]; sim::rx.erase(0, 1); return c; }
  void print(const char* s) { sim::tx += s; }
  void print(float v, int digits) { char b[32]; snprintf(b, sizeof b, "%.*f", digits, v); sim::tx += b; }
  void print(uint32_t v) { sim::tx += std::to_string(v); }
  void println(const char* s) { print(s); sim::tx += "\n"; }
  void println(float v, int digits) { print(v, digits); sim::tx += "\n"; }
  void println(uint32_t v) { print(v); sim::tx += "\n"; }
};
