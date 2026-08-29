# stack-chan-realtime-local

[日本語](README.md) | English

This is a voice agent that connects Stack-chan to a Windows PC on the same local network and uses the Microsoft Foundry Realtime API through the Relay running on that PC.

For a first-time setup, follow [Getting Started with Stack-chan](docs/en/how-to-start.md) from Azure resource preparation through USB connection, configuration, and firmware flashing.

Based on the original [`mastermoro/stack-chan-realtime`](https://github.com/mastermoro/stack-chan-realtime), this version moves the Relay's primary runtime location from Azure Container Apps to a Windows PC. It retains Foundry Realtime, Responses API web search, and the Stack-chan firmware.

## Architecture

```text
Stack-chan (CoreS3)
  ├─ PCM16 / 24 kHz / mono
  ├─ Wi-Fi WebSocket ─────────────────>┐
  └─ USB serial -> Windows USB bridge ─┤
                                       v
Windows PC / Local Relay
  ├─ Device authentication and conversation state management
  ├─ Foundry Realtime connection
  ├─ Responses API web_search
  └─ Registered local functions
       └─ open_browser_url -> Default browser
                 |
                 v
Microsoft Foundry
  ├─ Realtime model
  └─ Responses model + web_search
```

On Windows, only explicitly registered functions are executed. Arbitrary PowerShell, commands, and file operations are not exposed to the model.

## Getting Started on Windows

Prerequisites:

- Windows 10 or 11
- Microsoft Foundry / Azure OpenAI resource
- Model deployments for Realtime and Responses
- An API key, or an Azure login available through `DefaultAzureCredential`

After cloning the repository, double-click `setup.cmd` in the repository root.

The setup GUI lets you perform the following tasks in one place:

- Check and install Python 3.11 or later, Relay dependencies, and PlatformIO
- Enter the Foundry endpoint, deployment names, API key, and other settings
- Enter Stack-chan's Wi-Fi details, device ID, and device token, and safely generate a token
- Start Relay Manager
- Flash the firmware to a USB-connected Stack-chan

If Python is not installed, the GUI uses Windows `winget` to install Python 3.12 for the current user. If you use Azure CLI authentication instead of an API key, the GUI displays Azure CLI itself as an optional item with its current status.
On subsequent runs, package installation is skipped when `pyproject.toml` and the selected extras are unchanged and the installed dependencies remain healthy. Run `relay\setup-windows.ps1 -WithHeadset -Force` to force an update. If the Manager is already healthy, "保存してManagerを起動" opens the existing UI instead of starting a duplicate process.

For command-line setup, you can continue to run the following commands:

```powershell
cd relay
.\setup-windows.ps1 -WithHeadset
notepad .env
.\run-manager.ps1
```

Open `http://127.0.0.1:8787` in a browser and start the Relay. The Manager UI is exposed only within the PC, while the Stack-chan Relay listens on `0.0.0.0:8080` for LAN connections.
The USB bridge also starts with the Relay and automatically detects Espressif USB COM ports.
To exit, click "Managerを終了" at the bottom of the Manager UI. It stops the managed Relay, USB bridge, and headset test before terminating the Manager itself.

Minimum configuration for `relay/.env`:

```dotenv
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AZURE_OPENAI_REALTIME_DEPLOYMENT=gpt-realtime-2.1
AZURE_OPENAI_RESPONSES_DEPLOYMENT=gpt-5.6-terra
AZURE_OPENAI_API_KEY=

DEVICE_TOKENS_JSON={"stackchan-001":"長いランダムな端末トークン"}
```

To enable opening pages in the local PC's browser by voice:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

Leaving the allowed domains empty permits any HTTP(S) host. Initially, listing only the required domains is recommended.

## Configuring Stack-chan

For the initial setup only, copy `stackchan/include/secrets.example.hpp` to `secrets.hpp`, then configure the Wi-Fi credentials and the same device token registered in `DEVICE_TOKENS_JSON` in `relay/.env`. If you used `setup-windows.ps1`, the file has already been created.

```cpp
#define WIFI_SSID "..."
#define WIFI_PASSWORD "..."
#define DEVICE_ID "stackchan-001"
#define DEVICE_TOKEN "長いランダムな端末トークン"
```

Start the Relay from the Manager UI, connect the CoreS3 by USB, and run the following on the Relay PC:

```powershell
cd stackchan
.\deploy-from-relay.ps1
```

This script selects a LAN IPv4 address from the active default routes on Windows, generates the untracked `include/relay_endpoint.hpp`, and then builds and flashes with PlatformIO. It does not overwrite the Wi-Fi credentials or device token files. Before flashing, it also checks `http://選択したIP:8080/healthz`.

For multiple NICs, VPNs, or unusual network configurations, you can specify how the address is selected.

```powershell
# Stack-chanの現在のIPへ到達する経路から選択
.\deploy-from-relay.ps1 -StackChanAddress 192.168.1.50

# Windowsのアダプター名で選択
.\deploy-from-relay.ps1 -InterfaceAlias "Wi-Fi"

# Relay PCのアドレスを明示
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10

# 書き込まずビルドだけ行う
.\deploy-from-relay.ps1 -BuildOnly

# シリアルポートを明示
.\deploy-from-relay.ps1 -UploadPort COM5
```

Use `-ResolveOnly` to safely check only the selection result. Specify `-SkipRelayCheck` only when flashing before the Relay has been started.

If a Windows Defender Firewall prompt appears, allow access only on trusted private networks. Do not forward port 8080 to the internet.

## USB / Wi-Fi Modes

Select the connection route with `AUTO`, `WI-FI`, or `USB` on Stack-chan's `NETWORK` tab. The selection is saved on the device and persists after restarting.
Tap `Relay endpoint` on the same tab to edit the IPv4 address and port used for Wi-Fi. Tap a field, adjust it with `-` / `+`, then tap `SAVE` to persist the endpoint and reconnect over Wi-Fi.

- `AUTO`: Waits three seconds for USB at startup, then connects over Wi-Fi if USB is unavailable. While USB is in use, Wi-Fi and ESP-NOW are stopped; Wi-Fi starts when USB disconnects. If USB is detected during a conversation over Wi-Fi, the route switches after the response finishes.
- `WI-FI`: Uses only the conventional Wi-Fi connection and does not automatically fall back to USB.
- `USB`: Uses only the USB connection and does not switch to Wi-Fi when the cable is disconnected. The ESP-NOW remote control cannot be used while connected by USB.

Manually changing the route ends the current conversation and context, then starts a session on the new connection. In the Manager's USB bridge section, you can view the COM port, device ID, connection status, corrupted-frame and sequence-gap statistics, and device logs.

When flashing firmware, `deploy-from-relay.ps1` temporarily pauses the Manager-managed USB bridge to release the COM port and automatically resumes it afterward. When manually using PlatformIO upload or the serial monitor, first click "ブリッジを停止" in the Manager.

Design decisions, hardware validation conditions, and acceptance tests are documented in [`docs/en/usb-relay-mode-plan.md`](docs/en/usb-relay-mode-plan.md).

## Operating Stack-chan

- Tap the Face screen to start a conversation; tap it again during the conversation to pause.
- After 30 seconds without interaction or speech while idle on Face, the cat face
  looks around. After five minutes in that state, it changes to the sleeping
  display; touch or speech returns it to the normal display.
- Tap the top of the CoreS3 once to move to the Face screen and start a conversation.
- Tap the top of the CoreS3 twice within two seconds to stop the conversation and playback, then move the neck down into the sleep posture.
- Tap the volume `-` / `+` controls in Settings to adjust by one step, or press and hold for continuous adjustment.
- Tap the camera area in the upper-left corner of the Face screen to toggle face tracking on and off.

## Browser Function

When the user explicitly asks to "open this page on the Windows PC," Foundry can call `open_browser_url`. The Relay checks the following before passing the URL to the default Windows browser:

- Whether the feature is enabled in the configuration
- Whether the URL uses `http://` or `https://`
- Whether a username or password is embedded in the URL
- Whether the URL is within the allowed domains

See [`docs/en/local-tools.md`](docs/en/local-tools.md) for implementation and extension details.

## AEC and Full Duplex

The design for diagnostics, a fixed ring buffer, playback synchronization, state management, and half-duplex fallback before implementing CoreS3-side AEC is summarized in [`docs/en/aec-roadmap.md`](docs/en/aec-roadmap.md). Dependency pinning and a safe half-duplex baseline build have already been incorporated into this repository. Full-duplex I2S and ESP-SR AEC will be enabled incrementally after passing hardware gates.

## Validation

```powershell
cd relay
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest

Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:8080/capabilities
```

## Repository Structure

```text
stackchan/  CoreS3 firmware
relay/      Windows LAN Relay, Manager UI, and local functions
docs/       Design, protocol, and operations documentation
infra/      Optional Azure Container Apps configuration from the original repository
```

`infra/` is retained for compatibility and future options, but it is not used in the standard operation of this fork.

## Security

- Do not commit `relay/.env`, `secrets.hpp`, the generated `relay_endpoint.hpp`, API keys, or device tokens.
- Use `ws://` between the Relay and Stack-chan only within a trusted LAN.
- Do not change Manager UI port 8787 from `127.0.0.1`.
- Keep local functions registration-based, and do not add generic shell execution.
- Allow only the required domains for the browser function.
- Use TLS and additional access controls on public Wi-Fi, guest LANs, or when exposing the service to the internet.
