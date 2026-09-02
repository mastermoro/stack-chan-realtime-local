[日本語](../development.md)

# Development workflow

## Branching

- `main`: protected integration branch.
- Feature branches: `agent/<description>` or `feature/<description>`.
- Changes are merged by pull request.
- Keep firmware, relay, and infrastructure changes independently reviewable where practical.

## Checks

Relay:

```bash
cd relay
pip install -e '.[dev]'
ruff check app tests
pytest
```

Firmware:

```bash
cd stackchan
pio run
```

## Run the Windows LAN Relay

The local Relay environment uses `relay/.venv` and `relay/.env`.

For the normal first-run flow, launch `setup.cmd` from the repository root.
The native Windows setup UI works before Python dependencies are installed and
configures both `relay/.env` and the ignored Stack-chan credentials header.
Dependency state is recorded in `relay/.venv/.stackchan-install.json`. Installation
is skipped on subsequent runs when `pyproject.toml`, the selected extras, and
`pip check` are unchanged. Pass `setup-windows.ps1 -Force` to refresh it.

```powershell
cd relay
.\setup-windows.ps1 -WithHeadset
.\run-local.ps1 -Reload
```

It listens on `0.0.0.0:8080` by default so Stack-chan can reach it from the
trusted LAN. Verify the process locally with:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
```

`healthz` confirms that the HTTP process is running. `readyz` returns `ready` only
after `AZURE_OPENAI_ENDPOINT` and both deployment names in `relay/.env` have real
values. A device session additionally needs either `AZURE_OPENAI_API_KEY` or a
locally available `DefaultAzureCredential`, such as a signed-in Azure CLI session.

To restrict a direct development run to this PC, specify:

```powershell
.\run-local.ps1 -ListenAddress 127.0.0.1 -Reload
```

For a LAN Relay, deploy the firmware from the Relay PC:

```powershell
cd stackchan
.\deploy-from-relay.ps1
```

The script selects the IPv4 address on the preferred physical default route,
generates ignored `include/relay_endpoint.hpp`, verifies `/healthz`, builds, and
uploads the firmware. It never rewrites `include/secrets.hpp`. If a VPN or
multiple adapters make the route ambiguous, select the path explicitly:

```powershell
.\deploy-from-relay.ps1 -StackChanAddress 192.168.1.50
.\deploy-from-relay.ps1 -InterfaceAlias "Wi-Fi"
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10
```

`-StackChanAddress` asks Windows which local source address it would use to
reach that device; no application payload is sent. `-ResolveOnly` prints the
selection without changing files, and `-BuildOnly` generates and builds without
uploading. The default is `ws://`; use `-UseTls` only with a TLS endpoint whose
certificate the device trusts. Do not expose the plain-WebSocket listener
beyond a trusted LAN.

## Local Relay manager UI

Start the manager, then open `http://127.0.0.1:8787` in a browser:

```powershell
cd relay
.\run-manager.ps1
```

To replace an already running manager after a code update, use:

```powershell
.\run-manager.ps1 -Restart
```

`-Restart` first asks the existing Manager to stop its managed Relay, then only
stops a process on the selected port when it can verify that it belongs to this
Relay Manager; it will not terminate another application.

The UI starts and stops a Relay child process, displays its PID and exit code, and
tails its local stdout/stderr log. The Manager UI itself is bound to `127.0.0.1`,
while the Relay child is bound to `0.0.0.0:8080` so Stack-chan can connect from
the LAN. Its status display checks the local `http://127.0.0.1:8080/healthz`
endpoint and distinguishes a Relay it manages from one started outside the UI. A managed Relay is stopped when
the Manager exits or restarts; an external Relay is shown as such and is never
terminated by the Manager.

The same UI provides a headset test. It captures the selected (or default) local
microphone as PCM16 / 24 kHz / mono, sends it to the local Relay as a device, and
plays returned audio on the selected output device. Install its optional local
dependency when setting up a new environment:

```powershell
cd relay
.\.venv\Scripts\python.exe -m pip install -e '.[dev,headset]'
```

The test requires a configured Foundry endpoint, deployment names, and device
token in `relay/.env`; it does not require Stack-chan hardware. First use
`テストを起動` to establish and retain one Relay/Realtime session. `会話を開始` then
starts a continuous half-duplex conversation: server VAD detects each utterance,
microphone input stops while Foundry is thinking or speaking, and it automatically
returns to listening after each completed response. `会話を一時停止` preserves the
same session and its context; use `テストを停止` only when the session can be ended.

## Stack-chan controls and UI

The CoreS3 firmware is half duplex. The Face screen is the normal conversation
screen and keeps the lip sync visible; tabs and diagnostics are hidden there to
avoid display flicker. Swipe horizontally to move among Status, Face, and
Settings. Settings provides device-side volume adjustment and Status shows the
current network/Relay state without displaying credentials.

Tap the Face screen to control a continuous conversation, so another tap is not
required after every response:

- `READY`: tap the display to start a conversation and microphone streaming.
- `LISTENING`: tap to end the conversation and stop microphone streaming.
- `THINKING`, `SEARCHING`, or `SPEAKING`: tap to cancel the current response and
  playback, then return to `LISTENING`.

The upper-left 64×64px area of the Face screen is reserved for the camera
wipe. Tap it to show the CoreS3 camera picture-in-picture and start face
tracking; tap it again to hide the wipe, stop tracking, and return the head to
its home position. All other areas keep the conversation controls above.

### Cat face and idle behavior

The cat face is the standard avatar on the Face screen. Relay's `ui.mode=face`
switches to a cat-like speaking style without resetting conversation context.
On the device, the eyes, mouth, cheeks, and searching motion reflect the
conversation state and `emotion` value.

Automatic idle behavior runs only on Face while the state is `READY` or
`LISTENING` and the camera wipe is hidden.

1. After 30 seconds since the last interaction or detected speech, enter
  `LookingAround` and show the cat idle face.
2. In `LookingAround`, move the gaze and neck at random intervals from 2 to 15 seconds.
3. Five minutes after `LookingAround` begins, return the neck home, enter
  `Sleeping`, and update the closed eyes, breathing motion, and `Z` display
  about every 700 ms.
4. Return to `Active` after a touch, or after detecting voice activity for three
  consecutive frames while `LISTENING`. If the camera wipe is hidden, also
  return the neck to its home position.

Automatic `Sleeping` is a visual idle state and does not pause the conversation
session. Explicit sleep from a top double tap is a separate action: it stops
capture and playback, sends `conversation.pause`, and lowers the neck into the
sleep posture.

The CoreS3 top touch sensor provides controls that work from every UI page. A
single tap opens Face, hides overlays, and starts listening when the Relay is
connected. A second top tap within two seconds pauses the conversation, stops
capture and playback, and moves the head to its sleeping pitch. On Settings,
the volume minus and plus controls repeat after a 500 ms hold; a short tap still
changes the volume by one step.

Server VAD determines the end of each user utterance. The microphone is disabled
during response generation and audio playback, then automatically resumes when
the Relay sends `response.done` followed by `ready`.

## Windows-local Function Calling

The local Relay can register `open_browser_url` with Foundry. It is disabled by
default. Enable it and optionally restrict hosts in `relay/.env`:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

The tool accepts only HTTP(S) URLs and does not expose PowerShell or arbitrary
commands. See `docs/local-tools.md` before adding another Windows action.

The face changes with the Relay's neutral, happy, sad, angry, surprised, and
sleepy emotion events. During web search it shows a gentle searching animation.
Status also shows Relay connection notices, concise error details, and the first
title returned by web search. Wi-Fi and WebSocket reconnect automatically after
a connection loss.

## Secrets

Never commit:

- `relay/.env`
- `stackchan/include/secrets.hpp`
- `stackchan/include/relay_endpoint.hpp`
- Azure API keys
- Wi-Fi credentials
- device tokens
- captured PCM/WAV files
