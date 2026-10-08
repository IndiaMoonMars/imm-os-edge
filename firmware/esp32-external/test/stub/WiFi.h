// Stand-in for the ESP32 WiFi API: connects at once when credentials are given (test/sim.cpp).
#pragma once
#include <functional>
#include "Arduino.h"
#define WIFI_STA 1
#define WL_CONNECTED 3
#define ARDUINO_EVENT_WIFI_STA_DISCONNECTED 5
typedef int WiFiEvent_t;
struct WiFiEventInfo_t { struct { int reason; } wifi_sta_disconnected; };
namespace sim {
extern std::string wifi_ssid, wifi_pass;
extern bool wifi_up, wifi_stack, wifi_blocked, wifi_sleep;
extern int wifi_begins;
extern std::function<void(WiFiEvent_t, WiFiEventInfo_t)> wifi_on_disconnect;
}
struct IPAddress { String toString() const { return String("192.168.1.77"); } };
class WiFiClass {
 public:
  void mode(int) { sim::wifi_stack = true; }                       // brings the network stack up
  void setAutoReconnect(bool) {}
  void setSleep(bool on) { sim::wifi_sleep = on; }
  void setHostname(const char*) {}
  void onEvent(std::function<void(WiFiEvent_t, WiFiEventInfo_t)> f, int) { sim::wifi_on_disconnect = f; }
  void begin(const char* s, const char* p) {                       // "wifiblock 1": the network won't take it
    sim::wifi_ssid = s; sim::wifi_pass = p; sim::wifi_begins++;
    if (!sim::wifi_blocked) sim::wifi_up = true;
  }
  void disconnect(bool = false) { sim::wifi_up = false; }
  int status() { return sim::wifi_up ? WL_CONNECTED : 0; }
  int RSSI() { return -58; }
  IPAddress localIP() { return IPAddress(); }
};
inline WiFiClass WiFi;
