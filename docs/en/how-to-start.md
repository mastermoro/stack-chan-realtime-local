[日本語](../how-to-start.md) | **English**

# Getting Started with Stack-chan

This guide takes a new user from the first USB connection of a Stack-chan based on M5Stack CoreS3 through Microsoft Foundry configuration, firmware flashing, and the first voice conversation.

## 1. System overview

The standard configuration runs the Relay on a Windows PC on the same trusted LAN as Stack-chan. Azure Container Apps, a container registry, and a separate server are not required.

The only required cloud component is a Microsoft Foundry / Azure OpenAI resource with two model deployments:

- a Realtime-capable model for voice conversations;
- a Responses API model with web-search support.

Stack-chan connects to the Windows Relay over USB or Wi-Fi. Only the Relay connects to Azure.

## 2. What you need

- A Stack-chan built around M5Stack CoreS3
- A data-capable USB Type-C cable
- A Windows 10 or Windows 11 PC
- A 2.4 GHz Wi-Fi/LAN reachable by both the PC and Stack-chan
- A Microsoft Azure subscription
- A clone or extracted copy of this repository
- Approximately 3 GB of free disk space for Python, PlatformIO, and ESP32 build tools

> **Wi-Fi hardware limitation:** The ESP32-S3 in CoreS3 supports 2.4 GHz Wi-Fi only and cannot connect to 5 GHz networks. If the access point uses separate SSIDs for 2.4 GHz and 5 GHz, select the 2.4 GHz SSID. Wi-Fi cannot be used with an access point configured for 5 GHz only.

## 3. Prepare Microsoft Foundry

### 3.1 Create the AI resource

In Azure Portal or the Microsoft Foundry portal, create a Foundry resource that supports Azure OpenAI. Model and version availability varies by region and subscription, so select a region in which both a Realtime model and a Responses-capable model can be deployed.

After creating the resource, locate **Keys and Endpoint** or the equivalent page and record:

- **Endpoint**: the complete HTTPS URL entered as `Foundry endpoint` in setup;
- **Key**: required when using API-key authentication.

Use the endpoint exactly as shown by the portal. Examples include:

```text
https://my-resource.openai.azure.com
https://my-resource.services.ai.azure.com
```

### 3.2 Create two model deployments

Create the following deployments from the Foundry model catalog or deployment page.

| Purpose | Required capability | Default deployment name in this repository |
|---|---|---|
| Voice conversation | Realtime API | `gpt-realtime-2.1` |
| Web search | Responses API with web search | `gpt-5.6-terra` |

Setup requires the **deployment names**, not necessarily the model display names. If you choose different deployment names or newer compatible models, enter the names you assigned to those deployments.

### 3.3 Choose authentication

API-key authentication is the simplest option for initial setup.

- **API key**: enter one of the AI resource keys in setup.
- **Azure sign-in**: leave the API-key field empty, run `az login`, and grant the signed-in user an inference role such as Foundry User on the AI resource.

Never put API keys or access tokens in README files, source code, or Git-tracked files. The setup GUI stores the key in the ignored `relay/.env` file.

## 4. Connect Stack-chan over USB for the first time

1. Turn Stack-chan off.
2. Connect CoreS3 directly to the Windows PC with a data-capable USB Type-C cable.
3. Turn Stack-chan on.
4. Open Windows Device Manager.
5. Under **Ports (COM & LPT)**, confirm that `USB Serial Device (COMx)` appears.

If detection is unreliable through a USB hub, connect directly to the PC. If no COM port appears, replace charge-only cables and try another USB port.

## 5. Run setup

Double-click [setup.cmd](../../setup.cmd) in the repository root. If Windows displays a warning, verify the file location before running it.

### 5.1 Programs

On **1. Programs**, click the install button. Setup checks or installs:

- Python 3.11 or later;
- Relay Python packages;
- PlatformIO;
- Git, which is optional after the repository has already been cloned;
- Azure CLI, which is optional when using an API key.

Enable the headset-test option only when you also want to test the Relay with the PC microphone and headphones. Subsequent setup runs skip package installation while the project definition and installed dependencies remain unchanged.

### 5.2 Connection settings

On **2. Connection settings**, enter:

| Field | Value |
|---|---|
| Foundry endpoint | Complete endpoint shown by Azure/Foundry |
| Realtime deployment | Deployment name created for voice conversations |
| Responses deployment | Deployment name created for web search |
| API key | Resource key, or blank when using Azure sign-in |
| Web-search country | Usually `JP` for Japan |
| Time zone | `Asia/Tokyo` for Japan |
| Voice browser launch | Enable only if Stack-chan should open pages on this PC |
| Allowed browser domains | Comma-separated host allow-list |

An empty browser-domain list permits any HTTP(S) host. Start with only the required domains, such as `microsoft.com,github.com`.

### 5.3 Stack-chan settings

On **3. Stack-chan**, enter:

| Field | Value |
|---|---|
| Device ID | A device name, normally `stackchan-001` |
| Device token | Use the secure-token generation button |
| Wi-Fi SSID | SSID of the 2.4 GHz Wi-Fi used by Stack-chan; 5 GHz-only SSIDs are unsupported |
| Wi-Fi password | Password for that network |

The same device ID and token are saved for both the Relay and firmware. Do not share the device token.

## 6. Start the Manager and Relay

1. Click **Save and start Manager**.
2. Confirm that `http://127.0.0.1:8787` opens.
3. Click **Start Relay** in the Manager.
4. Confirm the status is running and managed.

Windows Defender Firewall may prompt on the first run. Permit access only on trusted **private networks**. Never forward Relay port 8080 to the internet.

Starting the Relay also starts the USB bridge. With compatible firmware, the Manager shows the CoreS3 COM port and device ID.

## 7. Flash the firmware

1. Keep CoreS3 connected over USB.
2. Click the firmware-flash button in setup.
3. Wait for the build and upload in the opened PowerShell window.
4. Confirm that `Stack-chan deployment completed.` appears.
5. Wait for CoreS3 to restart automatically.

The deployment process selects a LAN address for the Relay PC and writes it as the initial firmware endpoint. With multiple adapters or a VPN, specify the route explicitly:

```powershell
cd stackchan
.\deploy-from-relay.ps1 -InterfaceAlias "Wi-Fi"

# Or specify the Relay PC address and COM port
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10 -UploadPort COM3
```

## 8. Verify Stack-chan networking

After flashing, the display has four tabs:

- `STATUS`: connection state, device IP, and Relay endpoint;
- `FACE`: conversation and expressions;
- `NETWORK`: USB/Wi-Fi route and Relay endpoint;
- `SETTINGS`: volume and ESP-NOW remote settings.

The `NETWORK` tab provides three route modes:

- `AUTO`: prefer USB and use Wi-Fi when USB is unavailable; recommended;
- `WI-FI`: use Wi-Fi only;
- `USB`: use USB only.

To change the Wi-Fi Relay destination, tap `Relay endpoint`, select an IPv4 octet or port, adjust it with `-` / `+`, and tap `SAVE`. The value is persisted and the Wi-Fi connection is restarted. Tap `CANCEL` to discard changes.

A healthy Wi-Fi connection appears as a device WebSocket in the Relay log. A healthy USB connection appears as **Relay connected** in the USB bridge section.

## 9. Start a conversation

1. Open the `FACE` tab.
2. Tap the screen.
3. Speak when the state becomes `LISTENING`.
4. After playback, Stack-chan listens for the next turn.
5. Tap again to pause the conversation.

A single tap on top of CoreS3 also opens Face and starts listening. Two top taps within two seconds stop conversation and playback and move Stack-chan into its sleep posture.

## 10. Shut down

Click **Managerを終了** at the bottom of the Manager page. It stops the managed headset test, USB bridge, and Relay before shutting down the Manager itself.

Pause a conversation before turning Stack-chan off.

## 11. Troubleshooting

### No COM port

- Use a data-capable USB cable.
- Remove USB hubs and connect directly.
- Power-cycle CoreS3.
- Check for USB Serial Device in Device Manager.
- Stop the USB bridge or serial monitor if another process owns the COM port.

### Relay health check fails

- Start the Relay from the Manager.
- Check the status and logs at `http://127.0.0.1:8787`.
- With a VPN or multiple adapters, use `-InterfaceAlias` or `-RelayAddress`.
- Permit Python/Relay on trusted private networks in Windows Firewall.

### Azure returns 401 or 403

- Confirm that the endpoint belongs to the intended resource.
- Recheck the API key, or the account, tenant, and subscription selected by `az login`.
- Confirm that the signed-in user has an inference role on the AI resource.

### Model not found

Enter the **deployment name**, not the model name. Also confirm that Realtime and Responses deployment names have not been swapped.

### Stack-chan does not connect

- Confirm that the Relay address on `STATUS` is the Windows PC LAN address.
- Select `USB` or `WI-FI` temporarily to isolate the route.
- For Wi-Fi, confirm the SSID and password and select the 2.4 GHz SSID instead of a 5 GHz-only SSID.
- Reflash after changing the device ID or token.
- Check both Relay and USB bridge logs in the Manager.

## Ready-to-use checklist

- [ ] CoreS3 appears as a Windows COM port
- [ ] Foundry endpoint is available
- [ ] Realtime deployment is created
- [ ] Responses/web-search deployment is created
- [ ] API-key or Azure sign-in authentication is ready
- [ ] Dependencies and connection settings are saved in setup
- [ ] Relay is running and managed
- [ ] Firmware upload completed
- [ ] Device connection appears on Stack-chan or in Manager logs
- [ ] A conversation starts from `FACE`

For operational and development details, see the [README](../../README.en.md), [development guide](development.md), and [architecture](architecture.md).
