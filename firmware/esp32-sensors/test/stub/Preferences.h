// In-memory stand-in for the ESP32 NVS Preferences API (kept across simulated resets).
#pragma once
#include <map>
#include <string>
namespace sim { extern std::map<std::string, float> nvs; }
class Preferences {
 public:
  bool begin(const char*, bool) { return true; }
  float getFloat(const char* k, float def) { auto it = sim::nvs.find(k); return it == sim::nvs.end() ? def : it->second; }
  size_t putFloat(const char* k, float v) { sim::nvs[k] = v; return 4; }
};
