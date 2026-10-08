// Stand-in for STM32duino's IWatchdog: records the longest gap between reloads (test/sim.cpp).
#pragma once
#include <cstdint>
namespace sim { extern uint32_t now_ms, iwdg_timeout_us, iwdg_last, iwdg_max_gap; extern bool iwdg_was_reset; }
class IWatchdogClass {
 public:
  void begin(uint32_t timeout_us) { sim::iwdg_timeout_us = timeout_us; sim::iwdg_last = sim::now_ms; }
  void reload() {
    if (sim::iwdg_timeout_us && sim::now_ms - sim::iwdg_last > sim::iwdg_max_gap) sim::iwdg_max_gap = sim::now_ms - sim::iwdg_last;
    sim::iwdg_last = sim::now_ms;
  }
  bool isReset(bool clear = false) { bool r = sim::iwdg_was_reset; if (clear) sim::iwdg_was_reset = false; return r; }
};
inline IWatchdogClass IWatchdog;
