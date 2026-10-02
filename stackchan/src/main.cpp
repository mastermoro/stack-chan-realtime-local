#include <Arduino.h>
#include <WiFi.h>
#include <M5StackChan.h>
#include <Preferences.h>
#include <esp_now.h>
#include <esp_camera.h>
#include <esp_random.h>

#include <list>
#include <vector>
#include <human_face_detect_msr01.hpp>
#include <human_face_detect_mnp01.hpp>

#include "m5_audio_hal.hpp"
#include "protocol.hpp"
#include "relay_client.hpp"
#include "secrets.hpp"

using namespace stackchan;

namespace {
M5AudioHal audio;
RelayClient relay;
// Compose every frame off-screen, then transfer it in one operation.  Keeping
// this in PSRAM avoids consuming the memory needed for audio buffers.
M5Canvas ui_sprite(&M5.Display);
AgentState state = AgentState::Disconnected;
int16_t capture_buffer[kFrameSamples];
bool conversation_active = false;
bool resume_after_response = false;
bool relay_started = false;
enum class ConnectionMode : uint8_t { Auto, Wifi, Usb };
enum class UiPage : uint8_t { Status, Face, Network, Settings };
enum class IdleMode : uint8_t { Active, LookingAround, Sleeping };
UiPage ui_page = UiPage::Status;
IdleMode idle_mode = IdleMode::Active;
bool face_overlay_visible = false;
uint32_t face_overlay_until = 0;
bool camera_wipe_visible = false;
bool camera_ready = false;
bool camera_wipe_cache_valid = false;
uint32_t last_camera_frame = 0;
uint32_t last_tracking_log = 0;
uint32_t last_face_detected = 0;
uint32_t last_search_move = 0;
uint32_t last_face_measurement = 0;
uint8_t search_step = 0;
uint8_t face_detection_streak = 0;
bool face_tracking_locked = false;
float tracked_x = 0.0F;
float tracked_y = 0.0F;
float measured_x = 0.0F;
float measured_y = 0.0F;
float face_velocity_x = 0.0F;
float face_velocity_y = 0.0F;
uint32_t last_activity_ms = 0;
uint32_t idle_started_ms = 0;
uint32_t next_idle_move_ms = 0;
uint32_t last_idle_render_ms = 0;
uint32_t idle_motion_settle_until_ms = 0;
int idle_gaze_x = 0;
int idle_gaze_y = 0;
uint8_t voice_activity_frames = 0;
uint32_t last_top_tap_ms = 0;
uint32_t next_volume_repeat_ms = 0;
bool volume_hold_adjusted = false;
uint16_t camera_wipe_cache[128 * 96];
String emotion = "neutral";
String detail = "Connecting to Wi-Fi";
uint32_t last_wifi_attempt = 0;
wl_status_t last_wifi_status = WL_IDLE_STATUS;
uint8_t wifi_attempt_count = 0;
bool wifi_failed = false;
bool espnow_ready = false;
ConnectionMode connection_mode = ConnectionMode::Auto;
RelayTransport selected_transport = RelayTransport::None;
Preferences preferences;
uint32_t connection_mode_started_ms = 0;
bool pending_usb_switch = false;
IPAddress relay_address;
uint16_t relay_port = RELAY_PORT;
bool relay_endpoint_editing = false;
uint8_t relay_address_draft[4] = {};
uint16_t relay_port_draft = RELAY_PORT;
uint8_t relay_endpoint_field = 0;
volatile uint8_t espnow_receiver_id = 1;
struct RemoteMotionCommand {
  int16_t yaw = 0;
  int16_t pitch = 450;
  int16_t speed = 600;
  bool pending = false;
};
RemoteMotionCommand remote_motion;
portMUX_TYPE remote_motion_lock = portMUX_INITIALIZER_UNLOCKED;

constexpr uint32_t kWifiReconnectIntervalMs = 10000;
constexpr uint32_t kAutoUsbWaitMs = 3000;
constexpr uint8_t kWifiMaxAttempts = 3;
constexpr int kTabHeight = 28;
constexpr int kCameraWipeWidth = 128;
constexpr int kCameraWipeHeight = 96;
constexpr int kCameraWipeTapWidth = 64;
constexpr int kCameraWipeTapHeight = 64;
constexpr uint32_t kCameraFrameIntervalMs = 125;
constexpr uint32_t kCameraSpeakingFrameIntervalMs = 500;
constexpr uint32_t kFaceLostBeforeSearchMs = 8000;
constexpr uint32_t kSearchStepIntervalMs = 1200;
constexpr uint32_t kIdleAfterSilenceMs = 30'000;
constexpr uint32_t kSleepAfterIdleMs = 5 * 60'000;
constexpr uint32_t kIdleMoveMinIntervalMs = 2000;
constexpr uint32_t kIdleMoveMaxIntervalMs = 15'000;
constexpr uint32_t kIdleMotionSettleMs = 1200;
constexpr uint32_t kTopDoubleTapMs = 2000;
constexpr uint32_t kVolumeHoldDelayMs = 500;
constexpr uint32_t kVolumeRepeatIntervalMs = 120;
constexpr int kIdleMoveMinSpeed = 180;
constexpr int kIdleMoveMaxSpeed = 650;
constexpr int kSleepingPitch = 50;
constexpr uint16_t kVoiceActivityMean = 350;
constexpr uint8_t kVoiceActivityConfirmFrames = 3;
constexpr uint8_t kFaceLockConfirmFrames = 3;
constexpr float kTrackingLeadSeconds = 0.18F;
// The CoreS3 camera sits below the neck pivot, so the image centre needs a
// downward mechanical offset to make the optical axis level with a person.
constexpr float kCameraPitchDownBias = -0.35F;
constexpr float kTrackingHorizontalStep = 0.08F;
constexpr float kTrackingVerticalStep = 0.06F;
// The installed camera faces opposite the Motion yaw axis: moving the neck
// right shifts a stationary face left in the image, so horizontal tracking
// must use the inverse image coordinate.
constexpr float kCameraYawSign = -1.0F;
// Espressif's official ESP-NOW component places its 20-byte transport header
// before the 8-byte StackChan RemoteControl payload.
constexpr size_t kEspNowTransportHeaderBytes = 20;
constexpr size_t kRemotePayloadBytes = 8;

void handle_state(AgentState next);
void render_ui(bool should_present = true);
void report_target_network_visibility();
void draw_cached_camera_wipe();
void note_activity(bool return_home = true);
void update_idle_behavior();
void handle_top_touch();
void manage_connection();
void set_connection_mode(ConnectionMode mode);
void maintain_wifi();
void start_relay_when_wifi_ready();

// CoreS3's built-in GC0308 uses a dedicated parallel camera bus.  The camera
// owns the internal I2C pins while it is active, so M5.In_I2C is released just
// before esp_camera_init() (as required by the CoreS3 camera driver).
static camera_config_t camera_config = {
    .pin_pwdn = -1,
    .pin_reset = -1,
    .pin_xclk = -1,
    .pin_sscb_sda = 12,
    .pin_sscb_scl = 11,
    .pin_d7 = 47,
    .pin_d6 = 48,
    .pin_d5 = 16,
    .pin_d4 = 15,
    .pin_d3 = 42,
    .pin_d2 = 41,
    .pin_d1 = 40,
    .pin_d0 = 39,
    .pin_vsync = 46,
    .pin_href = 38,
    .pin_pclk = 45,
    .xclk_freq_hz = 20000000,
    .ledc_timer = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,
    .pixel_format = PIXFORMAT_RGB565,
    .frame_size = FRAMESIZE_QVGA,
    .jpeg_quality = 0,
    .fb_count = 2,
    .fb_location = CAMERA_FB_IN_PSRAM,
    .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
    .sccb_i2c_port = -1,
};

// Use the ESP-DL two-stage detector. The first stage finds candidate regions;
// the second stage rejects false positives and supplies a stable face box for
// the servo tracker.
HumanFaceDetectMSR01 face_detector_stage1(0.1F, 0.5F, 10, 0.2F);
HumanFaceDetectMNP01 face_detector_stage2(0.5F, 0.3F, 5);

void present_ui() { ui_sprite.pushSprite(0, 0); }

bool start_camera() {
  if (camera_ready) return true;
  M5.In_I2C.release();
  const esp_err_t result = esp_camera_init(&camera_config);
  if (result != ESP_OK) {
    Serial.printf("Camera init failed: %d\n", result);
    detail = "Camera unavailable";
    return false;
  }
  camera_ready = true;
  Serial.println("Camera ready; face tracking enabled");
  return true;
}

void draw_cached_camera_wipe() {
  if (!camera_wipe_cache_valid) return;
  ui_sprite.pushImage(0, 0, kCameraWipeWidth, kCameraWipeHeight, camera_wipe_cache);
  ui_sprite.drawRect(0, 0, kCameraWipeWidth, kCameraWipeHeight, TFT_CYAN);
  ui_sprite.fillRect(0, 0, kCameraWipeWidth, 12, TFT_BLACK);
  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_CYAN, TFT_BLACK);
  ui_sprite.setCursor(4, 2);
  ui_sprite.print(face_tracking_locked ? "CAM / LOCK" : "CAM / SEARCH");
}

void cache_camera_wipe(const camera_fb_t* frame) {
  const auto* source = reinterpret_cast<const uint16_t*>(frame->buf);
  for (int y = 0; y < kCameraWipeHeight; ++y) {
    const int source_y = y * frame->height / kCameraWipeHeight;
    for (int x = 0; x < kCameraWipeWidth; ++x) {
      const int source_x = x * frame->width / kCameraWipeWidth;
      camera_wipe_cache[y * kCameraWipeWidth + x] = source[source_y * frame->width + source_x];
    }
  }
  camera_wipe_cache_valid = true;
}

void update_camera_wipe() {
  const uint32_t frame_interval = state == AgentState::Speaking
                                      ? kCameraSpeakingFrameIntervalMs
                                      : kCameraFrameIntervalMs;
  if (!camera_wipe_visible || !camera_ready ||
      millis() - last_camera_frame < frame_interval) return;
  last_camera_frame = millis();
  camera_fb_t* frame = esp_camera_fb_get();
  if (!frame) {
    Serial.println("Camera frame capture failed");
    return;
  }
  cache_camera_wipe(frame);

  auto& candidates = face_detector_stage1.infer(
      reinterpret_cast<uint16_t*>(frame->buf),
      {static_cast<int>(frame->height), static_cast<int>(frame->width), 3});
  auto& faces = face_detector_stage2.infer(
      reinterpret_cast<uint16_t*>(frame->buf),
      {static_cast<int>(frame->height), static_cast<int>(frame->width), 3}, candidates);
  if (faces.empty()) {
    face_detection_streak = 0;
  } else if (face_detection_streak < kFaceLockConfirmFrames) {
    ++face_detection_streak;
  }
  const bool was_tracking_locked = face_tracking_locked;
  const bool face_confirmed = face_detection_streak >= kFaceLockConfirmFrames;
  if (face_confirmed) {
    face_tracking_locked = true;
    last_face_detected = millis();
    search_step = 0;
    // Cancel any in-progress search spring as soon as a face reappears. This
    // prevents a transient LOCK from continuing toward an old search pose.
    if (!was_tracking_locked) {
      M5StackChan.Motion.stop();
      // Continue from the actual search pose instead of snapping from a stale
      // software target when the face is reacquired.
      tracked_x = constrain(M5StackChan.Motion.getCurrentYawAngle() / 1280.0F, -1.0F, 1.0F);
      tracked_y = constrain((2.0F * M5StackChan.Motion.getCurrentPitchAngle() / 900.0F) - 1.0F,
                            -1.0F, 1.0F);
    }
    const auto& face = faces.front();
    const int center_x = (face.box[0] + face.box[2]) / 2;
    const int center_y = (face.box[1] + face.box[3]) / 2;
    // The camera's horizontal image axis is opposite the installed neck yaw
    // axis. Invert it so a face at the image's left is driven toward centre.
    const float raw_x = constrain(kCameraYawSign * ((2.0F * center_x / frame->width) - 1.0F),
                                  -1.0F, 1.0F);
    const float raw_y = constrain(1.0F - (2.0F * center_y / frame->height), -1.0F, 1.0F);
    // Face boxes describe a frame captured before inference completed, while
    // the camera moves with the neck. Estimate short-term face motion and lead
    // the command by the measured camera/detector/servo delay.
    const uint32_t now = millis();
    if (last_face_measurement != 0) {
      const float seconds = (now - last_face_measurement) / 1000.0F;
      if (seconds > 0.02F && seconds < 0.8F) {
        const float instant_x = constrain((raw_x - measured_x) / seconds, -2.0F, 2.0F);
        const float instant_y = constrain((raw_y - measured_y) / seconds, -2.0F, 2.0F);
        face_velocity_x = face_velocity_x * 0.75F + instant_x * 0.25F;
        face_velocity_y = face_velocity_y * 0.75F + instant_y * 0.25F;
      }
    }
    last_face_measurement = now;
    measured_x = raw_x;
    measured_y = raw_y;
    // This is an *incremental* controller: image-centre means zero correction,
    // not "return the head to home". It prevents the old centre-to-home loop
    // that immediately pushed a centered face back out of frame.
    float correction_x = 0.0F;
    float correction_y = 0.0F;
    if (abs(raw_x) >= 0.10F) {
      correction_x = constrain(raw_x + face_velocity_x * kTrackingLeadSeconds, -1.0F, 1.0F);
    } else {
      face_velocity_x *= 0.5F;
    }
    if (abs(raw_y) >= 0.08F) {
      correction_y = constrain(raw_y + face_velocity_y * kTrackingLeadSeconds, -1.0F, 1.0F);
    } else {
      face_velocity_y *= 0.5F;
    }
    if (correction_x != 0.0F || correction_y != 0.0F) {
      tracked_x = constrain(tracked_x + correction_x * kTrackingHorizontalStep, -1.0F, 1.0F);
      tracked_y = constrain(tracked_y + correction_y * kTrackingVerticalStep, -1.0F, 1.0F);
      M5StackChan.Motion.lookAtNormalized(tracked_x, tracked_y, 280);
    }
    if (millis() - last_tracking_log >= 1000) {
      last_tracking_log = millis();
      Serial.printf("TRACK lock box=%d,%d-%d,%d target=%.2f,%.2f lead=%.2f,%.2f\n",
                    face.box[0], face.box[1], face.box[2], face.box[3], tracked_x, tracked_y,
                    face_velocity_x, face_velocity_y);
    }
  } else {
    // Do not abandon a confirmed target on one or two missed frames. The head
    // holds its last command for eight seconds so detector jitter cannot turn
    // a fresh LOCK straight back into a wide search movement.
    if (was_tracking_locked && millis() - last_face_detected < kFaceLostBeforeSearchMs) {
      // Keep the last tracking target; no new servo command is issued here.
    } else if (millis() - last_face_detected >= kFaceLostBeforeSearchMs &&
               millis() - last_search_move >= kSearchStepIntervalMs) {
      face_tracking_locked = false;
      // Five closely spaced horizontal points on each of four vertical rows.
      // Alternating the direction on each row produces a continuous serpentine
      // sweep instead of repeatedly making a large left-to-right jump.
      static constexpr float kSearchX[] = {-0.80F, -0.40F, 0.0F, 0.40F, 0.80F};
      static constexpr float kSearchY[] = {-0.05F, -0.35F, -0.65F, -0.90F};
      constexpr uint8_t kColumns = sizeof(kSearchX) / sizeof(kSearchX[0]);
      constexpr uint8_t kRows = sizeof(kSearchY) / sizeof(kSearchY[0]);
      const uint8_t index = search_step++ % (kColumns * kRows);
      const uint8_t row = index / kColumns;
      const uint8_t column = index % kColumns;
      const uint8_t swept_column = (row & 1) ? (kColumns - 1 - column) : column;
      tracked_x = kSearchX[swept_column];
      tracked_y = kSearchY[row];
      M5StackChan.Motion.lookAtNormalized(tracked_x, tracked_y, 90);
      last_search_move = millis();
      Serial.printf("TRACK search target=%.2f,%.2f\n", tracked_x, tracked_y);
    } else if (millis() - last_tracking_log >= 1000) {
      last_tracking_log = millis();
      Serial.printf("TRACK holding=%u candidates=%u\n", was_tracking_locked,
                    static_cast<unsigned>(candidates.size()));
    }
  }

  // Compose the face and the camera into the same sprite transfer.  Presenting
  // between these two operations caused the Face screen to briefly overwrite
  // the camera area once per camera frame.
  render_ui(false);
  present_ui();
  esp_camera_fb_return(frame);
}

void on_espnow_receive(const uint8_t*, const uint8_t* data, int length) {
  if (length < static_cast<int>(kEspNowTransportHeaderBytes + kRemotePayloadBytes)) return;
  const uint8_t* payload = data + kEspNowTransportHeaderBytes;
  const uint8_t target_id = payload[0];
  if (target_id != 0 && target_id != espnow_receiver_id) return;

  const int16_t yaw = static_cast<int16_t>(payload[1] | (payload[2] << 8));
  const int16_t pitch = static_cast<int16_t>(payload[3] | (payload[4] << 8));
  const int16_t speed = static_cast<int16_t>(payload[5] | (payload[6] << 8));
  portENTER_CRITICAL_ISR(&remote_motion_lock);
  remote_motion.yaw = constrain(yaw, -1280, 1280);
  remote_motion.pitch = constrain(pitch, 50, 850);
  remote_motion.speed = constrain(speed, 0, 1000);
  remote_motion.pending = true;
  portEXIT_CRITICAL_ISR(&remote_motion_lock);
}

void start_espnow_remote() {
  if (espnow_ready) return;
  const esp_err_t init_result = esp_now_init();
  if (init_result != ESP_OK) {
    Serial.printf("ESP-NOW init failed: %d\n", init_result);
    return;
  }
  const esp_err_t callback_result = esp_now_register_recv_cb(on_espnow_receive);
  if (callback_result != ESP_OK) {
    Serial.printf("ESP-NOW callback failed: %d\n", callback_result);
    esp_now_deinit();
    return;
  }
  espnow_ready = true;
  Serial.printf("ESP-NOW remote ready: channel %u, receiver ID %u\n", WiFi.channel(),
                espnow_receiver_id);
}

void apply_espnow_remote() {
  RemoteMotionCommand command;
  portENTER_CRITICAL(&remote_motion_lock);
  if (!remote_motion.pending) {
    portEXIT_CRITICAL(&remote_motion_lock);
    return;
  }
  command = remote_motion;
  remote_motion.pending = false;
  portEXIT_CRITICAL(&remote_motion_lock);

  if (ui_page != UiPage::Face) return;
  M5StackChan.Motion.move(command.yaw, command.pitch, command.speed);
}

const char* wifi_status_label(wl_status_t value) {
  switch (value) {
    case WL_IDLE_STATUS: return "idle";
    case WL_NO_SSID_AVAIL: return "SSID not found";
    case WL_SCAN_COMPLETED: return "scan complete";
    case WL_CONNECTED: return "connected";
    case WL_CONNECT_FAILED: return "auth failed";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED: return "disconnected";
    default: return "unknown";
  }
}

void report_wifi_status(bool force = false) {
  const wl_status_t current = WiFi.status();
  if (!force && current == last_wifi_status) return;
  last_wifi_status = current;

  if (current == WL_CONNECTED) {
    detail = "Wi-Fi OK: " + WiFi.localIP().toString();
  } else {
    detail = String("Wi-Fi: ") + wifi_status_label(current);
  }
  Serial.printf("Wi-Fi status: %s (%d)\\n", wifi_status_label(current), current);
  render_ui();
}

const char* state_label(AgentState value) {
  switch (value) {
    case AgentState::Ready: return "READY";
    case AgentState::Listening: return "LISTENING";
    case AgentState::Thinking: return "THINKING";
    case AgentState::Searching: return "SEARCHING";
    case AgentState::Speaking: return "SPEAKING";
    case AgentState::Error: return "ERROR";
    case AgentState::Connecting: return "CONNECTING";
    case AgentState::Disconnected: return "DISCONNECTED";
  }
  return "UNKNOWN";
}

const char* action_label() {
  if (state == AgentState::Ready) return "TAP: START CHAT";
  if (state == AgentState::Listening) return "TAP: PAUSE CHAT";
  if (state == AgentState::Thinking || state == AgentState::Searching ||
      state == AgentState::Speaking) {
    return "TAP: PAUSE CHAT";
  }
  return "WAITING FOR RELAY";
}

uint32_t state_color() {
  if (state == AgentState::Speaking) return TFT_GREEN;
  if (state == AgentState::Error || state == AgentState::Disconnected) return TFT_RED;
  if (state == AgentState::Thinking || state == AgentState::Searching) return TFT_YELLOW;
  return TFT_CYAN;
}

void draw_tabs() {
  static constexpr const char* kTabs[] = {"STATUS", "FACE", "NETWORK", "SETTINGS"};
  const int tab_width = ui_sprite.width() / 4;
  ui_sprite.fillRect(0, 0, ui_sprite.width(), kTabHeight, TFT_DARKGREY);
  ui_sprite.setTextSize(1);
  for (int index = 0; index < 4; ++index) {
    const bool selected = index == static_cast<int>(ui_page);
    const int x = index * tab_width;
    ui_sprite.fillRect(x + 2, 3, tab_width - 4, kTabHeight - 6,
                        selected ? state_color() : TFT_BLACK);
    ui_sprite.setTextColor(selected ? TFT_BLACK : TFT_LIGHTGREY,
                            selected ? state_color() : TFT_BLACK);
    ui_sprite.setCursor(x + (tab_width - strlen(kTabs[index]) * 6) / 2, 11);
    ui_sprite.print(kTabs[index]);
  }
}

void draw_action_button(uint32_t color) {
  const int y = ui_sprite.height() - 48;
  ui_sprite.drawRoundRect(8, y, ui_sprite.width() - 16, 36, 8, color);
  ui_sprite.setTextSize(2);
  ui_sprite.setTextColor(color, TFT_BLACK);
  ui_sprite.setCursor(24, y + 8);
  ui_sprite.print(action_label());
}

void render_status() {
  const uint32_t color = state_color();
  ui_sprite.setTextColor(TFT_WHITE, TFT_BLACK);
  ui_sprite.setTextSize(2);
  ui_sprite.setCursor(12, 42);
  ui_sprite.print("Stack-chan Relay");
  ui_sprite.setTextColor(color, TFT_BLACK);
  ui_sprite.setCursor(12, 78);
  ui_sprite.print(state_label(state));
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setTextSize(1);
  ui_sprite.setCursor(12, 112);
  ui_sprite.print(detail);
  ui_sprite.setCursor(12, 132);
  if (WiFi.status() == WL_CONNECTED) {
    ui_sprite.printf("Wi-Fi: %s", WiFi.localIP().toString().c_str());
  } else {
    ui_sprite.printf("Wi-Fi: %s", wifi_status_label(WiFi.status()));
  }
  ui_sprite.setCursor(12, 150);
  ui_sprite.printf("Relay: %s:%u", relay_address.toString().c_str(), relay_port);
  draw_action_button(color);
}

void render_cat_eye(int center_x, int center_y, uint32_t color) {
  ui_sprite.fillRoundRect(center_x - 24, center_y - 29, 48, 58, 22, color);
  ui_sprite.fillRoundRect(center_x - 8, center_y - 17, 16, 36, 8, TFT_BLACK);
  ui_sprite.fillCircle(center_x - 9, center_y - 13, 5, TFT_WHITE);
  ui_sprite.fillCircle(center_x + 7, center_y + 10, 3, TFT_WHITE);
}

void render_listening_cat_eye(int center_x, int center_y) {
  ui_sprite.fillRoundRect(center_x - 27, center_y - 32, 54, 64, 25, TFT_PINK);
  ui_sprite.fillRoundRect(center_x - 23, center_y - 28, 46, 56, 21, TFT_WHITE);
  ui_sprite.fillCircle(center_x, center_y + 2, 13, TFT_BLACK);
  ui_sprite.fillCircle(center_x - 6, center_y - 6, 5, TFT_WHITE);
  ui_sprite.fillCircle(center_x + 5, center_y + 8, 3, TFT_WHITE);
}

void render_searching_cat_eye(int center_x, int center_y, int pupil_shift) {
  ui_sprite.fillRoundRect(center_x - 24, center_y - 27, 48, 54, 21, TFT_YELLOW);
  ui_sprite.fillRoundRect(center_x + pupil_shift - 6, center_y - 15, 12, 32, 6, TFT_BLACK);
  ui_sprite.fillCircle(center_x + pupil_shift - 6, center_y - 11, 4, TFT_WHITE);
}

void render_idle_cat_eye(int center_x, int center_y) {
  ui_sprite.fillRoundRect(center_x - 24, center_y - 29, 48, 58, 22, TFT_PINK);
  ui_sprite.fillRoundRect(center_x - 7 + idle_gaze_x, center_y - 16 + idle_gaze_y, 14, 34, 7,
                          TFT_BLACK);
  ui_sprite.fillCircle(center_x - 5 + idle_gaze_x, center_y - 11 + idle_gaze_y, 4, TFT_WHITE);
}

void render_face_mouth() {
  const uint32_t color = state_color();
  const uint32_t mouth_color = TFT_PINK;
  const int center_x = ui_sprite.width() / 2;
  ui_sprite.fillRect(center_x - 78, 134, 156, 58, TFT_BLACK);

  // Redraw the muzzle on every lip-sync frame so the animated mouth remains
  // one coherent cat expression instead of floating below the nose.
  ui_sprite.fillCircle(center_x - 17, 153, 22, TFT_WHITE);
  ui_sprite.fillCircle(center_x + 17, 153, 22, TFT_WHITE);
  ui_sprite.fillTriangle(center_x - 9, 143, center_x + 9, 143, center_x, 151, TFT_PINK);
  ui_sprite.drawLine(center_x, 151, center_x, 157, TFT_DARKGREY);

  for (int offset : {-8, 8}) {
    ui_sprite.drawLine(center_x - 30, 153 + offset, center_x - 72, 147 + offset, TFT_LIGHTGREY);
    ui_sprite.drawLine(center_x + 30, 153 + offset, center_x + 72, 147 + offset, TFT_LIGHTGREY);
  }

  if (state == AgentState::Speaking) {
    static constexpr int kMouthOpenHeights[] = {0, 7, 12, 7};
    const int mouth_height = kMouthOpenHeights[(millis() / 110) % 4];
    ui_sprite.drawArc(center_x - 10, 157, 11, 10, 0, 90, mouth_color);
    ui_sprite.drawArc(center_x + 10, 157, 11, 10, 90, 180, mouth_color);
    if (mouth_height > 0) {
      const int mouth_width = 20 + mouth_height;
      ui_sprite.fillRoundRect(center_x - mouth_width / 2, 160, mouth_width, mouth_height,
                              mouth_height / 2, TFT_BLACK);
      ui_sprite.drawRoundRect(center_x - mouth_width / 2, 160, mouth_width, mouth_height,
              mouth_height / 2, mouth_color);
      if (mouth_height == 12) {
        ui_sprite.fillRoundRect(center_x - 7, 167, 14, 4, 2, TFT_PINK);
      }
    }
  } else if (state == AgentState::Searching) {
    ui_sprite.drawLine(center_x - 13, 160, center_x, 164, mouth_color);
    ui_sprite.drawLine(center_x, 164, center_x + 13, 160, mouth_color);
  } else if (state == AgentState::Thinking) {
    const int active_dot = (millis() / 180) % 3;
    for (int dot = 0; dot < 3; ++dot) {
      ui_sprite.fillCircle(center_x - 12 + dot * 12, 171, dot == active_dot ? 4 : 2, color);
    }
  } else if (state == AgentState::Error || state == AgentState::Disconnected) {
    ui_sprite.drawArc(center_x, 176, 22, 14, 200, 340, TFT_RED);
  } else {
    if (emotion == "sad") {
      ui_sprite.drawArc(center_x, 176, 25, 16, 198, 342, TFT_SKYBLUE);
    } else if (emotion == "angry") {
      ui_sprite.fillRoundRect(center_x - 20, 163, 40, 7, 3, TFT_ORANGE);
    } else if (emotion == "surprised") {
      ui_sprite.drawCircle(center_x, 170, 11, mouth_color);
    } else if (emotion == "happy") {
      ui_sprite.drawArc(center_x - 10, 157, 11, 10, 0, 90, TFT_PINK);
      ui_sprite.drawArc(center_x + 10, 157, 11, 10, 90, 180, TFT_PINK);
    } else {
      ui_sprite.drawArc(center_x - 10, 157, 11, 10, 0, 90, mouth_color);
      ui_sprite.drawArc(center_x + 10, 157, 11, 10, 90, 180, mouth_color);
    }
  }
}

void render_search_animation_frame() {
  const int center_x = ui_sprite.width() / 2;
  const int phase = static_cast<int>((millis() / 220) % 4);
  static constexpr int kPupilShifts[] = {-7, 0, 7, 0};
  const int pupil_shift = kPupilShifts[phase];
  const int bob = phase == 1 ? 2 : 0;
  ui_sprite.fillRect(12, 48, ui_sprite.width() - 24, 142, TFT_BLACK);

  render_searching_cat_eye(center_x - 64, 96 + bob, pupil_shift);
  render_searching_cat_eye(center_x + 64, 96 + bob, pupil_shift);
  ui_sprite.drawLine(center_x - 90, 65 + bob, center_x - 44, 72 + bob, TFT_PINK);
  ui_sprite.drawLine(center_x + 90, 65 + bob, center_x + 44, 72 + bob, TFT_PINK);
  render_face_mouth();

  const int glass_x = 244 + pupil_shift / 2;
  const int glass_y = 143 + bob;
  ui_sprite.drawCircle(glass_x, glass_y, 20, TFT_PINK);
  ui_sprite.drawCircle(glass_x, glass_y, 17, TFT_WHITE);
  ui_sprite.drawLine(glass_x + 14, glass_y + 14, glass_x + 34, glass_y + 34, TFT_PINK);
  ui_sprite.drawLine(glass_x + 16, glass_y + 12, glass_x + 36, glass_y + 32, TFT_PINK);
  ui_sprite.fillCircle(226, 169 + bob, 12, TFT_WHITE);
  ui_sprite.fillCircle(220, 160 + bob, 4, TFT_PINK);
  ui_sprite.fillCircle(228, 157 + bob, 4, TFT_PINK);
  ui_sprite.fillCircle(236, 161 + bob, 4, TFT_PINK);

  ui_sprite.fillCircle(45, 77 + phase * 2, 4, TFT_SKYBLUE);
  ui_sprite.fillTriangle(41, 77 + phase * 2, 49, 77 + phase * 2, 45, 66 + phase * 2,
                         TFT_SKYBLUE);
}

void render_idle_face() {
  const int center_x = ui_sprite.width() / 2;
  render_idle_cat_eye(center_x - 64, 98);
  render_idle_cat_eye(center_x + 64, 98);
  render_face_mouth();
}

void render_sleeping_face() {
  const int center_x = ui_sprite.width() / 2;
  const int eye_y = 100;
  const int breathe = (millis() / 700) % 2;
  ui_sprite.drawArc(center_x - 64, eye_y, 24, 12, 20, 160, TFT_PINK);
  ui_sprite.drawArc(center_x + 64, eye_y, 24, 12, 20, 160, TFT_PINK);
  render_face_mouth();
  ui_sprite.fillCircle(center_x - 104, 146 + breathe, 7, TFT_PINK);
  ui_sprite.fillCircle(center_x + 104, 146 + breathe, 7, TFT_PINK);
  ui_sprite.setTextColor(TFT_SKYBLUE, TFT_BLACK);
  ui_sprite.setTextSize(1);
  ui_sprite.setCursor(245, 72 - breathe * 2);
  ui_sprite.print("z");
  ui_sprite.setTextSize(2);
  ui_sprite.setCursor(265, 52 - breathe * 3);
  ui_sprite.print("Z");
}

void render_face(bool show_overlay) {
  const uint32_t color = state_color();
  const int center_x = ui_sprite.width() / 2;
  const int eye_y = 98;
  const int eye_offset = 64;
  if (show_overlay) {
    ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    ui_sprite.setTextSize(1);
    ui_sprite.setCursor(8, 34);
    ui_sprite.print(state_label(state));
  }
  if (idle_mode == IdleMode::Sleeping) {
    render_sleeping_face();
  } else if (idle_mode == IdleMode::LookingAround) {
    render_idle_face();
  } else if (state == AgentState::Error || state == AgentState::Disconnected) {
    for (int side : {-1, 1}) {
      const int x = center_x + side * eye_offset;
      ui_sprite.drawLine(x - 16, eye_y - 16, x + 16, eye_y + 16, color);
      ui_sprite.drawLine(x + 16, eye_y - 16, x - 16, eye_y + 16, color);
    }
    render_face_mouth();
  } else if (state == AgentState::Searching) {
    render_search_animation_frame();
  } else if (state == AgentState::Thinking) {
    const int bob = (millis() / 180) % 3;
    render_cat_eye(center_x - eye_offset, eye_y + bob, color);
    render_cat_eye(center_x + eye_offset, eye_y - bob, color);
    render_face_mouth();
  } else {
    if (state == AgentState::Listening) {
      render_listening_cat_eye(center_x - eye_offset, eye_y);
      render_listening_cat_eye(center_x + eye_offset, eye_y);
      ui_sprite.fillCircle(center_x - 104, 145, 8, TFT_PINK);
      ui_sprite.fillCircle(center_x + 104, 145, 8, TFT_PINK);
    } else if (emotion == "sad") {
      ui_sprite.fillRoundRect(center_x - eye_offset - 18, eye_y - 18, 36, 34, 16, color);
      ui_sprite.fillRoundRect(center_x + eye_offset - 18, eye_y - 18, 36, 34, 16, color);
      ui_sprite.fillCircle(center_x - eye_offset + 7, eye_y - 3, 5, TFT_BLACK);
      ui_sprite.fillCircle(center_x + eye_offset + 7, eye_y - 3, 5, TFT_BLACK);
      ui_sprite.drawLine(center_x - eye_offset - 24, eye_y - 28, center_x - eye_offset + 20,
                          eye_y - 16, TFT_SKYBLUE);
      ui_sprite.drawLine(center_x + eye_offset + 24, eye_y - 28, center_x + eye_offset - 20,
                          eye_y - 16, TFT_SKYBLUE);
      ui_sprite.fillCircle(center_x - eye_offset - 18, eye_y + 28, 5, TFT_SKYBLUE);
      ui_sprite.fillCircle(center_x + eye_offset + 18, eye_y + 28, 5, TFT_SKYBLUE);
    } else if (emotion == "angry") {
      ui_sprite.fillCircle(center_x - eye_offset, eye_y, 15, color);
      ui_sprite.fillCircle(center_x + eye_offset, eye_y, 15, color);
      ui_sprite.drawLine(center_x - eye_offset - 26, eye_y - 26, center_x - eye_offset + 22,
                          eye_y - 10, TFT_ORANGE);
      ui_sprite.drawLine(center_x + eye_offset + 26, eye_y - 26, center_x + eye_offset - 22,
                          eye_y - 10, TFT_ORANGE);
    } else if (emotion == "surprised") {
      ui_sprite.fillCircle(center_x - eye_offset, eye_y, 23, color);
      ui_sprite.fillCircle(center_x + eye_offset, eye_y, 23, color);
      ui_sprite.fillCircle(center_x - eye_offset, eye_y, 10, TFT_BLACK);
      ui_sprite.fillCircle(center_x + eye_offset, eye_y, 10, TFT_BLACK);
    } else if (emotion == "sleepy") {
      ui_sprite.drawLine(center_x - eye_offset - 22, eye_y, center_x - eye_offset + 22, eye_y,
                          color);
      ui_sprite.drawLine(center_x + eye_offset - 22, eye_y, center_x + eye_offset + 22, eye_y,
                          color);
    } else {
      render_cat_eye(center_x - eye_offset, eye_y, color);
      render_cat_eye(center_x + eye_offset, eye_y, color);
      if (emotion == "happy") {
        ui_sprite.fillCircle(center_x - 104, 145, 11, TFT_PINK);
        ui_sprite.fillCircle(center_x + 104, 145, 11, TFT_PINK);
      }
    }
    render_face_mouth();
  }
  if (show_overlay) {
    ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    ui_sprite.setTextSize(1);
    ui_sprite.setCursor(8, 196);
    ui_sprite.print(detail);
    draw_action_button(color);
  }
}

const char* connection_mode_label(ConnectionMode mode) {
  switch (mode) {
    case ConnectionMode::Auto: return "AUTO";
    case ConnectionMode::Wifi: return "WI-FI";
    case ConnectionMode::Usb: return "USB";
  }
  return "?";
}

const char* active_transport_label() {
  switch (selected_transport) {
    case RelayTransport::Wifi: return "Wi-Fi";
    case RelayTransport::Usb: return "USB";
    case RelayTransport::None: return "waiting";
  }
  return "waiting";
}

void draw_connection_mode_button(ConnectionMode mode, int x, int width) {
  const uint32_t color = state_color();
  if (connection_mode == mode) {
    ui_sprite.fillRoundRect(x, 80, width, 28, 5, color);
    ui_sprite.setTextColor(TFT_BLACK, color);
  } else {
    ui_sprite.drawRoundRect(x, 80, width, 28, 5, TFT_DARKGREY);
    ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  }
  ui_sprite.setTextSize(1);
  const char* label = connection_mode_label(mode);
  ui_sprite.setCursor(x + (width - strlen(label) * 6) / 2, 90);
  ui_sprite.print(label);
}

void draw_relay_endpoint_field(int x, int y, int width, const String& value, bool selected) {
  const uint32_t color = state_color();
  ui_sprite.drawRoundRect(x, y, width, 28, 5, selected ? color : TFT_DARKGREY);
  ui_sprite.setTextColor(selected ? color : TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setTextSize(1);
  ui_sprite.setCursor(x + (width - value.length() * 6) / 2, y + 10);
  ui_sprite.print(value);
}

void render_relay_endpoint_editor() {
  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setCursor(12, 67);
  ui_sprite.print("IPv4 address");
  for (int index = 0; index < 4; ++index) {
    draw_relay_endpoint_field(12 + index * 76, 80, 64, String(relay_address_draft[index]),
                              relay_endpoint_field == index);
  }
  ui_sprite.setCursor(12, 121);
  ui_sprite.print("Port");
  draw_relay_endpoint_field(70, 112, 110, String(relay_port_draft),
                            relay_endpoint_field == 4);

  const uint32_t color = state_color();
  ui_sprite.drawRoundRect(12, 148, 92, 32, 6, color);
  ui_sprite.drawRoundRect(216, 148, 92, 32, 6, color);
  ui_sprite.setTextSize(2);
  ui_sprite.setTextColor(color, TFT_BLACK);
  ui_sprite.setCursor(53, 156);
  ui_sprite.print("-");
  ui_sprite.setCursor(257, 156);
  ui_sprite.print("+");

  ui_sprite.fillRoundRect(12, 194, 140, 34, 6, color);
  ui_sprite.drawRoundRect(168, 194, 140, 34, 6, TFT_DARKGREY);
  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_BLACK, color);
  ui_sprite.setCursor(68, 207);
  ui_sprite.print("SAVE");
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setCursor(218, 207);
  ui_sprite.print("CANCEL");
}

void render_network() {
  ui_sprite.setTextColor(TFT_WHITE, TFT_BLACK);
  ui_sprite.setTextSize(2);
  ui_sprite.setCursor(12, 40);
  ui_sprite.print("Network");
  if (relay_endpoint_editing) {
    render_relay_endpoint_editor();
    return;
  }

  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setCursor(12, 67);
  ui_sprite.print("Connection mode");
  draw_connection_mode_button(ConnectionMode::Auto, 12, 92);
  draw_connection_mode_button(ConnectionMode::Wifi, 114, 92);
  draw_connection_mode_button(ConnectionMode::Usb, 216, 92);
  ui_sprite.setCursor(12, 122);
  ui_sprite.printf("Active: %s%s", active_transport_label(),
                   pending_usb_switch ? " (USB pending)" : "");
  ui_sprite.setCursor(12, 142);
  ui_sprite.printf("Wi-Fi: %s", WiFi.status() == WL_CONNECTED
                                   ? WiFi.localIP().toString().c_str()
                                   : "off / disconnected");
  ui_sprite.setCursor(12, 166);
  ui_sprite.print("Relay endpoint");
  ui_sprite.drawRoundRect(12, 178, 296, 38, 6, state_color());
  ui_sprite.setTextColor(state_color(), TFT_BLACK);
  ui_sprite.setCursor(24, 193);
  ui_sprite.printf("%s:%u", relay_address.toString().c_str(), relay_port);
  ui_sprite.setCursor(244, 193);
  ui_sprite.print("EDIT");
}

void render_settings() {
  const uint32_t color = state_color();
  ui_sprite.setTextColor(TFT_WHITE, TFT_BLACK);
  ui_sprite.setTextSize(2);
  ui_sprite.setCursor(12, 40);
  ui_sprite.print("Settings");
  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setCursor(12, 72);
  ui_sprite.printf("Volume: %u", audio.volume());
  ui_sprite.drawRoundRect(12, 88, 52, 32, 6, color);
  ui_sprite.drawRoundRect(256, 88, 52, 32, 6, color);
  ui_sprite.fillRect(76, 101, 168, 6, TFT_DARKGREY);
  ui_sprite.fillRect(76, 101, map(audio.volume(), 0, 255, 0, 168), 6, color);
  ui_sprite.setTextSize(2);
  ui_sprite.setCursor(31, 96);
  ui_sprite.print("-");
  ui_sprite.setCursor(275, 94);
  ui_sprite.print("+");
  ui_sprite.setTextSize(1);
  ui_sprite.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  ui_sprite.setCursor(12, 154);
  ui_sprite.print("ESP-NOW remote receiver");
  ui_sprite.drawRoundRect(12, 170, 296, 40, 6, color);
  ui_sprite.setCursor(36, 185);
  ui_sprite.printf("Remote: < %u >  ESP-NOW: %s", espnow_receiver_id,
                   espnow_ready ? String(WiFi.channel()).c_str() : "off");
}

void render_ui(bool should_present) {
  ui_sprite.fillScreen(TFT_BLACK);
  if (ui_page != UiPage::Face || face_overlay_visible) draw_tabs();
  if (ui_page == UiPage::Status) render_status();
  if (ui_page == UiPage::Face) render_face(face_overlay_visible);
  if (ui_page == UiPage::Face && camera_wipe_visible) draw_cached_camera_wipe();
  if (ui_page == UiPage::Network) render_network();
  if (ui_page == UiPage::Settings) render_settings();
  if (should_present) present_ui();
}

void connect_wifi() {
  WiFi.mode(WIFI_STA);
  // Continuous PCM WebSocket traffic is latency-sensitive. ESP32 modem sleep
  // can otherwise stall the connection long enough for the peer to disconnect.
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  wifi_attempt_count = 1;
  wifi_failed = false;
  Serial.printf("Wi-Fi connecting to SSID '%s'\\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  last_wifi_attempt = millis();
  report_wifi_status(true);
}

void stop_espnow_remote() {
  if (espnow_ready) {
    esp_now_deinit();
    espnow_ready = false;
  }
  portENTER_CRITICAL(&remote_motion_lock);
  remote_motion.pending = false;
  portEXIT_CRITICAL(&remote_motion_lock);
}

void end_active_session() {
  if (relay.connected()) relay.send_control("conversation.pause");
  conversation_active = false;
  resume_after_response = false;
  audio.set_capture_enabled(false);
  audio.stop_playback();
  relay.end();
  relay_started = false;
}

void activate_usb_transport() {
  end_active_session();
  stop_espnow_remote();
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  wifi_failed = false;
  selected_transport = RelayTransport::Usb;
  pending_usb_switch = false;
  detail = relay.usb_host_available() ? "Connecting Relay over USB" : "Waiting for USB bridge";
  handle_state(AgentState::Connecting);
  relay.begin_usb();
  Serial.println("Relay transport selected: USB");
}

void activate_wifi_transport() {
  end_active_session();
  stop_espnow_remote();
  selected_transport = RelayTransport::Wifi;
  pending_usb_switch = false;
  detail = "Starting Wi-Fi";
  handle_state(AgentState::Connecting);
  connect_wifi();
  Serial.println("Relay transport selected: Wi-Fi");
}

void set_connection_mode(ConnectionMode mode) {
  if (connection_mode == mode) return;
  connection_mode = mode;
  preferences.putUChar("conn_mode", static_cast<uint8_t>(mode));
  connection_mode_started_ms = millis();
  selected_transport = RelayTransport::None;
  end_active_session();
  stop_espnow_remote();
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  detail = String("Mode: ") + connection_mode_label(mode);
  handle_state(AgentState::Connecting);

  if (mode == ConnectionMode::Wifi) {
    activate_wifi_transport();
  } else if (mode == ConnectionMode::Usb) {
    activate_usb_transport();
  }
}

void manage_connection() {
  const bool usb_available = relay.usb_host_available();

  if (connection_mode == ConnectionMode::Wifi) {
    if (selected_transport != RelayTransport::Wifi) activate_wifi_transport();
    maintain_wifi();
    start_relay_when_wifi_ready();
    return;
  }

  if (connection_mode == ConnectionMode::Usb) {
    if (selected_transport != RelayTransport::Usb) activate_usb_transport();
    return;
  }

  // AUTO: wait briefly for the preferred USB path before powering up Wi-Fi.
  if (selected_transport == RelayTransport::None) {
    if (usb_available) {
      activate_usb_transport();
    } else if (millis() - connection_mode_started_ms >= kAutoUsbWaitMs) {
      activate_wifi_transport();
    }
    return;
  }

  if (selected_transport == RelayTransport::Usb) {
    if (!usb_available) activate_wifi_transport();
    return;
  }

  maintain_wifi();
  start_relay_when_wifi_ready();
  if (!usb_available) {
    pending_usb_switch = false;
    return;
  }
  if (conversation_active || state == AgentState::Listening || state == AgentState::Thinking ||
      state == AgentState::Searching || state == AgentState::Speaking) {
    pending_usb_switch = true;
    return;
  }
  activate_usb_transport();
}

void maintain_wifi() {
  report_wifi_status();
  if (WiFi.status() == WL_CONNECTED) return;
  if (wifi_failed) return;
  if (millis() - last_wifi_attempt >= kWifiReconnectIntervalMs) {
    if (wifi_attempt_count >= kWifiMaxAttempts) {
      wifi_failed = true;
      report_target_network_visibility();
      Serial.println("Wi-Fi retry limit reached");
      handle_state(AgentState::Error);
      return;
    }
    ++wifi_attempt_count;
    detail = "Retrying Wi-Fi connection";
    render_ui();
    Serial.printf("Wi-Fi retry %u/%u for SSID '%s'\\n", wifi_attempt_count,
                  kWifiMaxAttempts, WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    last_wifi_attempt = millis();
  }
}

void report_target_network_visibility() {
  const int network_count = WiFi.scanNetworks(false, true);
  int32_t target_rssi = 0;
  for (int index = 0; index < network_count; ++index) {
    if (WiFi.SSID(index) == WIFI_SSID) {
      target_rssi = WiFi.RSSI(index);
      break;
    }
  }
  WiFi.scanDelete();

  if (target_rssi == 0) {
    detail = "Wi-Fi failed: SSID not visible";
    Serial.printf("Wi-Fi scan: SSID '%s' not visible\\n", WIFI_SSID);
  } else {
    detail = String("Wi-Fi failed: SSID seen ") + target_rssi + " dBm";
    Serial.printf("Wi-Fi scan: SSID '%s' seen at %ld dBm\\n", WIFI_SSID,
                  static_cast<long>(target_rssi));
  }
}

void start_relay_when_wifi_ready() {
  if (relay_started || WiFi.status() != WL_CONNECTED) return;
  relay_started = true;
  detail = "Wi-Fi OK; connecting Relay";
  Serial.println("Wi-Fi connected; starting Relay");
  render_ui();
  start_espnow_remote();
  relay.begin_wifi(relay_address.toString().c_str(), relay_port);
}

void start_listening() {
  if (!conversation_active || !relay.connected()) return;
  resume_after_response = false;
  detail = "Listening... speak now";
  relay.send_control("audio.start");
  handle_state(AgentState::Listening);
}

void handle_state(AgentState next) {
  state = next;
  if (next == AgentState::Error || next == AgentState::Disconnected) {
    conversation_active = false;
    resume_after_response = false;
  }
  audio.set_capture_enabled(conversation_active && next == AgentState::Listening);
  render_ui();
  if (next == AgentState::Ready && pending_usb_switch) {
    conversation_active = false;
    resume_after_response = false;
    audio.set_capture_enabled(false);
    return;
  }
  if (next == AgentState::Ready && resume_after_response && conversation_active) {
    start_listening();
  }
}

void handle_response_done() {
  if (!conversation_active) return;
  resume_after_response = true;
  detail = "Response complete; your turn";
  render_ui();
}

void handle_session_reconnected() {
  if (!conversation_active) return;
  // Relay will send ready immediately after this notification. Re-enter
  // listening through the normal ready transition, not as a fake response.
  resume_after_response = true;
  detail = "Foundry reconnected; your turn";
  render_ui();
}

void handle_emotion(const char* next_emotion) {
  emotion = next_emotion;
  if (ui_page == UiPage::Face) render_ui();
}

void handle_notice(const char* title, const char* message) {
  detail = String(title) + ": " + message;
  if (detail.length() > 46) {
    detail = detail.substring(0, 43) + "...";
  }
  if (!strcmp(title, "RELAY ERROR") || !strcmp(title, "CONNECTION ERROR")) {
    conversation_active = false;
    resume_after_response = false;
    state = AgentState::Error;
    audio.set_capture_enabled(false);
  }
  render_ui();
}

void sync_ui_mode() {
  if (!relay.connected()) return;
  relay.send_ui_mode(ui_page == UiPage::Face ? "face" : "standard");
}

void note_activity(bool return_home) {
  last_activity_ms = millis();
  if (idle_mode == IdleMode::Active) return;
  idle_mode = IdleMode::Active;
  idle_gaze_x = 0;
  idle_gaze_y = 0;
  voice_activity_frames = 0;
  if (return_home && !camera_wipe_visible) M5StackChan.Motion.goHome(350);
  if (ui_page == UiPage::Face) render_ui();
}

void update_idle_behavior() {
  const uint32_t now = millis();
  const bool can_idle = ui_page == UiPage::Face &&
                        (state == AgentState::Ready || state == AgentState::Listening) &&
                        !camera_wipe_visible;
  if (!can_idle) {
    if (idle_mode != IdleMode::Active) note_activity(false);
    last_activity_ms = now;
    return;
  }

  if (idle_mode == IdleMode::Active) {
    if (now - last_activity_ms < kIdleAfterSilenceMs) return;
    idle_mode = IdleMode::LookingAround;
    idle_started_ms = now;
    next_idle_move_ms = now;
    if (ui_page == UiPage::Face) render_ui();
  }

  if (idle_mode == IdleMode::LookingAround && now - idle_started_ms >= kSleepAfterIdleMs) {
    idle_mode = IdleMode::Sleeping;
    idle_gaze_x = 0;
    idle_gaze_y = 0;
    M5StackChan.Motion.goHome(700);
    if (ui_page == UiPage::Face) render_ui();
    return;
  }

  if (idle_mode == IdleMode::LookingAround && now >= next_idle_move_ms) {
    const int yaw = random(-700, 701);
    const int pitch = random(300, 651);
    idle_gaze_x = map(yaw, -700, 700, -9, 9);
    idle_gaze_y = map(pitch, 300, 650, -5, 5);
    M5StackChan.Motion.move(yaw, pitch, random(kIdleMoveMinSpeed, kIdleMoveMaxSpeed + 1));
    idle_motion_settle_until_ms = now + kIdleMotionSettleMs;
    voice_activity_frames = 0;
    next_idle_move_ms = now + random(kIdleMoveMinIntervalMs, kIdleMoveMaxIntervalMs + 1);
    if (ui_page == UiPage::Face) render_ui();
  } else if (idle_mode == IdleMode::Sleeping && now - last_idle_render_ms >= 700) {
    last_idle_render_ms = now;
    if (ui_page == UiPage::Face) render_ui();
  }
}

void enter_face_listening() {
  ui_page = UiPage::Face;
  face_overlay_visible = false;
  camera_wipe_visible = false;
  note_activity(false);
  sync_ui_mode();
  if (!relay.connected()) {
    render_ui();
    return;
  }
  if (state == AgentState::Thinking || state == AgentState::Searching ||
      state == AgentState::Speaking) {
    audio.stop_playback();
    relay.send_control("conversation.pause");
  }
  conversation_active = true;
  start_listening();
}

void enter_face_sleeping() {
  ui_page = UiPage::Face;
  face_overlay_visible = false;
  camera_wipe_visible = false;
  conversation_active = false;
  resume_after_response = false;
  audio.set_capture_enabled(false);
  audio.stop_playback();
  if (relay.connected()) relay.send_control("conversation.pause");
  if (state == AgentState::Listening || state == AgentState::Thinking ||
      state == AgentState::Searching || state == AgentState::Speaking) {
    state = AgentState::Ready;
  }
  idle_mode = IdleMode::Sleeping;
  idle_gaze_x = 0;
  idle_gaze_y = 0;
  M5StackChan.Motion.move(0, kSleepingPitch, 700);
  sync_ui_mode();
  render_ui();
}

void handle_top_touch() {
  auto& top_touch = M5StackChan.TouchSensor;
  if (!top_touch.wasClicked()) return;

  const uint32_t now = millis();
  if (last_top_tap_ms != 0 && now - last_top_tap_ms <= kTopDoubleTapMs) {
    last_top_tap_ms = 0;
    enter_face_sleeping();
  } else {
    last_top_tap_ms = now;
    enter_face_listening();
  }
}

void adjust_volume(int delta) {
  audio.set_volume(constrain(static_cast<int>(audio.volume()) + delta, 0, 255));
  render_ui();
}

void handle_touch() {
  if (!M5.Touch.getCount()) {
    next_volume_repeat_ms = 0;
    volume_hold_adjusted = false;
    return;
  }
  const auto touch = M5.Touch.getDetail();
  note_activity();

  const bool volume_down = ui_page == UiPage::Settings && touch.y >= 84 &&
                           touch.y <= 126 && touch.x < 76;
  const bool volume_up = ui_page == UiPage::Settings && touch.y >= 84 &&
                         touch.y <= 126 && touch.x > 244;
  if ((volume_down || volume_up) && touch.isPressed()) {
    const uint32_t now = millis();
    if (next_volume_repeat_ms == 0) {
      next_volume_repeat_ms = now + kVolumeHoldDelayMs;
    } else if (now >= next_volume_repeat_ms) {
      adjust_volume(volume_down ? -8 : 8);
      volume_hold_adjusted = true;
      next_volume_repeat_ms = now + kVolumeRepeatIntervalMs;
    }
  }

  if (touch.wasFlicked() && abs(touch.distanceX()) > 50 &&
      abs(touch.distanceX()) > abs(touch.distanceY())) {
    int page = static_cast<int>(ui_page) + (touch.distanceX() < 0 ? 1 : -1);
    if (page < 0) page = 3;
    if (page > 3) page = 0;
    relay_endpoint_editing = false;
    ui_page = static_cast<UiPage>(page);
    face_overlay_visible = false;
    camera_wipe_visible = false;
    sync_ui_mode();
    render_ui();
    return;
  }

  if (!touch.wasClicked()) return;

  if ((ui_page != UiPage::Face || face_overlay_visible) && touch.y < kTabHeight) {
    const UiPage selected_page =
        static_cast<UiPage>(std::min(3, touch.x / (ui_sprite.width() / 4)));
    if (selected_page != UiPage::Network) relay_endpoint_editing = false;
    ui_page = selected_page;
    face_overlay_visible = false;
    camera_wipe_visible = false;
    sync_ui_mode();
    render_ui();
    return;
  }

  if (ui_page == UiPage::Network && relay_endpoint_editing) {
    if (touch.y >= 76 && touch.y <= 112) {
      relay_endpoint_field = std::min(3, touch.x / 76);
    } else if (touch.y >= 112 && touch.y <= 144) {
      relay_endpoint_field = 4;
    } else if (touch.y >= 144 && touch.y <= 184 && (touch.x < 112 || touch.x > 208)) {
      const int delta = touch.x < 112 ? -1 : 1;
      if (relay_endpoint_field < 4) {
        relay_address_draft[relay_endpoint_field] = constrain(
            static_cast<int>(relay_address_draft[relay_endpoint_field]) + delta, 0, 255);
      } else {
        relay_port_draft = constrain(static_cast<int>(relay_port_draft) + delta, 1, 65535);
      }
    } else if (touch.y >= 188 && touch.x < 160) {
      relay_address = IPAddress(relay_address_draft[0], relay_address_draft[1],
                                relay_address_draft[2], relay_address_draft[3]);
      relay_port = relay_port_draft;
      preferences.putString("relay_host", relay_address.toString());
      preferences.putUShort("relay_port", relay_port);
      relay_endpoint_editing = false;
      Serial.printf("Relay endpoint saved: %s:%u\n", relay_address.toString().c_str(), relay_port);
      if (selected_transport == RelayTransport::Wifi) activate_wifi_transport();
    } else if (touch.y >= 188 && touch.x >= 160) {
      relay_endpoint_editing = false;
    }
    render_ui();
    return;
  }

  // The top-left corner is reserved for the camera PIP in Face mode.  It is
  // intentionally handled before the conversation tap so opening/closing the
  // camera never starts or pauses a chat.
  if (ui_page == UiPage::Face && touch.x < kCameraWipeTapWidth &&
      touch.y < kCameraWipeTapHeight) {
    if (camera_wipe_visible) {
      camera_wipe_visible = false;
      face_tracking_locked = false;
      M5StackChan.Motion.goHome(350);
      detail = "Camera tracking stopped";
    } else if (start_camera()) {
      camera_wipe_visible = true;
      last_camera_frame = 0;
      face_tracking_locked = false;
      tracked_x = 0.0F;
      tracked_y = kCameraPitchDownBias;
      last_face_detected = millis();
      last_search_move = 0;
      search_step = 0;
      face_detection_streak = 0;
      last_face_measurement = 0;
      face_velocity_x = 0.0F;
      face_velocity_y = 0.0F;
      detail = "Camera tracking active";
    }
    render_ui();
    return;
  }

  if (ui_page == UiPage::Settings && touch.y >= 84 && touch.y <= 126) {
    if (!volume_hold_adjusted) {
      if (touch.x < 76) adjust_volume(-8);
      if (touch.x > 244) adjust_volume(8);
    }
    next_volume_repeat_ms = 0;
    volume_hold_adjusted = false;
    return;
  }

  if (ui_page == UiPage::Network && touch.y >= 76 && touch.y <= 112) {
    const int segment = std::min(2, touch.x / (M5.Display.width() / 3));
    set_connection_mode(static_cast<ConnectionMode>(segment));
    render_ui();
    return;
  }

  if (ui_page == UiPage::Network && touch.y >= 174 && touch.y <= 220) {
    for (int index = 0; index < 4; ++index) relay_address_draft[index] = relay_address[index];
    relay_port_draft = relay_port;
    relay_endpoint_field = 0;
    relay_endpoint_editing = true;
    render_ui();
    return;
  }

  if (ui_page == UiPage::Settings && touch.y >= 166 && touch.y <= 214) {
    if (touch.x < M5.Display.width() / 2 && espnow_receiver_id > 1) {
      --espnow_receiver_id;
    } else if (touch.x >= M5.Display.width() / 2 && espnow_receiver_id < 254) {
      ++espnow_receiver_id;
    }
    Serial.printf("ESP-NOW receiver ID: %u\n", espnow_receiver_id);
    render_ui();
    return;
  }

  if (state == AgentState::Ready) {
    conversation_active = true;
    start_listening();
  } else if (state == AgentState::Listening) {
    conversation_active = false;
    resume_after_response = false;
    detail = "Conversation paused";
    relay.send_control("conversation.pause");
    handle_state(AgentState::Ready);
  } else if (state == AgentState::Thinking || state == AgentState::Searching ||
             state == AgentState::Speaking) {
    if (ui_page == UiPage::Face) {
      audio.stop_playback();
      conversation_active = true;
      resume_after_response = false;
      relay.send_control("conversation.pause");
      start_listening();
      return;
    }
    conversation_active = false;
    resume_after_response = false;
    detail = "Conversation ended";
    // Stop locally first.  response.cancel travels through Relay and Foundry,
    // but a few already-received PCM frames must not remain audible.
    audio.stop_playback();
    relay.send_control("conversation.pause");
    handle_state(AgentState::Ready);
  }
}
}  // namespace

void setup() {
  Serial.setRxBufferSize(16 * 1024);
  Serial.setTxBufferSize(16 * 1024);
  Serial.begin(115200);
  randomSeed(esp_random());
  M5StackChan.begin();
  M5StackChan.Motion.setAutoAngleSyncEnabled(false);
  audio.begin();
  ui_sprite.setColorDepth(16);
  ui_sprite.setPsram(true);
  ui_sprite.createSprite(M5.Display.width(), M5.Display.height());
  last_activity_ms = millis();
  preferences.begin("stackchan", false);
  const String stored_relay_address = preferences.getString("relay_host", RELAY_HOST);
  if (!relay_address.fromString(stored_relay_address) && !relay_address.fromString(RELAY_HOST)) {
    relay_address = IPAddress(127, 0, 0, 1);
  }
  relay_port = preferences.getUShort("relay_port", RELAY_PORT);
  if (relay_port == 0) relay_port = RELAY_PORT;
  const uint8_t stored_mode = preferences.getUChar("conn_mode", 0);
  connection_mode = stored_mode <= static_cast<uint8_t>(ConnectionMode::Usb)
                        ? static_cast<ConnectionMode>(stored_mode)
                        : ConnectionMode::Auto;
  connection_mode_started_ms = millis();
  handle_state(AgentState::Connecting);

  relay.on_state(handle_state);
  relay.on_response_done(handle_response_done);
  relay.on_session_reconnected(handle_session_reconnected);
  relay.on_emotion(handle_emotion);
  relay.on_notice(handle_notice);
  relay.on_audio([](const uint8_t* data, size_t length) {
    if (length % sizeof(int16_t) != 0) return;
    // Ignore audio that raced with a local cancellation.
    if (!conversation_active || state != AgentState::Speaking) return;
    audio.play(reinterpret_cast<const int16_t*>(data), length / sizeof(int16_t));
  });
}

void loop() {
  M5StackChan.update();
  // React to the sampled touch before dispatching another batch of response
  // audio. UsbTransport also yields bounded receive work under a busy stream.
  handle_top_touch();
  handle_touch();
  relay.loop();
  manage_connection();
  static bool relay_was_connected = false;
  if (relay.connected() && !relay_was_connected) sync_ui_mode();
  relay_was_connected = relay.connected();
  audio.loop();
  apply_espnow_remote();
  update_idle_behavior();

  if (ui_page == UiPage::Face) {
    if (face_overlay_visible && millis() >= face_overlay_until) {
      face_overlay_visible = false;
      render_ui();
    }
    static uint32_t last_lip_render = 0;
    if (state == AgentState::Speaking && millis() - last_lip_render >= 110) {
      last_lip_render = millis();
      // Keep the last camera frame in the PIP while lip sync updates.  A
      // mouth-only transfer would otherwise erase the PIP until the next
      // camera frame, which was the visible flicker during speech.
      if (camera_wipe_visible) {
        render_ui();
      } else {
        render_face_mouth();
        present_ui();
      }
    }
    static uint32_t last_search_render = 0;
    if (state == AgentState::Searching && millis() - last_search_render >= 140) {
      last_search_render = millis();
      render_search_animation_frame();
      present_ui();
    }
    update_camera_wipe();
  }

  if (relay.connected() && state == AgentState::Listening && conversation_active) {
    const size_t captured = audio.capture(capture_buffer, kFrameSamples);
    if (captured > 0) {
      uint32_t magnitude_sum = 0;
      for (size_t index = 0; index < captured; ++index) {
        const int16_t sample = capture_buffer[index];
        magnitude_sum += sample == INT16_MIN ? INT16_MAX : abs(sample);
      }
      const bool voice_detected = magnitude_sum / captured >= kVoiceActivityMean;
      if (idle_mode != IdleMode::Active && millis() >= idle_motion_settle_until_ms &&
          voice_detected) {
        if (++voice_activity_frames >= kVoiceActivityConfirmFrames) note_activity();
      } else {
        voice_activity_frames = 0;
      }
      relay.send_audio(capture_buffer, captured);
    }
  }

  delay(1);
}
