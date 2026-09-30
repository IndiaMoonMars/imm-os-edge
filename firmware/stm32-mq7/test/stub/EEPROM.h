#pragma once
#include <cstring>
namespace sim { extern unsigned char eeprom[64]; }
struct EEPROMClass {
  template <class T> void get(int addr, T& v) { std::memcpy(&v, sim::eeprom + addr, sizeof v); }
  template <class T> void put(int addr, const T& v) { std::memcpy(sim::eeprom + addr, &v, sizeof v); }
};
static EEPROMClass EEPROM;
