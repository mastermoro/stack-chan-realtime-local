# Browser Function crash — agent handoff

Last updated: 2026-08-29 16:12 JST

## Resolution

Resolved on 2026-08-29. Chromium crashed when it was launched as a child of the managed Relay
process. Calling `explorer.exe URL` from Relay avoided the crash but did not reach the interactive
Explorer process, even though both processes were in Windows Session 1. Its exit code `1` was also
observed for a successful handoff from an interactive PowerShell process, so that code alone cannot
prove that a page opened.

The Windows browser action now sends the already validated URL to a dedicated endpoint on the
loopback-only Local Manager. The Manager repeats the HTTP/HTTPS, credentials, and optional domain
allow-list checks, then runs `explorer.exe URL` from its interactive process context. The endpoint
accepts no command or executable arguments.

Verified after restarting both Manager and Relay:

- Direct Manager broker test opened `https://example.com/` in the existing Chrome profile without
   an application-error dialog.
- Three consecutive Function calls over Wi-Fi opened successfully without a dialog while the USB
   bridge was completely stopped.
- Stackchan was switched to USB mode and the bridge reported `relay_connected=true` on COM3.
- Three consecutive Function calls over USB opened successfully without a dialog.
- Focused automated tests: 13 passed. Full Relay suite: 69 passed. Ruff passed for all four
   changed Python files, and `git diff --check` passed.

The remaining sections preserve the original failure evidence and investigation history.

## Objective

Fix the Windows browser Function so that a URL requested by Stackchan opens in the
interactive user's default Chrome session without an application-error dialog. The fix must work
through both the Wi-Fi and USB Relay transports and must preserve the URL/domain security checks.

Do not reset or clean the worktree. The USB mode implementation and subsequent fixes are currently
uncommitted. The current Git `HEAD` is `a151dcd`.

## Current failure

The Function launches a Chromium process, reports success to the model, and then Windows displays:

```text
chrome.exe - Application Error
The exception unknown software exception (0x80000003) occurred in the application
at location 0x00007FF9023D9D60.
```

The Japanese dialog shown to the user has the same values. `0x80000003` is a breakpoint exception.
The failed process remains alive behind the modal error dialog, so `Popen.pid > 0` is not a valid
success check.

An earlier reproduction showed the same exception class in `msedge.exe`. This is not known to be
specific to Chrome itself; both installed browsers are Chromium-based.

Installed versions at the time of reproduction:

- Google Chrome: `151.0.7922.175`
- Microsoft Edge: `151.0.4129.107`

## Wi-Fi/USB isolation result

The failure reproduces over a confirmed Wi-Fi WebSocket path, so USB framing/audio forwarding is
not required to trigger it.

Current Wi-Fi test topology:

- Relay PC IPv4: `192.168.1.107`
- Stackchan IPv4: `192.168.1.105`
- Relay endpoint compiled into firmware: `ws://192.168.1.107:8080/v1/realtime`
- Relay log: `192.168.1.105:57615 - "WebSocket /v1/realtime" [accepted]`
- Firmware log: `Relay transport selected: Wi-Fi`, then `Relay WebSocket connected`
- USB bridge state during the Wi-Fi reproduction: `ready`, `relay_connected=false`

The most recent Wi-Fi reproduction was:

```text
2026-08-29 15:50:28,752 INFO app.tools.local_actions
browser launch requested executable=chrome.exe pid=7052
2026-08-29 15:50:28,752 INFO app.tools.local_actions
local browser opened host=tenki.jp
```

The user then observed the Chrome `0x80000003` dialog. PID 7052 was still `Responding=True`, with no
main window title, because the native exception dialog had not yet been dismissed.

Important remaining isolation test: although the device data path was Wi-Fi, the USB bridge process
was still running and probing COM3. Stop it completely with the Manager API and repeat once before
concluding that the USB bridge process cannot contribute:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/usb/stop
```

Restart it afterward if needed:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/usb/start
```

## Browser implementation history

The committed pre-USB implementation in `HEAD` is only:

```python
@staticmethod
def _open_browser(url: str) -> bool:
    return webbrowser.open(url, new=2, autoraise=True)
```

Relevant file: `relay/app/tools/local_actions.py`.

That implementation selected Edge in the current Windows environment and produced an Edge
application-error dialog. The user stated Chrome is the default browser, but this process could not
read:

```text
HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice
```

`HKCR\ChromeHTML\shell\open\command` was available and contained:

```text
"C:\Program Files\Google\Chrome\Application\chrome.exe" --single-argument %1
```

A local, uncommitted workaround then added `_windows_default_browser_executable()` and launched
Chrome directly. Two variants have been tried:

1. `chrome.exe --new-window URL` with `CREATE_NEW_PROCESS_GROUP`: Chrome crashed.
2. `chrome.exe --single-argument URL` without `CREATE_NEW_PROCESS_GROUP`: a direct command-line
   smoke test appeared to hand off successfully, but real Function calls still crash Chrome.

Actual Function reproductions after direct Chrome selection:

```text
15:41:24 host=weathernews.jp pid=28312  # USB path, Chrome error
15:44:45 host=www.google.com pid=10384  # USB path, Chrome error after variant 2
15:50:28 host=tenki.jp pid=7052         # confirmed Wi-Fi path, Chrome error
```

Therefore, do not treat the current direct-executable workaround as a fix. Compare against `HEAD`
before editing:

```powershell
git diff HEAD -- relay/app/tools/local_actions.py
git show HEAD:relay/app/tools/local_actions.py
```

## Process context

The Manager starts Relay with `subprocess.Popen()` from `relay/local_manager.py`. The Relay then
calls `_open_browser()` inside `asyncio.to_thread()`.

The virtual environment launcher produces two Python process entries per service on this machine:

- `relay/.venv/Scripts/python.exe`
- the underlying `C:/Users/user/.platformio/python3/python.exe`

There are currently three expected service pairs: Manager, Relay, and USB bridge. Do not assume
these pairs are duplicate Relay instances without checking parent/command-line information.

The Relay process redirects stdout/stderr to:

```text
%TEMP%\stackchan-relay\relay.log
```

USB bridge logging is at:

```text
%TEMP%\stackchan-relay\usb-bridge.log
```

The Relay launcher itself was not materially changed by the USB work; the USB work added a sibling
bridge process and Manager supervision. This makes process context, Chromium state/update, injected
modules, or an interaction with the additional bridge process more plausible than corruption of a
URL over the transport.

## Why Relay currently reports a false success

`LocalActionExecutor.execute()` considers the launch successful when `_open_browser()` returns
true. The current implementation returns true immediately when `subprocess.Popen()` provides a
positive PID. It does not wait for startup, inspect the return code, or detect a native exception
dialog. Relay logs therefore say `local browser opened` even when Windows immediately shows the
error.

## Recommended investigation order

1. Preserve the dirty worktree and confirm the current device is still on Wi-Fi.
2. Stop the USB bridge process completely and reproduce the Function once over Wi-Fi.
3. Dismiss the error dialog, then query Application Error / Windows Error Reporting events. Events
   were empty while the modal dialog was still active, so collect them after dismissal.
4. If permitted, collect a dump/module list for the failed Chrome PID. Chrome Crashpad paths were
   not readable in the normal workspace sandbox and may require approval.
5. Compare these launch contexts with the same URL:
   - an interactive PowerShell/Python process;
   - the managed Relay Function;
   - Windows `ShellExecute`/Explorer handoff;
   - a small interactive-user browser broker process, if needed.
6. Determine whether the failure follows the Relay parent/job/environment or merely coincides with
   Chromium 151. Direct execution from an approved interactive tool command did not immediately
   reproduce, while the real Relay Function consistently did.
7. Implement startup/error detection so the Function cannot return `opened` merely from a PID.

A dedicated browser broker is a reasonable fallback architecture if Chromium consistently crashes
only when spawned from Relay: start the broker in the interactive desktop context and send only
validated HTTP/HTTPS URLs to it over a narrowly scoped local IPC channel. Do not add arbitrary
command execution.

## Security constraints to preserve

- Only `http` and `https` URLs.
- Reject credentials embedded in URLs.
- Keep the optional allowed-domain enforcement.
- Do not execute a shell command constructed from a model-provided URL.
- Do not expose `.env`, `stackchan/include/secrets.hpp`, tokens, or Wi-Fi credentials in logs or
  handoff notes.

## Relevant files

- `relay/app/tools/local_actions.py` — browser Function and current workaround
- `relay/tests/test_local_actions.py` — Function validation/launcher tests
- `relay/app/session/orchestrator.py` — Function dispatch and result handling
- `relay/local_manager.py` — Relay and USB bridge process supervision
- `relay/app/usb/bridge.py` — sibling USB bridge process
- `relay/app/gateway/websocket.py` — transport-independent device WebSocket entry point
- `docs/usb-relay-mode-plan.md` — USB feature plan and implementation notes

## Tests and operational notes

The local `.env` enables the browser action, so the test that assumes the default disabled setting
needs an explicit environment override:

```powershell
cd relay
$env:LOCAL_BROWSER_TOOL_ENABLED = 'false'
.\.venv\Scripts\python.exe -m pytest tests/test_local_actions.py -q --basetemp=.pytest-tmp-browser
.\.venv\Scripts\python.exe -m ruff check app/tools/local_actions.py tests/test_local_actions.py
```

The current launcher tests pass, but they mock `Popen` and only prove argument construction; they do
not cover the native Windows failure.

After changing Relay code, restart Relay through the Manager so the new module is loaded:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/stop
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/start
```

## Acceptance criteria

- Three consecutive browser Function calls over Wi-Fi open the requested URL without a Chrome/Edge
  application-error dialog.
- Three consecutive calls over USB do the same.
- The Function uses the intended interactive Chrome profile/session.
- No orphaned crashed Chromium process remains.
- A launch failure is reported as a Function error instead of `status=opened`.
- Existing URL validation/domain restrictions and all relevant automated tests remain intact.
