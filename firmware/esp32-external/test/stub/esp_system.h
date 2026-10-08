// Stand-in for esp_reset_reason() (test/sim.cpp sets what the "hardware" reports) and the heap.
#pragma once
#include <cstdint>
namespace sim { extern int reset_reason; extern int reboots; extern uint32_t heap_free, heap_min; }
inline int esp_reset_reason() { return sim::reset_reason; }
inline void esp_restart() { sim::reboots++; }
inline uint32_t esp_get_free_heap_size() { return sim::heap_free; }
inline uint32_t esp_get_minimum_free_heap_size() { return sim::heap_min; }
