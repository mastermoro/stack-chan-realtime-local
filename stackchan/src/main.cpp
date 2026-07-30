#include <Arduino.h>
#include <WiFi.h>
#include <M5StackChan.h>
#include <esp_now.h>
#include <esp_camera.h>

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
enum class UiPage : uint8_t { Status, Face, Settings };
UiPage ui_page = UiPage::Status;
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
uint16_t camera_wipe_cache[128 * 96];
String emotion = "neutral";
String detail = "Connecting to Wi-Fi";
uint32_t last_wifi_attempt = 0;
wl_status_t last_wifi_status = WL_IDLE_STATUS;
uint8_t wifi_attempt_count = 0;
bool wifi_failed = false;
bool espnow_ready = false;
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
  static constexpr const char* kTabs[] = {"STATUS", "FACE", "SETTINGS"};
  const int tab_width = ui_sprite.width() / 3;
  ui_sprite.fillRect(0, 0, ui_sprite.width(), kTabHeight, TFT_DARKGREY);
  ui_sprite.setTextSize(1);
  for (int index = 0; index < 3; ++index) {
    const bool selected = index == static_cast<int>(ui_page);
    const int x = index * tab_width;
    ui_sprite.fillRect(x + 2, 3, tab_width - 4, kTabHeight - 6,
                        selected ? state_color() : TFT_BLACK);
    ui_sprite.setTextColor(selected ? TFT_BLACK : TFT_LIGHTGREY,
                            selected ? state_color() : TFT_BLACK);
    ui_sprite.setCursor(x + 10, 11);
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
  ui_sprite.printf("Relay: %s:%d", RELAY_HOST, RELAY_PORT);
  draw_action_button(color);
}

void render_face_mouth() {
  const uint32_t color = state_color();
  const int center_x = ui_sprite.width() / 2;
  ui_sprite.fillRect(center_x - 48, 140, 96, 46, TFT_BLACK);
  if (state == AgentState::Speaking) {
    const int mouth_height = 12 + ((millis() / 130) % 3) * 10;
    ui_sprite.fillRoundRect(center_x - 34, 160 - mouth_height / 2, 68, mouth_height, 8,
                             color);
  } else if (state == AgentState::Listening) {
    ui_sprite.drawRoundRect(center_x - 32, 152, 64, 26, 12, color);
  } else if (state != AgentState::Error && state != AgentState::Disconnected &&
             state != AgentState::Thinking && state != AgentState::Searching) {
    if (emotion == "sad") {
      ui_sprite.drawLine(center_x - 32, 172, center_x, 156, TFT_SKYBLUE);
      ui_sprite.drawLine(center_x, 156, center_x + 32, 172, TFT_SKYBLUE);
    } else if (emotion == "angry") {
      ui_sprite.fillRoundRect(center_x - 26, 160, 52, 8, 4, TFT_ORANGE);
    } else if (emotion == "surprised") {
      ui_sprite.drawCircle(center_x, 164, 14, color);
    } else if (emotion == "happy") {
      ui_sprite.drawLine(center_x - 34, 154, center_x, 174, TFT_PINK);
      ui_sprite.drawLine(center_x, 174, center_x + 34, 154, TFT_PINK);
    } else {
      ui_sprite.drawLine(center_x - 26, 164, center_x, 170, color);
      ui_sprite.drawLine(center_x, 170, center_x + 26, 164, color);
    }
  }
}

void render_search_animation_frame() {
  const int center_x = ui_sprite.width() / 2;
  const int sway = static_cast<int>((millis() / 260) % 3) - 1;
  const int pulse = static_cast<int>((millis() / 260) % 3);
  ui_sprite.fillRect(12, 48, ui_sprite.width() - 24, 142, TFT_BLACK);

  // Soft focused eyes: a gentle, studious look rather than rapid eye movement.
  ui_sprite.fillRoundRect(50, 74, 68, 32, 15, TFT_CYAN);
  ui_sprite.fillRoundRect(202, 74, 68, 32, 15, TFT_CYAN);
  ui_sprite.fillCircle(86, 91 + sway, 5, TFT_BLACK);
  ui_sprite.fillCircle(234, 91 + sway, 5, TFT_BLACK);
  ui_sprite.drawLine(54, 66, 112, 70, TFT_LIGHTGREY);
  ui_sprite.drawLine(206, 70, 264, 66, TFT_LIGHTGREY);

  // A rounded research note and a small moving magnifier read as "working".
  ui_sprite.fillRoundRect(102, 120, 76, 48, 8, TFT_DARKGREY);
  ui_sprite.drawRoundRect(102, 120, 76, 48, 8, TFT_LIGHTGREY);
  ui_sprite.drawLine(116, 134, 164, 134, TFT_LIGHTGREY);
  ui_sprite.drawLine(116, 145, 154, 145, TFT_LIGHTGREY);
  ui_sprite.drawLine(116, 156, 146, 156, TFT_LIGHTGREY);
  const int glass_x = 206 + sway * 4;
  ui_sprite.drawCircle(glass_x, 142, 15, TFT_PINK);
  ui_sprite.drawLine(glass_x + 11, 153, glass_x + 25, 167, TFT_PINK);
  for (int dot = 0; dot < 3; ++dot) {
    ui_sprite.fillCircle(center_x - 14 + dot * 14, 184, dot == pulse ? 4 : 2, TFT_PINK);
  }
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
  if (state == AgentState::Error || state == AgentState::Disconnected) {
    for (int side : {-1, 1}) {
      const int x = center_x + side * eye_offset;
      ui_sprite.drawLine(x - 16, eye_y - 16, x + 16, eye_y + 16, color);
      ui_sprite.drawLine(x + 16, eye_y - 16, x - 16, eye_y + 16, color);
    }
    ui_sprite.drawLine(center_x - 34, 166, center_x + 34, 166, color);
  } else if (state == AgentState::Searching) {
    render_search_animation_frame();
  } else if (state == AgentState::Thinking) {
    const int bob = (millis() / 180) % 3;
    ui_sprite.fillCircle(center_x - eye_offset, eye_y + bob, 13, color);
    ui_sprite.fillCircle(center_x + eye_offset, eye_y - bob, 13, color);
    ui_sprite.drawCircle(center_x, 164, 18, color);
  } else {
    if (emotion == "sad") {
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
      ui_sprite.fillRoundRect(center_x - eye_offset - 22, eye_y - 26, 44, 52, 20, color);
      ui_sprite.fillRoundRect(center_x + eye_offset - 22, eye_y - 26, 44, 52, 20, color);
      ui_sprite.fillCircle(center_x - eye_offset + 8, eye_y - 8, 6, TFT_BLACK);
      ui_sprite.fillCircle(center_x + eye_offset + 8, eye_y - 8, 6, TFT_BLACK);
      if (emotion == "happy") {
        ui_sprite.fillCircle(center_x - 104, 152, 12, TFT_PINK);
        ui_sprite.fillCircle(center_x + 104, 152, 12, TFT_PINK);
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
  ui_sprite.setCursor(12, 138);
  ui_sprite.printf("Wi-Fi: %s", WIFI_SSID);
  ui_sprite.setCursor(12, 156);
  ui_sprite.printf("IP: %s", WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str()
                                                               : "not connected");
  ui_sprite.setCursor(12, 174);
  ui_sprite.printf("Relay: %s:%d%s", RELAY_HOST, RELAY_PORT,
                    RELAY_USE_TLS ? " (TLS)" : "");
  ui_sprite.setCursor(12, 192);
  ui_sprite.printf("ESP-NOW channel: %u", WiFi.status() == WL_CONNECTED ? WiFi.channel() : 0);
  ui_sprite.setCursor(12, 212);
  ui_sprite.printf("Remote ID: < %u >", espnow_receiver_id);
}

void render_ui(bool should_present) {
  ui_sprite.fillScreen(TFT_BLACK);
  if (ui_page != UiPage::Face || face_overlay_visible) draw_tabs();
  if (ui_page == UiPage::Status) render_status();
  if (ui_page == UiPage::Face) render_face(face_overlay_visible);
  if (ui_page == UiPage::Face && camera_wipe_visible) draw_cached_camera_wipe();
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
  relay.begin();
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

void handle_touch() {
  if (!M5.Touch.getCount()) return;
  const auto touch = M5.Touch.getDetail();

  if (touch.wasFlicked() && abs(touch.distanceX()) > 50 &&
      abs(touch.distanceX()) > abs(touch.distanceY())) {
    int page = static_cast<int>(ui_page) + (touch.distanceX() < 0 ? 1 : -1);
    if (page < 0) page = 2;
    if (page > 2) page = 0;
    ui_page = static_cast<UiPage>(page);
    face_overlay_visible = false;
    camera_wipe_visible = false;
    render_ui();
    return;
  }

  if (!touch.wasClicked()) return;

  if ((ui_page != UiPage::Face || face_overlay_visible) && touch.y < kTabHeight) {
    ui_page = static_cast<UiPage>(std::min(2, touch.x / (ui_sprite.width() / 3)));
    face_overlay_visible = false;
    camera_wipe_visible = false;
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
    int volume = audio.volume();
    if (touch.x < 76) volume -= 8;
    if (touch.x > 244) volume += 8;
    audio.set_volume(constrain(volume, 0, 255));
    render_ui();
    return;
  }

  if (ui_page == UiPage::Settings && touch.y >= 196 && touch.y <= 236) {
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
  Serial.begin(115200);
  M5StackChan.begin();
  M5StackChan.Motion.setAutoAngleSyncEnabled(false);
  audio.begin();
  ui_sprite.setColorDepth(16);
  ui_sprite.setPsram(true);
  ui_sprite.createSprite(M5.Display.width(), M5.Display.height());
  handle_state(AgentState::Connecting);
  connect_wifi();

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
  M5.update();
  maintain_wifi();
  start_relay_when_wifi_ready();
  relay.loop();
  audio.loop();
  handle_touch();
  apply_espnow_remote();

  if (ui_page == UiPage::Face) {
    if (face_overlay_visible && millis() >= face_overlay_until) {
      face_overlay_visible = false;
      render_ui();
    }
    static uint32_t last_lip_render = 0;
    if (state == AgentState::Speaking && millis() - last_lip_render >= 130) {
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
      relay.send_audio(capture_buffer, captured);
    }
  }

  delay(1);
}
