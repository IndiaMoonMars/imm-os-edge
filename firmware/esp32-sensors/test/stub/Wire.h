// Stand-in for the ESP32 Wire (I2C) API: transactions go to simulated devices (test/sim.cpp).
#pragma once
#include <cstdint>
#include <cstddef>
#include <map>
#include <vector>

struct I2CDevice {
  virtual ~I2CDevice() {}
  virtual void write(const std::vector<uint8_t>& bytes) = 0;   // one write transaction
  virtual std::vector<uint8_t> read(size_t n) = 0;             // one read transaction
};

namespace sim { extern std::map<uint8_t, I2CDevice*> bus; }

class TwoWire {
  uint8_t addr_ = 0;
  std::vector<uint8_t> out_, in_;
  size_t pos_ = 0;
 public:
  bool begin(int, int) { return true; }
  void setClock(uint32_t) {}
  void setTimeOut(uint16_t) {}
  void beginTransmission(uint8_t a) { addr_ = a; out_.clear(); }
  size_t write(uint8_t b) { out_.push_back(b); return 1; }
  size_t write(const uint8_t* d, size_t n) { out_.insert(out_.end(), d, d + n); return n; }
  uint8_t endTransmission(bool = true) {
    auto it = sim::bus.find(addr_);
    if (it == sim::bus.end()) return 2;                        // address NACK
    if (!out_.empty()) it->second->write(out_);
    return 0;
  }
  uint8_t requestFrom(uint8_t a, uint8_t n) {
    in_.clear(); pos_ = 0;
    auto it = sim::bus.find(a);
    if (it == sim::bus.end()) return 0;
    in_ = it->second->read(n);
    return (uint8_t)in_.size();
  }
  int available() { return (int)(in_.size() - pos_); }
  int read() { return pos_ < in_.size() ? in_[pos_++] : -1; }
};
extern TwoWire Wire;
