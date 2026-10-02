#include "protocol.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace stackchan;
enum class UiPage { Status, Face };
UiPage ui_page = UiPage::Face;
AgentState state = AgentState::Speaking;
bool face_overlay_visible = false;
bool camera_wipe_visible = false;
bool conversation_active = true;
bool resume_after_response = false;
bool context_reset_on_resume = false;
bool pending_usb_switch = false;
String detail;
void handle_state(AgentState next);
void start_listening();
void render_ui() {}
void note_activity(bool) {}
void sync_ui_mode() {}

struct AudioFake {
  size_t stops = 0;
  bool capturing = false;
  void stop_playback() { ++stops; }
  void set_capture_enabled(bool enabled) { capturing = enabled; }
} audio;
struct RelayFake {
  bool fail_pause = false;
  size_t pauses = 0;
  size_t starts = 0;
  bool connected() const { return true; }
  bool send_control(const char* type) {
    if (!strcmp(type, "conversation.pause")) {
      ++pauses;
      if (fail_pause) {
        // Actual RelayClient failed-send callback behavior, tested separately.
        handle_state(AgentState::Error);
        return false;
      }
    }
    if (!strcmp(type, "audio.start")) ++starts;
    return true;
  }
} relay;

// Generated from the production main.cpp functions by run_tests.py. This
// executes the actual Face control flow without faking the whole M5 stack.
#include "main_face_controls_under_test.hpp"

void require(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}
int main(int argc, char** argv) {
  try {
    require(argc == 2, "expected test name");
    const std::string name = argv[1];
    if (name == "context_reset") {
      handle_session_reconnected();
      handle_state(AgentState::Ready);
      require(state == AgentState::Listening && audio.capturing && conversation_active,
              "upstream reconnect changed automatic listening behavior");
      require(detail == "Context reset; speak now", "resumed Listening hid the context reset notice");
      std::cout << "PASS face/" << name << '\n';
      return EXIT_SUCCESS;
    }
    relay.fail_pause = name.find("failed") != std::string::npos;
    if (name.rfind("top_", 0) == 0) enter_face_listening();
    else handle_conversation_touch();
    require(audio.stops == 1 && relay.pauses == 1, "Face control did not stop playback and pause once");
    if (relay.fail_pause) {
      require(state == AgentState::Error && !conversation_active && !audio.capturing,
              "failed pause was hidden by locally resuming listening");
      require(relay.starts == 0, "failed pause incorrectly sent audio.start");
    } else {
      require(state == AgentState::Listening && conversation_active && audio.capturing,
              "successful Face interruption did not immediately enable capture");
      require(relay.starts == 1, "successful Face interruption did not send audio.start once");
    }
    std::cout << "PASS face/" << name << '\n';
    return EXIT_SUCCESS;
  } catch (const std::exception& error) {
    std::cerr << "FAIL face/" << (argc > 1 ? argv[1] : "?") << ": " << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
