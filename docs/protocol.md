**日本語** | [English](en/protocol.md)

# Stack-chan / Relay プロトコル v1

このプロトコルは、Wi-Fi WebSocket または後述する USB ブリッジエンベロープを介して、JSON 制御メッセージとバイナリ PCM 音声を転送する。`ws://` は信頼済みのローカル LAN でのみ使用し、インターネットに公開する Relay では必ずセキュア WebSocket（`wss://`）を使用する。

## 認証ヘッダー

```http
Authorization: Bearer <DEVICE_TOKEN>
X-Device-Id: stackchan-001
```

## 音声

- バイナリメッセージ（WebSocket バイナリフレームまたは USB `*_BINARY` ペイロード）
- PCM16 リトルエンディアン
- 24,000 Hz
- モノラル
- 推奨デバイスフレーム: 20 ms / 480 サンプル / 960 バイト
- Relay からデバイスへの出力は、最大 8 KiB のバイナリメッセージに分割する。これは ESP WebSocket クライアントと USB エンベロープの両方の上限内に収まる。メッセージ境界は音声の境界ではないため、受信順にメッセージを再生する。

## デバイス -> Relay JSON

### hello

接続後、最初のテキストフレームとして送信しなければならない。

```json
{
  "type": "hello",
  "protocol": 1,
  "device_id": "stackchan-001",
  "audio": {
    "format": "pcm16",
    "sample_rate": 24000,
    "channels": 1
  }
}
```

### UI / ターン制御

```json
{"type":"audio.start"}
{"type":"audio.stop"}
{"type":"response.cancel"}
{"type":"conversation.pause","item_id":"item_123","content_index":0,"audio_end_ms":1500}
{"type":"ui.mode","mode":"face"}
{"type":"ping"}
```

v0.1 では Server VAD を正とする。Relay が音声フレームを受け付けるのは、デバイスの状態が `listening` の間だけである。Relay が思考中、検索中、または発話中の間、クライアントはマイクを停止しなければならない。`audio.start` / `audio.stop` はクライアントの会話状態を制御し、将来の手動 VAD モードのために維持する。`conversation.pause` は現在のターンを停止する一方、Realtime セッションと会話コンテキストを保持し、後続の `audio.start` で再利用できるようにする。

クライアントが出力の再生位置を把握している場合は、`item_id`、`content_index`、`audio_end_ms` を含めることが望ましい。Relay は進行中の応答をキャンセルし、まだ再生されていない音声を会話履歴から切り詰める。単純な組み込みクライアントでは、これら 3 つのフィールドは省略できる。

`ui.mode` には `standard` または `face` を指定できる。Relay は Realtime のシステム指示を更新し、会話コンテキストをリセットせずに、Face モードで猫らしい話し方を使用する。

## Relay -> デバイス JSON

```json
{"type":"session.ready","device_id":"stackchan-001"}
{"type":"session.reconnected","device_id":"stackchan-001"}
{"type":"state","state":"searching"}
{"type":"response.done"}
{"type":"output_audio.started","item_id":"item_123","content_index":0}
{"type":"emotion","emotion":"happy"}
{"type":"notice","title":"LOCAL ACTION","detail":"Browser opened on PC"}
{"type":"pong"}
```

`emotion` は UI に対する参考情報である。対応する値は `neutral`、`happy`、`sad`、`angry`、`surprised`、`sleepy` であり、クライアントは未知の値を無視しなければならない。

Web Search を使用した場合:

```json
{
  "type": "sources",
  "sources": [
    {"title":"Microsoft Learn","url":"https://learn.microsoft.com/..."}
  ]
}
```

## USB ブリッジエンベロープ

USB モードでは、同じ JSON テキストおよび PCM バイナリメッセージを PC ブリッジ経由で転送する。各シリアルパケットは COBS エンコードされ、前後をゼロデリミタで囲む。先頭のデリミタにより、フレームより前に出力された起動ログやフレームワークのログバイトを破棄する。デコード後のリトルエンディアンレイアウトは次のとおりである。

```text
uint8  envelope_version = 1
uint8  frame_type
uint16 sequence
uint32 payload_length
byte   payload[payload_length]   // maximum 8192 bytes
uint32 crc32                     // header + payload
```

フレームタイプは `HOST_PROBE=0x01`、`DEVICE_STATUS=0x02`、`DEVICE_OPEN=0x03`、`HOST_OPEN_ACK=0x04`、`DEVICE_CLOSE=0x05`、`HOST_CLOSE=0x06`、`DEVICE_TEXT=0x10`、`DEVICE_BINARY=0x11`、`HOST_TEXT=0x20`、`HOST_BINARY=0x21`、`DEVICE_LOG=0x30`、`ERROR=0x7f` である。

ホストは 500 ms ごとに `HOST_PROBE` を送信する。USB が選択中の Relay ルートかどうかにかかわらず、デバイスは `{"protocol":1,"device_id":"..."}` を含む `DEVICE_STATUS` で応答する。USB をルートとして開くには、デバイスがデバイス ID とトークンを含む `DEVICE_OPEN` を送信する。続いてブリッジが認証済みのローカル WebSocket を開き、`HOST_OPEN_ACK` を返す。それ以降、`*_TEXT` と `*_BINARY` は WebSocket のメッセージ種別を維持する。シーケンス欠落と CRC エラーは診断カウンターに記録し、破損したパケットはストリームを終了せずに破棄する。

## 再生割り込みのフェンス（issue #12）

新しいファームウェアは `conversation.pause` / `response.cancel` に、端末で増加させる
`interrupt_id`（1〜4294967295 の整数）を付ける。端末は送信より先に再生を停止し、
後続の応答状態・PCM・完了通知を遮断する。Face ではマイクを直ちに再開できる。

```json
{"type":"conversation.pause","interrupt_id":42}
{"type":"conversation.paused","interrupt_id":42}
```

Relay は割り込み前の送信処理が終了した後、同じ順序付きストリームで
`conversation.paused` を返す。その応答の古い PCM / 状態をフェンス後に送らない。
USB ブリッジも割り込みを受信すると PCM のペーシング待ちを解除し、フェンスまでの
滞留データを破棄する。端末とブリッジは最新 ID と一致した確認だけで遮断を解除する。
古い確認、`speaking`、経過時間、滞留した `session.reconnected` は解除条件ではない。
実際にトランスポートを張り直した場合はローカルフェンスをリセットする。

ID なしの既存クライアントも使用できる。新ファームウェアは対応 Relay と一緒に更新すること。
古い Relay は確認を返さないため、安全側に倒して出力を遮断したままになる。
通常は Relay / USB ブリッジを先に更新し、その後ファームウェアを更新する。

上流のキャンセルエラーに client event_id がなく、キャンセルが保留中の場合は、古い
応答との識別ができないため Foundry セッションを再接続する。この例外的な復旧では
会話コンテキストがリセットされるが、出力の誤再開や永続的なミュートを避ける。
