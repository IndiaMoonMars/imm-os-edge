// Stand-in for the ESP-IDF task watchdog: records the longest gap between feeds (test/sim.cpp).
#pragma once
#include <cstdint>
typedef int esp_err_t;
#define ESP_OK 0
namespace sim {
extern uint32_t now_ms, wdt_timeout_s, wdt_last_feed, wdt_max_gap;
extern bool wdt_added;
}
inline esp_err_t esp_task_wdt_init(uint32_t timeout_s, bool) { sim::wdt_timeout_s = timeout_s; return ESP_OK; }
inline esp_err_t esp_task_wdt_add(void*) { sim::wdt_added = true; sim::wdt_last_feed = sim::now_ms; return ESP_OK; }
inline esp_err_t esp_task_wdt_reset() {
  if (sim::wdt_added && sim::now_ms - sim::wdt_last_feed > sim::wdt_max_gap) sim::wdt_max_gap = sim::now_ms - sim::wdt_last_feed;
  sim::wdt_last_feed = sim::now_ms;
  return ESP_OK;
}
