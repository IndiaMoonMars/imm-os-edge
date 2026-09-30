// Runs the MQ-7 firmware against a simulated clock, ADC and UART.
//   sim <script>  — script lines: "adc N", "send TEXT", "run SECONDS", "reset", "heater",
//                   "wdtreset" (the next reset is by the watchdog), "wdt" (longest reload gap, timeout)
// Everything the firmware prints goes to stdout; "heater" prints the current PWM value.
#include <fstream>
#include <iostream>
#include <sstream>
#include "Arduino.h"
#include "EEPROM.h"
#include "IWatchdog.h"
namespace sim {
uint32_t now_ms = 0; int adc = 0; uint32_t heater = 0; std::string rx, tx; unsigned char eeprom[64] = {0};
uint32_t iwdg_timeout_us = 0, iwdg_last = 0, iwdg_max_gap = 0; bool iwdg_was_reset = false;
}
#include "../src/main.cpp"

static void flush() { std::cout << sim::tx; sim::tx.clear(); }

int main(int, char** argv) {
  std::ifstream in(argv[1]);
  std::string line;
  setup();
  flush();
  while (std::getline(in, line)) {
    std::istringstream ss(line);
    std::string op; ss >> op;
    if (op == "adc") ss >> sim::adc;
    else if (op == "send") { std::string rest; std::getline(ss, rest); sim::rx += rest.substr(1) + "\n"; }
    else if (op == "heater") std::cout << "HEATER " << sim::heater << "\n";
    else if (op == "reset") { cmdLen = 0; calRequested = false; r0 = 0; setup(); }
    else if (op == "wdtreset") sim::iwdg_was_reset = true;
    else if (op == "wdt") std::cout << "WDT " << sim::iwdg_max_gap << " " << sim::iwdg_timeout_us / 1000 << "\n";
    else if (op == "run") {
      double s; ss >> s;
      const uint32_t end = sim::now_ms + (uint32_t)(s * 1000);
      while (sim::now_ms < end) { const uint32_t t = sim::now_ms; loop(); if (sim::now_ms == t) sim::now_ms += 10; }
    }
    flush();
  }
  return 0;
}
