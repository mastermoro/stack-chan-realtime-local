#include "usb_transport.hpp"

#include <algorithm>

namespace stackchan {
namespace {
constexpr uint8_t kUsbProtocolVersion = 1;
constexpr size_t kHeaderSize = 8;
constexpr size_t kCrcSize = 4;
constexpr size_t kMaxPayload = 8192;
constexpr size_t kMaxRawFrame = kHeaderSize + kMaxPayload + kCrcSize;
constexpr size_t kMaxEncodedFrame = kMaxRawFrame + kMaxRawFrame / 254 + 3;
constexpr uint32_t kHostPresenceMs = 1500;
// Audio can arrive continuously, so draining Serial.available() to zero may
// never return to the touch handlers. Bound each pass, including short packet
// callbacks, and leave incomplete COBS frames buffered for the next pass.
constexpr size_t kReceiveByteBudget = 2048;
constexpr size_t kReceivePacketBudget = 4;
constexpr uint32_t kReceiveTimeBudgetUs = 2000;

uint32_t crc32(const uint8_t* data, size_t length) {
  uint32_t crc = 0xffffffffU;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1U) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
  }
  return crc ^ 0xffffffffU;
}

void append_u16(std::vector<uint8_t>& output, uint16_t value) {
  output.push_back(value & 0xffU);
  output.push_back((value >> 8U) & 0xffU);
}

void append_u32(std::vector<uint8_t>& output, uint32_t value) {
  for (uint8_t shift = 0; shift < 32; shift += 8) output.push_back((value >> shift) & 0xffU);
}

uint16_t read_u16(const uint8_t* data) {
  return static_cast<uint16_t>(data[0] | (static_cast<uint16_t>(data[1]) << 8U));
}

uint32_t read_u32(const uint8_t* data) {
  return static_cast<uint32_t>(data[0]) | (static_cast<uint32_t>(data[1]) << 8U) |
         (static_cast<uint32_t>(data[2]) << 16U) | (static_cast<uint32_t>(data[3]) << 24U);
}

void cobs_encode(const std::vector<uint8_t>& input, std::vector<uint8_t>& output) {
  output.clear();
  output.reserve(input.size() + input.size() / 254 + 3);
  // Write both delimiters and the encoded body in one Serial.write call. The
  // HWCDC transmit mutex then prevents framework logs from entering a frame.
  output.push_back(0);
  size_t code_index = 1;
  uint8_t code = 1;
  output.push_back(0);
  for (const uint8_t byte : input) {
    if (byte == 0) {
      output[code_index] = code;
      code_index = output.size();
      output.push_back(0);
      code = 1;
    } else {
      output.push_back(byte);
      ++code;
      if (code == 0xff) {
        output[code_index] = code;
        code_index = output.size();
        output.push_back(0);
        code = 1;
      }
    }
  }
  output[code_index] = code;
  output.push_back(0);
}

bool cobs_decode(const std::vector<uint8_t>& input, std::vector<uint8_t>& output) {
  output.clear();
  output.reserve(input.size());
  size_t index = 0;
  while (index < input.size()) {
    const uint8_t code = input[index++];
    if (code == 0 || index + code - 1 > input.size()) return false;
    for (uint8_t offset = 1; offset < code; ++offset) output.push_back(input[index++]);
    if (code != 0xff && index < input.size()) output.push_back(0);
  }
  return true;
}
}  // namespace

void UsbTransport::begin() {
  encoded_input_.reserve(kMaxEncodedFrame);
  decoded_input_.reserve(kMaxRawFrame);
  transmit_raw_.reserve(kMaxRawFrame);
  transmit_encoded_.reserve(kMaxEncodedFrame);
}

void UsbTransport::loop() {
  const uint32_t started_us = micros();
  size_t bytes_read = 0;
  size_t packets_read = 0;
  while (bytes_read < kReceiveByteBudget && packets_read < kReceivePacketBudget &&
         static_cast<uint32_t>(micros() - started_us) < kReceiveTimeBudgetUs &&
         Serial.available() > 0) {
    const int value = Serial.read();
    if (value < 0) break;
    ++bytes_read;
    if (value == 0) {
      if (!encoded_input_.empty()) {
        consume_packet();
        ++packets_read;
      }
      encoded_input_.clear();
    } else if (encoded_input_.size() < kMaxEncodedFrame) {
      encoded_input_.push_back(static_cast<uint8_t>(value));
    } else {
      encoded_input_.clear();
      ++invalid_frames_;
    }
  }
}

bool UsbTransport::host_available() const {
  return last_host_probe_ms_ != 0 &&
         static_cast<uint32_t>(millis() - last_host_probe_ms_) <= kHostPresenceMs;
}

bool UsbTransport::send(UsbFrameType type, const uint8_t* payload, size_t length) {
  if (length > kMaxPayload || (length > 0 && payload == nullptr)) return false;
  transmit_raw_.clear();
  transmit_raw_.push_back(kUsbProtocolVersion);
  transmit_raw_.push_back(static_cast<uint8_t>(type));
  append_u16(transmit_raw_, transmit_sequence_++);
  append_u32(transmit_raw_, static_cast<uint32_t>(length));
  if (length > 0) transmit_raw_.insert(transmit_raw_.end(), payload, payload + length);
  append_u32(transmit_raw_, crc32(transmit_raw_.data(), transmit_raw_.size()));
  cobs_encode(transmit_raw_, transmit_encoded_);
  return write_all(transmit_encoded_.data(), transmit_encoded_.size());
}

void UsbTransport::consume_packet() {
  if (!cobs_decode(encoded_input_, decoded_input_) ||
      decoded_input_.size() < kHeaderSize + kCrcSize ||
      decoded_input_[0] != kUsbProtocolVersion) {
    ++invalid_frames_;
    return;
  }
  const uint32_t payload_length = read_u32(decoded_input_.data() + 4);
  if (payload_length > kMaxPayload ||
      decoded_input_.size() != kHeaderSize + payload_length + kCrcSize) {
    ++invalid_frames_;
    return;
  }
  const uint32_t expected_crc = read_u32(decoded_input_.data() + kHeaderSize + payload_length);
  if (crc32(decoded_input_.data(), kHeaderSize + payload_length) != expected_crc) {
    ++invalid_frames_;
    return;
  }
  (void)read_u16(decoded_input_.data() + 2);  // Reserved for sequence-gap metrics.
  const auto type = static_cast<UsbFrameType>(decoded_input_[1]);
  if (type == UsbFrameType::HostProbe) last_host_probe_ms_ = millis();
  if (frame_handler_) {
    frame_handler_(type, decoded_input_.data() + kHeaderSize, payload_length);
  }
}

bool UsbTransport::write_all(const uint8_t* data, size_t length) {
  size_t written = 0;
  const uint32_t deadline = millis() + 1000;
  while (written < length && static_cast<int32_t>(deadline - millis()) > 0) {
    written += Serial.write(data + written, length - written);
    if (written < length) delay(1);
  }
  return written == length;
}
}  // namespace stackchan
