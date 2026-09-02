#pragma once

#include <array>

#include <M5Unified.h>

#include "audio_hal.hpp"

namespace stackchan {
class M5AudioHal final : public AudioHal {
 public:
  bool begin() override;
  size_t capture(int16_t* dst, size_t samples) override;
  void play(const int16_t* samples, size_t sample_count) override;
  void loop() override;
  void stop_playback() override;
  void set_capture_enabled(bool enabled) override;
  void set_volume(uint8_t volume);
  uint8_t volume() const { return volume_; }

 private:
  static constexpr size_t kPlaybackBufferSamples = 4096;
  std::array<std::array<int16_t, kPlaybackBufferSamples>, 3> playback_buffers_{};
  size_t playback_buffer_index_ = 0;
  uint8_t volume_ = 128;
  bool capture_enabled_ = true;
  uint32_t audio_start_after_ms_ = 0;
};
}  // namespace stackchan
