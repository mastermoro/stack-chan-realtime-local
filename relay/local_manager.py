# ruff: noqa: E501
from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config.settings import Settings
from app.tools.local_actions import LocalActionError, LocalActionExecutor


class LocalManagerError(RuntimeError):
    """Raised when a local Relay process cannot be managed safely."""


class RelaySupervisor:
    def __init__(
        self,
        relay_root: Path,
        *,
        listen_address: str = "0.0.0.0",
        relay_port: int = 8080,
        log_directory: Path | None = None,
    ) -> None:
        self._relay_root = relay_root
        self._listen_address = listen_address
        self._relay_port = relay_port
        self._log_directory = log_directory or Path(tempfile.gettempdir()) / "stackchan-relay"
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: BinaryIO | None = None
        self._log_path: Path | None = None
        self._started_at: datetime | None = None
        self._last_exit_code: int | None = None
        self._lock = threading.Lock()

    @property
    def _python_path(self) -> Path:
        return self._relay_root / ".venv" / "Scripts" / "python.exe"

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            return self._status()

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is not None:
                return self._status()
            if not self._python_path.is_file():
                raise LocalManagerError("Relay virtual environment was not found.")
            if self._port_is_in_use():
                raise LocalManagerError(
                    f"Port {self._relay_port} is already in use. Stop that process before starting "
                    "the managed Relay."
                )

            self._log_directory.mkdir(parents=True, exist_ok=True)
            self._log_path = self._log_directory / "relay.log"
            self._log_handle = self._log_path.open("ab")
            started_at = datetime.now(UTC)
            self._log_handle.write(
                f"\n--- Relay start requested at {started_at.isoformat()} ---\n".encode()
            )
            self._log_handle.flush()
            self._process = subprocess.Popen(
                [
                    str(self._python_path),
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    self._listen_address,
                    "--port",
                    str(self._relay_port),
                    # ESP32 keeps the connection alive itself. Give its audio
                    # and display loop a generous margin to answer a protocol
                    # ping instead of dropping an otherwise healthy session.
                    "--ws-ping-interval",
                    "60",
                    "--ws-ping-timeout",
                    "30",
                ],
                cwd=self._relay_root,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
            self._started_at = started_at
            self._last_exit_code = None
            return self._status()

    def stop(self, *, reject_unmanaged: bool = False) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is None:
                if reject_unmanaged and self._relay_is_reachable():
                    raise LocalManagerError(
                        "Relay is running outside this manager and cannot be stopped safely here."
                    )
                return self._status()

            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._clean_up_finished_process()
            return self._status()

    def read_logs(self, lines: int) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._log_path is None or not self._log_path.is_file():
                return {"text": "No managed Relay log is available yet."}
            return {"text": self._tail(self._log_path, lines)}

    def _status(self) -> dict[str, Any]:
        managed = self._process is not None
        return {
            "running": managed,
            "managed": managed,
            "reachable": self._relay_is_reachable(),
            "pid": self._process.pid if self._process is not None else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_exit_code": self._last_exit_code,
            "relay_url": f"http://{self._listen_address}:{self._relay_port}",
        }

    def _clean_up_finished_process(self) -> None:
        if self._process is None:
            return
        return_code = self._process.poll()
        if return_code is None:
            return
        self._last_exit_code = return_code
        self._process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def _port_is_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.2)
            # 0.0.0.0 is a bind-only address, not a usable destination for
            # local probes.  Always check the locally exposed listener via
            # loopback instead.
            return connection.connect_ex(("127.0.0.1", self._relay_port)) == 0

    def _relay_is_reachable(self) -> bool:
        connection = HTTPConnection("127.0.0.1", self._relay_port, timeout=0.5)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            return response.status == 200
        except OSError:
            return False
        finally:
            connection.close()

    @staticmethod
    def _tail(path: Path, lines: int) -> str:
        with path.open("rb") as log_file:
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(size - 64 * 1024, 0))
            content = log_file.read().decode("utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])


class UsbBridgeSupervisor:
    def __init__(self, relay_root: Path, *, log_directory: Path | None = None) -> None:
        self._relay_root = relay_root
        self._log_directory = log_directory or Path(tempfile.gettempdir()) / "stackchan-relay"
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: BinaryIO | None = None
        self._log_path: Path | None = None
        self._status_path = self._log_directory / "usb-bridge-state.json"
        self._started_at: datetime | None = None
        self._last_exit_code: int | None = None
        self._lock = threading.Lock()

    @property
    def _python_path(self) -> Path:
        return self._relay_root / ".venv" / "Scripts" / "python.exe"

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            return self._status()

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is not None:
                return self._status()
            if not self._python_path.is_file():
                raise LocalManagerError("Relay virtual environment was not found.")

            self._log_directory.mkdir(parents=True, exist_ok=True)
            self._log_path = self._log_directory / "usb-bridge.log"
            self._status_path.unlink(missing_ok=True)
            self._log_handle = self._log_path.open("ab")
            started_at = datetime.now(UTC)
            self._log_handle.write(
                f"\n--- USB bridge start requested at {started_at.isoformat()} ---\n".encode()
            )
            self._log_handle.flush()
            self._process = subprocess.Popen(
                [
                    str(self._python_path),
                    "-m",
                    "app.usb.bridge",
                    "--status-file",
                    str(self._status_path),
                ],
                cwd=self._relay_root,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
            self._started_at = started_at
            self._last_exit_code = None
            return self._status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is None:
                return self._status()
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._clean_up_finished_process()
            return self._status()

    def read_logs(self, lines: int) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._log_path is None or not self._log_path.is_file():
                return {"text": "No managed USB bridge log is available yet."}
            return {"text": RelaySupervisor._tail(self._log_path, lines)}

    def _status(self) -> dict[str, Any]:
        child_status: dict[str, Any] | None = None
        if self._process is not None:
            try:
                payload = json.loads(self._status_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    child_status = payload
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        return {
            "running": self._process is not None,
            "pid": self._process.pid if self._process is not None else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_exit_code": self._last_exit_code,
            "bridge": child_status,
        }

    def _clean_up_finished_process(self) -> None:
        if self._process is None:
            return
        return_code = self._process.poll()
        if return_code is None:
            return
        self._last_exit_code = return_code
        self._process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class HeadsetSupervisor:
    def __init__(self, relay_root: Path, *, log_directory: Path | None = None) -> None:
        self._relay_root = relay_root
        self._log_directory = log_directory or Path(tempfile.gettempdir()) / "stackchan-relay"
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: BinaryIO | None = None
        self._log_path: Path | None = None
        self._status_path = self._log_directory / "headset-test-state.json"
        self._control_path = self._log_directory / "headset-test-control.json"
        self._started_at: datetime | None = None
        self._last_exit_code: int | None = None
        self._lock = threading.Lock()

    @property
    def _python_path(self) -> Path:
        return self._relay_root / ".venv" / "Scripts" / "python.exe"

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            return self._status()

    def start(self, *, input_device: int | None, output_device: int | None) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is not None:
                return self._status()
            if not self._python_path.is_file():
                raise LocalManagerError("Relay virtual environment was not found.")

            self._log_directory.mkdir(parents=True, exist_ok=True)
            self._log_path = self._log_directory / "headset-test.log"
            self._status_path.unlink(missing_ok=True)
            self._control_path.unlink(missing_ok=True)
            self._log_handle = self._log_path.open("ab")
            started_at = datetime.now(UTC)
            self._log_handle.write(
                f"\n--- Headset test start requested at {started_at.isoformat()} ---\n".encode()
            )
            self._log_handle.flush()
            command = [
                str(self._python_path),
                "headset_test.py",
                "--status-file",
                str(self._status_path),
                "--control-file",
                str(self._control_path),
            ]
            if input_device is not None:
                command.extend(["--input-device", str(input_device)])
            if output_device is not None:
                command.extend(["--output-device", str(output_device)])
            self._process = subprocess.Popen(
                command,
                cwd=self._relay_root,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
            self._started_at = started_at
            self._last_exit_code = None
            return self._status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is None:
                return self._status()
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._clean_up_finished_process()
            return self._status()

    def read_logs(self, lines: int) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._log_path is None or not self._log_path.is_file():
                return {"text": "No managed headset test log is available yet."}
            return {"text": RelaySupervisor._tail(self._log_path, lines)}

    def control_conversation(self, action: str) -> dict[str, Any]:
        with self._lock:
            self._clean_up_finished_process()
            if self._process is None:
                raise LocalManagerError("Start the headset test before controlling a conversation.")
            payload = {
                "action": action,
                "request_id": datetime.now(UTC).isoformat(),
            }
            try:
                # The headset process polls this file. Avoid atomic replacement on
                # Windows because its short-lived read can deny the rename.
                self._control_path.write_text(json.dumps(payload), encoding="utf-8")
            except OSError as exc:
                raise LocalManagerError("Could not send the conversation command.") from exc
            return self._status()

    def _status(self) -> dict[str, Any]:
        return {
            "running": self._process is not None,
            "pid": self._process.pid if self._process is not None else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_exit_code": self._last_exit_code,
            "conversation": self._read_conversation_status() if self._process is not None else None,
        }

    def _read_conversation_status(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def _clean_up_finished_process(self) -> None:
        if self._process is None:
            return
        return_code = self._process.poll()
        if return_code is None:
            return
        self._last_exit_code = return_code
        self._process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


RELAY_ROOT = Path(__file__).resolve().parent
supervisor = RelaySupervisor(RELAY_ROOT)
usb_bridge_supervisor = UsbBridgeSupervisor(RELAY_ROOT)
headset_supervisor = HeadsetSupervisor(RELAY_ROOT)


class HeadsetStartRequest(BaseModel):
    input_device: int | None = None
    output_device: int | None = None


class BrowserOpenRequest(BaseModel):
    url: str


class BrowserBroker:
    @staticmethod
    def open(url: str) -> bool:
        try:
            process = subprocess.Popen(["explorer.exe", url])
        except OSError:
            return False
        try:
            return process.wait(timeout=5) in {0, 1}
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return False


browser_broker = BrowserBroker()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        headset_supervisor.stop()
        usb_bridge_supervisor.stop()
        supervisor.stop()


app = FastAPI(title="Stack-chan Relay Local Manager", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stack-chan Relay Manager</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { max-width: 920px; margin: 36px auto; padding: 0 20px; background: #111827; color: #e5e7eb; }
    h1 { margin-bottom: 4px; } p { color: #a5b4c6; }
    section { margin-top: 24px; padding: 20px; border: 1px solid #334155; border-radius: 12px; background: #172033; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    button { border: 0; border-radius: 8px; padding: 10px 16px; font-weight: 700; cursor: pointer; }
    #start { background: #34d399; color: #052e25; } #stop { background: #fb7185; color: #500724; }
    #refresh { background: #93c5fd; color: #172554; } #state { font-weight: 700; }
    #headset-start, #conversation-start { background: #c4b5fd; color: #2e1065; }
    #headset-stop, #conversation-pause { background: #fb7185; color: #500724; }
    #usb-start { background: #67e8f9; color: #164e63; } #usb-stop { background: #fb7185; color: #500724; }
    select { min-width: 260px; padding: 8px; border-radius: 6px; }
    pre { min-height: 260px; max-height: 520px; overflow: auto; margin: 14px 0 0; padding: 14px; border-radius: 8px; background: #020617; color: #cbd5e1; white-space: pre-wrap; }
    .ok { color: #6ee7b7; } .idle { color: #fbbf24; } .error { color: #fda4af; }
  </style>
</head>
<body>
  <h1>Stack-chan Relay</h1>
  <p>ローカル Relay の開始・停止・状態・ログを管理します。管理 UI はこの PC からのみ操作できます。</p>
  <section>
    <div class="row"><span>状態:</span><span id="state">確認中...</span></div>
    <div class="row" style="margin-top: 16px">
      <button id="start">Relay を開始</button><button id="stop">Relay を停止</button>
      <button id="refresh">今すぐ更新</button>
    </div>
    <p id="details"></p><p id="message" class="error"></p>
  </section>
  <section><div class="row"><h2>Relay ログ</h2><button id="relay-log-copy">ログをコピー</button></div>
    <pre id="logs" tabindex="0">読み込み中...</pre></section>
  <section>
    <h2>USB ブリッジ</h2>
    <p>接続された Stack-chan を検出し、USB 通信をローカル Relay へ中継します。書き込み時は停止して COM ポートを解放してください。</p>
    <div class="row"><span>状態:</span><span id="usb-state">確認中...</span>
      <button id="usb-start">ブリッジを開始</button><button id="usb-stop">ブリッジを停止</button></div>
    <p id="usb-details"></p><p id="usb-message" class="error"></p>
  </section>
  <section><div class="row"><h2>USB ブリッジ ログ</h2><button id="usb-log-copy">ログをコピー</button></div>
    <pre id="usb-logs" tabindex="0">読み込み中...</pre></section>
  <section>
    <h2>ヘッドセット・テスト</h2>
    <p>Stack-chan と同じ半二重会話をテストします。テストの起動中はRelay接続と文脈を維持し、会話だけを開始・一時停止できます。</p>
    <div class="row"><label>入力 <select id="input-device"><option value="">既定のマイク</option></select></label>
      <label>出力 <select id="output-device"><option value="">既定のヘッドセット</option></select></label></div>
    <div class="row" style="margin-top: 16px"><span>状態:</span><span id="headset-state">確認中...</span>
      <button id="headset-start">テストを起動</button><button id="headset-stop">テストを停止</button>
      <button id="conversation-start">会話を開始</button><button id="conversation-pause">会話を一時停止</button></div>
    <p id="headset-details"></p><p id="headset-message" class="error"></p>
  </section>
  <section><div class="row"><h2>ヘッドセット・テスト ログ</h2><button id="headset-log-copy">ログをコピー</button></div>
    <pre id="headset-logs" tabindex="0">読み込み中...</pre></section>
  <script>
    const state = document.querySelector('#state');
    const details = document.querySelector('#details');
    const message = document.querySelector('#message');
    const logs = document.querySelector('#logs');
    const usbState = document.querySelector('#usb-state');
    const usbDetails = document.querySelector('#usb-details');
    const usbMessage = document.querySelector('#usb-message');
    const usbLogs = document.querySelector('#usb-logs');
    const headsetState = document.querySelector('#headset-state');
    const headsetDetails = document.querySelector('#headset-details');
    const headsetMessage = document.querySelector('#headset-message');
    const headsetLogs = document.querySelector('#headset-logs');
    const inputDevice = document.querySelector('#input-device');
    const outputDevice = document.querySelector('#output-device');
    async function request(path, options = {}) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '操作に失敗しました。');
      return data;
    }
    function selectionIsInside(element) {
      const selection = window.getSelection();
      return Boolean(selection && !selection.isCollapsed &&
        (element.contains(selection.anchorNode) || element.contains(selection.focusNode)));
    }
    function updateLog(element, text, force = false) {
      if (!force && (document.activeElement === element || selectionIsInside(element))) return;
      const stickToBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 24;
      element.textContent = text;
      if (stickToBottom) element.scrollTop = element.scrollHeight;
    }
    async function copyLog(element, target) {
      try {
        await navigator.clipboard.writeText(element.textContent);
        target.textContent = 'ログをコピーしました。';
      } catch (error) {
        target.textContent = `コピーできませんでした: ${error.message}`;
      }
    }
    async function refresh(forceLogs = false) {
      try {
        const [status, log] = await Promise.all([request('/api/status'), request('/api/logs?lines=250')]);
        const reachable = status.reachable;
        state.textContent = reachable
          ? (status.managed ? '稼働中（管理中）' : '稼働中（外部起動）')
          : (status.running ? '起動中' : '停止中');
        state.className = reachable ? 'ok' : 'idle';
        details.textContent = status.managed
          ? `PID ${status.pid} — ${status.relay_url} — 開始 ${status.started_at}`
          : reachable
            ? `${status.relay_url} — 管理UI外で起動されています。停止は起動元で行ってください。`
            : `最終終了コード: ${status.last_exit_code ?? 'なし'} — ${status.relay_url}`;
        updateLog(logs, log.text, forceLogs);
        message.textContent = '';
      } catch (error) { message.textContent = error.message; }
    }
    async function refreshHeadset(forceLogs = false) {
      try {
        const [status, log] = await Promise.all([request('/api/headset/status'), request('/api/headset/logs?lines=250')]);
        const conversation = status.conversation;
        const labels = {connecting: '接続中', ready: '待機中', listening: '聞き取り中', thinking: '応答準備中', searching: '検索中', speaking: '再生中'};
        headsetState.textContent = status.running ? (labels[conversation?.state] || '起動中') : '停止中';
        headsetState.className = status.running ? 'ok' : 'idle';
        headsetDetails.textContent = status.running
          ? `PID ${status.pid} — ${conversation?.capture_enabled ? 'マイク入力中' : 'マイク停止中'} — 開始 ${status.started_at}`
          : `最終終了コード: ${status.last_exit_code ?? 'なし'}`;
        updateLog(headsetLogs, log.text, forceLogs);
        headsetMessage.textContent = '';
      } catch (error) { headsetMessage.textContent = error.message; }
    }
    async function refreshUsb(forceLogs = false) {
      try {
        const [status, log] = await Promise.all([request('/api/usb/status'), request('/api/usb/logs?lines=250')]);
        const bridge = status.bridge;
        const labels = {scanning: 'Stack-chan を検索中', connected: 'USB 接続中', relay_connecting: 'Relay 接続中', relay_connected: 'Relay 中継中'};
        usbState.textContent = status.running ? (labels[bridge?.state] || '起動中') : '停止中';
        usbState.className = bridge?.state === 'relay_connected' ? 'ok' : (status.running ? 'idle' : 'idle');
        usbDetails.textContent = status.running
          ? `PID ${status.pid} — ${bridge?.port || 'COM 検出待ち'} — ${bridge?.device_id || '端末待ち'} — 開始 ${status.started_at}`
          : `最終終了コード: ${status.last_exit_code ?? 'なし'}`;
        updateLog(usbLogs, log.text, forceLogs);
        usbMessage.textContent = bridge?.last_error || '';
      } catch (error) { usbMessage.textContent = error.message; }
    }
    function deviceValue(select) { return select.value === '' ? null : Number(select.value); }
    function addDevices(select, devices) {
      for (const device of devices) {
        const option = document.createElement('option'); option.value = device.id; option.textContent = device.label;
        select.append(option);
      }
    }
    async function loadAudioDevices() {
      try {
        const devices = await request('/api/audio-devices');
        addDevices(inputDevice, devices.inputs); addDevices(outputDevice, devices.outputs);
      } catch (error) { headsetMessage.textContent = error.message; }
    }
    document.querySelector('#start').onclick = async () => { try { await request('/api/start', {method: 'POST'}); } catch (error) { message.textContent = error.message; } finally { refresh(); } };
    document.querySelector('#stop').onclick = async () => { try { await request('/api/stop', {method: 'POST'}); } catch (error) { message.textContent = error.message; } finally { refresh(); } };
    document.querySelector('#refresh').onclick = () => refresh(true);
    document.querySelector('#relay-log-copy').onclick = () => copyLog(logs, message);
    document.querySelector('#usb-log-copy').onclick = () => copyLog(usbLogs, usbMessage);
    document.querySelector('#headset-log-copy').onclick = () => copyLog(headsetLogs, headsetMessage);
    document.querySelector('#usb-start').onclick = async () => {
      try { await request('/api/usb/start', {method: 'POST'}); }
      catch (error) { usbMessage.textContent = error.message; } finally { refreshUsb(); }
    };
    document.querySelector('#usb-stop').onclick = async () => {
      try { await request('/api/usb/stop', {method: 'POST'}); }
      catch (error) { usbMessage.textContent = error.message; } finally { refreshUsb(); }
    };
    document.querySelector('#headset-start').onclick = async () => {
      try {
        await request('/api/headset/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({input_device: deviceValue(inputDevice), output_device: deviceValue(outputDevice)})});
      } catch (error) { headsetMessage.textContent = error.message; } finally { refreshHeadset(); }
    };
    document.querySelector('#headset-stop').onclick = async () => {
      try { await request('/api/headset/stop', {method: 'POST'}); }
      catch (error) { headsetMessage.textContent = error.message; } finally { refreshHeadset(); }
    };
    document.querySelector('#conversation-start').onclick = async () => {
      try { await request('/api/headset/conversation/start', {method: 'POST'}); }
      catch (error) { headsetMessage.textContent = error.message; } finally { refreshHeadset(); }
    };
    document.querySelector('#conversation-pause').onclick = async () => {
      try { await request('/api/headset/conversation/pause', {method: 'POST'}); }
      catch (error) { headsetMessage.textContent = error.message; } finally { refreshHeadset(); }
    };
    refresh(); refreshUsb(); refreshHeadset(); loadAudioDevices();
    setInterval(() => { refresh(); refreshUsb(); refreshHeadset(); }, 2000);
  </script>
</body>
</html>"""


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return supervisor.status()


@app.post("/api/start")
async def start() -> dict[str, Any]:
    try:
        relay_status = supervisor.start()
        usb_bridge_supervisor.start()
        return relay_status
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/stop")
async def stop() -> dict[str, Any]:
    try:
        usb_bridge_supervisor.stop()
        return supervisor.stop(reject_unmanaged=True)
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/logs")
async def logs(lines: int = Query(default=250, ge=1, le=1000)) -> dict[str, Any]:
    return supervisor.read_logs(lines)


@app.post("/api/browser/open")
async def open_browser(request: BrowserOpenRequest) -> dict[str, Any]:
    settings = Settings(local_browser_tool_enabled=True)
    executor = LocalActionExecutor(settings, browser_launcher=browser_broker.open)
    try:
        return await executor.execute("open_browser_url", {"url": request.url})
    except LocalActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/usb/status")
async def usb_status() -> dict[str, Any]:
    return usb_bridge_supervisor.status()


@app.post("/api/usb/start")
async def start_usb_bridge() -> dict[str, Any]:
    if not supervisor.status()["reachable"]:
        raise HTTPException(status_code=409, detail="Start the Relay before the USB bridge.")
    try:
        return usb_bridge_supervisor.start()
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/usb/stop")
async def stop_usb_bridge() -> dict[str, Any]:
    return usb_bridge_supervisor.stop()


@app.get("/api/usb/logs")
async def usb_logs(lines: int = Query(default=250, ge=1, le=1000)) -> dict[str, Any]:
    return usb_bridge_supervisor.read_logs(lines)


@app.get("/api/audio-devices")
async def audio_devices() -> dict[str, Any]:
    try:
        import sounddevice as sd

        default_input, default_output = sd.default.device
        devices = sd.query_devices()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Audio device discovery failed: {exc}") from exc

    def available_devices(channel_key: str) -> list[dict[str, Any]]:
        return [
            {"id": index, "label": f"{index}: {device['name']}"}
            for index, device in enumerate(devices)
            if device[channel_key] > 0
        ]

    return {
        "inputs": available_devices("max_input_channels"),
        "outputs": available_devices("max_output_channels"),
        "default_input": default_input if default_input >= 0 else None,
        "default_output": default_output if default_output >= 0 else None,
    }


@app.get("/api/headset/status")
async def headset_status() -> dict[str, Any]:
    return headset_supervisor.status()


@app.post("/api/headset/start")
async def start_headset_test(request: HeadsetStartRequest) -> dict[str, Any]:
    if not supervisor.status()["reachable"]:
        raise HTTPException(status_code=409, detail="Start the managed Relay before the headset test.")
    settings = Settings()
    if not settings.foundry_configured:
        raise HTTPException(status_code=409, detail="Foundry is not configured in relay/.env.")
    if not settings.device_tokens_json:
        raise HTTPException(status_code=409, detail="DEVICE_TOKENS_JSON is not configured in relay/.env.")
    try:
        return headset_supervisor.start(
            input_device=request.input_device,
            output_device=request.output_device,
        )
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/headset/stop")
async def stop_headset_test() -> dict[str, Any]:
    return headset_supervisor.stop()


@app.post("/api/headset/conversation/start")
async def start_headset_conversation() -> dict[str, Any]:
    try:
        return headset_supervisor.control_conversation("start")
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/headset/conversation/pause")
async def pause_headset_conversation() -> dict[str, Any]:
    try:
        return headset_supervisor.control_conversation("pause")
    except LocalManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/headset/logs")
async def headset_logs(lines: int = Query(default=250, ge=1, le=1000)) -> dict[str, Any]:
    return headset_supervisor.read_logs(lines)
