// Stand-in for the ESP32 ESPmDNS API (test/sim.cpp records the announced hostname).
#pragma once
#include "Arduino.h"
namespace sim { extern std::string mdns_host; extern bool mdns_up; }
class MDNSResponder {
 public:
  bool begin(const char* host) { sim::mdns_host = host; sim::mdns_up = true; return true; }
  void addService(const char*, const char*, uint16_t) {}
  void end() { sim::mdns_up = false; }
};
inline MDNSResponder MDNS;
