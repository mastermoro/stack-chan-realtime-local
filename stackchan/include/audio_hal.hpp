#pragma once

#include <Arduino.h>

#include "protocol.hpp"

namespace stackchan {
class AudioHal {
 public:
  virtual ~AudioHal() = default;
  virtual bool begin() = 0;
  virtual size_t capture(int16_t* dst, size_t samples) = 0;
  virtual void play(const int16_t* samples, size_t sample_count) = 0;
  virtual void loop() {}
  virtual void stop_playback() = 0;
  virtual void set_capture_enabled(bool enabled) = 0;
};
}  // namespace stackchan
