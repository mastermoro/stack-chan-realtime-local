#pragma once

#include <Arduino.h>

namespace stackchan {
constexpr uint32_t kProtocolVersion = 1;
constexpr uint32_t kSampleRate = 24000;
constexpr uint8_t kChannels = 1;
constexpr size_t kFrameSamples = 480;  // 20 ms @ 24 kHz
constexpr size_t kFrameBytes = kFrameSamples * sizeof(int16_t);

enum class AgentState {
  Disconnected,
  Connecting,
  Ready,
  Listening,
  Thinking,
  Searching,
  Speaking,
  Error,
};
}  // namespace stackchan
