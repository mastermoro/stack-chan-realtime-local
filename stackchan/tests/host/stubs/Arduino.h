#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <string>
#include <vector>

using String = std::string;
inline uint64_t test_clock_us = 1000;
inline uint32_t millis() { return static_cast<uint32_t>(test_clock_us / 1000); }
inline uint32_t micros() { return static_cast<uint32_t>(test_clock_us); }
inline void delay(uint32_t duration) { test_clock_us += duration * 1000ULL; }

struct TestSerial {
  std::deque<uint8_t> input;
  std::vector<uint8_t> output;
  uint32_t read_duration_us = 0;
  size_t reads = 0;
  bool fail_write = false;
  // Refill after a read to model a producer that keeps Serial.available() high.
  std::function<void()> after_read;

  int available() const { return static_cast<int>(input.size()); }
  int read() {
    if (input.empty()) return -1;
    const int value = input.front();
    input.pop_front();
    ++reads;
    test_clock_us += read_duration_us;
    if (after_read) after_read();
    return value;
  }
  size_t write(const uint8_t* data, size_t length) {
    if (fail_write) return 0;
    output.insert(output.end(), data, data + length);
    return length;
  }
  void println(const char*) {}
  template <typename... Args> void printf(const char*, Args...) {}
  void reset() { *this = TestSerial{}; test_clock_us = 1000; }
};
inline TestSerial Serial;
