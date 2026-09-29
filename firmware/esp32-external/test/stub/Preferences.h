// In-memory stand-in for the ESP32 NVS Preferences API (kept across simulated resets).
#pragma once
#include <cstdint>
#include <map>
#include <string>
#include "Arduino.h"
namespace sim { extern std::map<std::string, std::string> nvs; }
class Preferences {
 public:
  bool begin(const char*, bool) { return true; }
  uint32_t getUInt(const char* k, uint32_t def) { auto it = sim::nvs.find(k); return it == sim::nvs.end() ? def : (uint32_t)std::stoul(it->second); }
  size_t putUInt(const char* k, uint32_t v) { sim::nvs[k] = std::to_string(v); return 4; }
  String getString(const char* k, const char* def) { auto it = sim::nvs.find(k); return it == sim::nvs.end() ? String(def) : String(it->second); }
  size_t putString(const char* k, const char* v) { sim::nvs[k] = v; return strlen(v); }
  void end() {}
};
