// Stand-in for the ESP32 WebServer: routes are called by test/sim.cpp ("http /json").
#pragma once
#include <functional>
#include <map>
#include <cstdio>
#include <cstdlib>
#include "Arduino.h"
namespace sim { extern bool wifi_stack; extern int web_begins; }
class WebServer {
 public:
  std::map<std::string, std::function<void()>> routes;
  std::string body, type;
  int code = 0;
  explicit WebServer(int) {}
  void on(const char* path, std::function<void()> f) { routes[path] = f; }
  void begin() {                      // the real ESP32 aborts: lwIP isn't up until WiFi.mode()
    if (!sim::wifi_stack) { std::printf("PANIC: web server started before Wi-Fi (network stack down)\n"); std::exit(3); }
    sim::web_begins++;
  }
  void stop() {}
  void handleClient() {}
  void sendHeader(const char*, const char*) {}
  void send(int c, const char* t, const char* b) { code = c; type = t; body = b; }
};
