// In-memory stand-in for the ESP32 NVS Preferences API (kept across simulated resets).
#pragma once
#include <map>
#include <string>
#include <cstdint>
#include <cstring>
#include <vector>
#include "Arduino.h"
namespace sim { extern std::map<std::string, float> nvs; extern std::map<std::string, std::vector<uint8_t>> nvsb;
                extern std::map<std::string, std::string> nvss; }
class Preferences {
 public:
  bool begin(const char*, bool) { return true; }
  bool isKey(const char* k) { return sim::nvs.count(k) > 0; }
  float getFloat(const char* k, float def) { auto it = sim::nvs.find(k); return it == sim::nvs.end() ? def : it->second; }
  size_t putFloat(const char* k, float v) { sim::nvs[k] = v; return 4; }
  uint32_t getUInt(const char* k, uint32_t def) { auto it = sim::nvs.find(k); return it == sim::nvs.end() ? def : (uint32_t)it->second; }
  size_t putUInt(const char* k, uint32_t v) { sim::nvs[k] = (float)v; return 4; }
  size_t getBytesLength(const char* k) { auto it = sim::nvsb.find(k); return it == sim::nvsb.end() ? 0 : it->second.size(); }
  size_t getBytes(const char* k, void* buf, size_t n) {
    auto it = sim::nvsb.find(k);
    if (it == sim::nvsb.end() || it->second.size() > n) return 0;
    memcpy(buf, it->second.data(), it->second.size());
    return it->second.size();
  }
  size_t putBytes(const char* k, const void* buf, size_t n) {
    sim::nvsb[k] = std::vector<uint8_t>((const uint8_t*)buf, (const uint8_t*)buf + n);
    return n;
  }
  String getString(const char* k, const char* def) { auto it = sim::nvss.find(k); return it == sim::nvss.end() ? String(def) : String(it->second); }
  size_t putString(const char* k, const char* v) { sim::nvss[k] = v; return strlen(v); }
  bool remove(const char* k) { return sim::nvs.erase(k) + sim::nvsb.erase(k) + sim::nvss.erase(k) > 0; }
  void end() {}
};
