// Stand-in for the ESP32 WiFi API: connects at once when credentials are given (test/sim.cpp).
#pragma once
#include "Arduino.h"
#define WIFI_STA 1
#define WL_CONNECTED 3
namespace sim { extern std::string wifi_ssid, wifi_pass; extern bool wifi_up; }
struct IPAddress { String toString() const { return String("192.168.1.77"); } };
class WiFiClass {
 public:
  void mode(int) {}
  void setAutoReconnect(bool) {}
  void setHostname(const char*) {}
  void begin(const char* s, const char* p) { sim::wifi_ssid = s; sim::wifi_pass = p; sim::wifi_up = true; }
  void disconnect(bool) { sim::wifi_up = false; }
  int status() { return sim::wifi_up ? WL_CONNECTED : 0; }
  int RSSI() { return -58; }
  IPAddress localIP() { return IPAddress(); }
};
inline WiFiClass WiFi;
