// Stand-in for the ESP32 WebServer: routes are called by test/sim.cpp ("http /json").
#pragma once
#include <functional>
#include <map>
#include "Arduino.h"
class WebServer {
 public:
  std::map<std::string, std::function<void()>> routes;
  std::string body, type;
  int code = 0;
  explicit WebServer(int) {}
  void on(const char* path, std::function<void()> f) { routes[path] = f; }
  void begin() {}
  void handleClient() {}
  void sendHeader(const char*, const char*) {}
  void send(int c, const char* t, const char* b) { code = c; type = t; body = b; }
};
