// Stand-in for esp_reset_reason() (test/sim.cpp sets what the "hardware" reports).
#pragma once
namespace sim { extern int reset_reason; }
inline int esp_reset_reason() { return sim::reset_reason; }
