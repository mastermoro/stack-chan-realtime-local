#include "m5_audio_hal.hpp"

#include <algorithm>
#include <cstring>

namespace stackchan {
namespace {
constexpr uint32_t kAudioEndpointSwitchDelayMs = 20;
constexpr uint32_t kAudioEndpointRetryDelayMs = 100;
}

bool M5AudioHal::begin() {
  M5.Speaker.setVolume(volume_);
  M5.Speaker.end();
  M5.Mic.begin();
  capture_enabled_ = true;
  return M5.Mic.isEnabled();
}

size_t M5AudioHal::capture(int16_t* dst, size_t samples) {
  if (!capture_enabled_ || !M5.Mic.isEnabled()) return 0;
  if (!M5.Mic.record(dst, samples, kSampleRate)) return 0;
  return samples;
}

void M5AudioHal::play(const int16_t* samples, size_t sample_count) {
  if (!M5.Speaker.isEnabled() || sample_count == 0) return;

  // M5Unified plays raw audio asynchronously and does not own the input
  // buffer. Retain three rotating buffers as recommended by M5Unified.
  size_t offset = 0;
  while (offset < sample_count) {
    const size_t chunk_samples = std::min(kPlaybackBufferSamples, sample_count - offset);
    auto& buffer = playback_buffers_[playback_buffer_index_];
    std::memcpy(buffer.data(), samples + offset, chunk_samples * sizeof(int16_t));
    M5.Speaker.playRaw(buffer.data(), chunk_samples, kSampleRate, false, 1, 0);
    playback_buffer_index_ = (playback_buffer_index_ + 1) % playback_buffers_.size();
    offset += chunk_samples;
  }
}

void M5AudioHal::loop() {
  const uint32_t now = millis();
  if (capture_enabled_) {
    if (M5.Mic.isEnabled() || M5.Speaker.isPlaying()) return;
    if (M5.Speaker.isEnabled()) {
      M5.Speaker.end();
      audio_start_after_ms_ = now + kAudioEndpointSwitchDelayMs;
      return;
    }
    if (static_cast<int32_t>(now - audio_start_after_ms_) < 0) return;
    M5.Mic.begin();
    audio_start_after_ms_ = now + kAudioEndpointRetryDelayMs;
  } else {
    if (M5.Speaker.isEnabled() || M5.Mic.isRecording()) return;
    if (M5.Mic.isEnabled()) {
      M5.Mic.end();
      audio_start_after_ms_ = now + kAudioEndpointSwitchDelayMs;
      return;
    }
    if (static_cast<int32_t>(now - audio_start_after_ms_) < 0) return;
    M5.Speaker.begin();
    audio_start_after_ms_ = now + kAudioEndpointRetryDelayMs;
  }
}

void M5AudioHal::stop_playback() { M5.Speaker.stop(); }

void M5AudioHal::set_volume(uint8_t volume) {
  volume_ = volume;
  M5.Speaker.setVolume(volume_);
}

void M5AudioHal::set_capture_enabled(bool enabled) {
  if (enabled == capture_enabled_) return;
  capture_enabled_ = enabled;
  loop();
}
}  // namespace stackchan
