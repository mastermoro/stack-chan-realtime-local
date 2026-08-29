# 実装・運用ナレッジ（ローカル Relay / Stack-chan）

[日本語](implementation-lessons.md) | [English](en/implementation-lessons.md)

この文書は、ローカル Relay、Foundry Realtime、ヘッドセット試験、Stack-chan
ファームウェアを統合する過程で発生した事象を時系列で記録する。各項目は、症状だけ
でなく、原因、採った対処、今後守るべき設計上の判断を残すことを目的とする。

## 前提

- 端末と Relay の間は PCM16 / 24 kHz / mono のバイナリ音声を使う。Wi-Fi経路では
  WebSocket Binary Frame、USB経路ではCOBS/CRC付きUSB封筒で同じJSON・PCMを運ぶ。
- Foundry Realtime は `gpt-realtime-2.1`、Web 検索は Responses API の
  `gpt-5.6-terra` を使う。
- Stack-chan はローカルLAN内の Relay を想定する。既定は `ws://` であり、TLS は
  インターネット公開時だけ有効にする。
- 音響エコーキャンセル（AEC）がないため、現行の標準動作は半二重である。

## 時系列

### 1. ローカル起動・管理経路を用意した

Relay の開始・停止・状態・ログ閲覧を管理UIで扱えるようにし、ヘッドセット試験を
同じUIから起動できるようにした。管理UIはRelayのプロセスとヘッドセット試験プロセス
を別々に監督する。

**知見**

- 状態取得は「ポートが開いている」だけでなく `/healthz` の応答で判定する。
- 管理UIを止めるときは、管理下のRelayとヘッドセット試験も停止する。
- Windowsでは短い間隔で読まれる状態・制御ファイルを原子的リネームすると失敗する
  場合がある。状態・制御の書き込みはベストエフォートにし、失敗で音声セッションを
  終了させない。

### 2. ヘッドセット試験に会話の開始・一時停止を追加した

「テスト開始/停止」だけではFoundryセッションの文脈まで失われるため、テストプロセス
は維持したまま会話だけ開始・一時停止できるようにした。

**仕様変更**

- `audio.start`: 新しい聞き取りターンを開始する。
- `conversation.pause`: 現在ターンを止めるが、接続が生きている限りセッションを維持する。
- 管理UIの一時停止はプロセス停止ではない。

### 3. 発話判定が早すぎた

短い自然な間で聞き取りが完了し、回答を始める問題が起きた。

**原因**

サーバーVADの終話判定が会話用途には早すぎた。

**対処と現行仕様**

- 標準を `semantic_vad` / `low` eagerness にした。
- `server_vad` はデプロイメント互換性用のフォールバックとして設定可能に残した。
- `interrupt_response` は `false` のままにした。AECがないStack-chanで全二重の
  自動割り込みを有効にすると、自端末の再生音をユーザー発話として誤検出するためである。

### 4. 入力音声が遅延して古い発話が送られた

`audio input queue full; dropping frame` が出て、話し終えた後の古い音声が遅れて
Foundryへ届く可能性があった。

**原因**

- PortAudioコールバックからイベントループへ無制限に処理を積めた。
- Relayが端末WebSocketを読む処理の中で、Foundryへの `input_audio_buffer.append` を
  直接待っていた。

**対処と現行仕様**

- マイク側は20 msフレームを最大3フレームだけ保持し、混雑時は最古のフレームを捨てる。
- PortAudioコールバックは保留中の最新フレーム1つに集約する。
- Relay側も最大5フレームの入力キューを持ち、端末WebSocketの受信とFoundry送信を
  別タスクに分離した。

**設計判断**

入力音声は「全量保存」より鮮度を優先する。古いマイク音声は会話としての価値が低く、
残すほど次ターンを壊す。

### 5. 出力音声をキュー満杯時に捨てていた

`audio output queue full` により、アシスタントの回答の一部が欠落した。

**対処と現行仕様**

- 通常の回答中は出力キューを無制限にし、出力フレームを捨てない。
- バックログが大きい場合は警告だけを出す。
- 再生完了を待ってから次の聞き取りへ移る。

**例外**

ユーザーが明示的に一時停止・中断したときだけ、未再生出力を破棄する。これは欠落では
なく、明示的な停止要求である。

### 6. 一時停止しても再生が続いた

Relayへ `response.cancel` を送っていても、端末がすでに受信済みのPCMを再生キューに
保持していたため、音声が最後まで鳴り続けた。

**原因**

WebSocket方式ではサーバーが端末の実再生位置を知らない。出力停止と会話履歴の切り詰め
はクライアント側の責務である。

**対処と仕様変更**

- ヘッドセット試験は一時停止時に未再生キューを即時に空にする。
- Relayへ `conversation.pause` と、再生済み位置
  (`item_id`, `content_index`, `audio_end_ms`) を送る。
- RelayはFoundryへ `response.cancel` の後に `conversation.item.truncate` を送る。
- Relayは `output_audio.started` を端末へ送り、ヘッドセット試験がどの出力項目をどこまで
  再生したか追跡できるようにした。
- Stack-chanはタップ時にまず `M5.Speaker.stop()` を実行し、競合して到着したPCMを
  再生しない。

### 7. 一時停止後に勝手に listening へ戻った

一時停止と「応答完了後に自動で聞き取りへ戻す」処理が競合し、古い非同期処理が
`audio.start` を送っていた。表示は listening でも、会話の内部状態は停止済みという
矛盾が発生した。

**対処**

- ヘッドセット試験に会話リビジョンを導入した。
- 自動再開は待機前に記録したリビジョンと、待機後のリビジョンが一致するときだけ実行する。
- listening 状態だけではマイクを有効にせず、`conversation_active` も必要条件とした。

**運用上の意味**

一時停止後は待機状態に留まる。再開は「会話を開始」操作で行う。

### 8. キャンセル済み応答のエラーを障害として表示した

停止直後に `Cancellation failed: no active response found` が発生した。

**原因**

応答がキャンセル要求より先に正常終了していた。これは停止と応答完了の競合である。

**対処**

キャンセル待ち状態でこのエラーを受けた場合は、想定内の競合として扱い、端末へ
`REALTIME_ERROR` を通知しない。

### 9. Web検索が20秒で失敗した

Web検索中に `asyncio.wait_for(..., 20)` が発火し、検索自体が完了する前に
`TimeoutError` となった。

**原因**

- Responses APIのWeb検索は取得・グラウンディングを行うため、20秒を超えることがある。
- `asyncio.to_thread()` で動かした同期HTTP処理は `wait_for` をキャンセルしても実際には
  バックグラウンドスレッドに残る。

**対処と現行仕様**

- Relay側の固定20秒タイムアウトを撤廃した。
- OpenAIクライアントのHTTPタイムアウトを一元的に使用する。
- `WEB_SEARCH_TIMEOUT_SECONDS` を追加し、既定90秒、設定可能範囲5〜300秒とした。
- 実際の `gpt-5.6-terra` 検索で、約10秒・ソース1件の完了を確認した。

### 10. Foundry再接続後、listeningでも応答が返らなかった

Foundry接続がリセットされた後、端末は listening へ戻るが、次の発話に対する出力が
破棄された。

**原因**

前のFoundry接続で設定した「キャンセル中/出力抑止中」フラグが、新しいWebSocket接続にも
残った。新しい応答を古いキャンセル済み応答と誤認していた。

**対処**

- Foundry接続ごとに出力抑止、キャンセル待ち、出力項目追跡を初期化する。
- 再接続を偽の `response.done` として送るのをやめ、`session.reconnected` を追加した。
- ヘッドセット試験とStack-chanは、会話が有効な場合だけこのイベント後の `ready` で
  聞き取りを再開する。

### 11. Stack-chan実装をRelayの現行仕様へ揃えた

**修正内容**

- 聞き取り中のタップも `audio.stop` ではなく `conversation.pause` を送る。
- thinking / searching / speaking 中のタップも同じ一時停止経路を通す。
- `session.reconnected` を通常の応答完了とは別のハンドラで処理する。
- `RELAY_USE_TLS` を追加した。サンプル設定は `RELAY_PORT=8080`、
  `RELAY_USE_TLS=0` とする。
- ArduinoJson 7の非推奨 `StaticJsonDocument` を `JsonDocument` へ置き換えた。
  これにより検索ソースを含む長いJSONで固定512バイトを超えた場合の取りこぼしも避ける。

### 12. Wi-Fi再接続が「接続中」のままに見えた

Wi-Fi接続に失敗した際、画面が接続試行中の表示を繰り返し、利用者には失敗理由も
再試行状況も分かりにくかった。

**原因と対処**

- Wi-Fi接続試行の上限と失敗後の表示遷移が明確でなかった。
- Wi-Fiスリープが接続の安定性に影響しうるため、無効化した。
- 再試行回数を制限し、上限到達時は `disconnected` として明示するようにした。
- Status画面にWi-FiとRelayの接続状態、直近の簡潔なエラーを表示した。

**設計判断**

接続復旧は自動化しても、永続的な「接続中」表示で失敗を隠さない。自動再試行と
観測可能な失敗状態を両立させる。

### 13. 実機の音声再生でノイズ・欠落・早送りが起きた

通信自体は成立していても、音声がノイズ化したり、再生の途中で切れたり、キューが
一気に消費されたように聞こえる事象があった。

**原因**

- ESP側WebSocketクライアントの受信サイズ制約を超える音声フレームが届きうる。
- 非同期スピーカーが再生中のバッファを、受信処理が先に再利用・上書きしうる。
- ネットワーク受信とスピーカー再生の進行速度が異なる。

**対処と現行仕様**

- Relayから端末へのPCMは最大8 KiBのbinary frameに分割する。
- CoreS3側は複数の再生バッファを使い、再生中の領域を上書きしない。
- 受信したframeはメッセージ単位ではなく、到着順の連続PCMとして扱う。

**設計判断**

音声データの転送単位と再生単位は分ける。WebSocket frame境界を音声の意味的境界と
解釈してはいけない。

### 14. 表情・診断UIを同じ画面で更新するとちらついた

リップシンク中の顔に、タブや診断情報を常時重ねて描くと、表情と情報表示がちらついて
見えた。

**仕様変更**

- 画面を Status、Face、Settings に分けた。
- Faceではタブと診断表示を隠し、表情とリップシンクを優先する。
- 横スワイプで画面を切り替え、Settingsでは端末側の音量を調整する。
- Relayの `emotion` イベントを使い、`neutral`、`happy`、`sad`、`angry`、
  `surprised`、`sleepy` を描画する。
- Web検索中は過度に速い目の動きではなく、穏やかに調べ物をするアニメーションにした。

**設計判断**

毎フレーム更新するアバターと、低頻度更新の情報UIは描画面を分離する。技術的に
状態を表せても、かわいさ・安心感・視認性は実機で確認して調整する。

### 15. モデル名・設定値が実装と文書でずれる危険があった

利用モデルが変更された後、既定値や手順書に古いモデル名が残る可能性があった。

**対処**

- 音声対話モデルを `gpt-realtime-2.1`、Responses APIの検索モデルを
  `gpt-5.6-terra` に統一した。
- アプリ設定、`.env.example`、Bicep、パラメータ例、テスト、README、運用文書を
  横断検索して更新した。

**設計判断**

モデル変更はアプリコードだけの変更ではない。デプロイ設定と利用者向けの手順を含めて
一つの設定面として更新・検査する。

### 16. GitHub公開前に除外規則と生成物を見直した

公開前の確認で、資格情報のテンプレートと実値、ビルド生成物、ローカル用の証明書を
明確に区別する必要があった。

**対処**

- `.env.*` と `secrets*.hpp` を除外し、サンプルファイルだけを明示的に許可した。
- 秘密鍵・証明書形式（`.pem`、`.key`、`.p12`、`.pfx`）を除外した。
- Bicepビルドで生成される `infra/main.json` を除外した。
- 追跡対象と未追跡の公開候補を対象に、既知形式のAPIキー、GitHubトークン、Azure接続
  文字列、秘密鍵がないことを確認した。
- FirmwareビルドとBicepビルドを再実行し、CI設定がRelay、Firmware、Bicepを個別に
  検証することを確認した。

**運用上の注意**

`.env.example`、`secrets.example.hpp`、`infra/main.bicepparam` は公開テンプレートであり、
実値を書かない。公開RelayはWSSとデバイストークン認証を使い、資格情報や端末が露出した
可能性があればデバイストークンをローテーションする。

### 17. 再生終了後、約30秒で端末WebSocketが切断された

音声の再生が終わった後に、画面上は待機状態へ戻るものの Relay との接続が切れ、数秒後に
自動再接続する事象があった。RelayやFoundryのプロセスは動作を継続していたため、
`response.done` がセッションを終わらせているように見えた。

**ログで確認した事実**

- Relayは `response.done` 後も端末WebSocketを維持する実装であり、切断命令は出していない。
- Relay側では `device_to_realtime` タスクが最初に完了し、close code は `1005`
  （close frameにコードなし）だった。
- CoreS3側は `Connection lost` を記録してから自動再接続していた。
- Wi-Fiの切断状態遷移や Foundry セッションの終了は同時には観測されなかった。

**原因の見立て**

再生終了後はPCMの送受信が止まり、ESP32とRelayのWebSocket経路がアイドルになる。
この経路では、短いWebSocket生存確認の応答遅延、またはTCPのアイドル接続回収が起きうる。
アプリ側の `response.done` ではなく、アイドル中のトランスポート接続が切断点だった。

**対処と現行仕様**

- CoreS3は10秒ごとにアプリケーション制御メッセージ `ping` をRelayへ送る。
- 同時にWebSocketプロトコルの Ping/Pong heartbeat を10秒間隔で有効化する。
  Heartbeatのタイムアウト自体は切断条件にせず、TCP切断時の既存再接続に委ねる。
- ローカルRelay（uvicorn）のWebSocket pingを60秒間隔、timeoutを30秒へ緩和する。
- 実機ログには `Relay keepalive sent`、切断時の理由長、`response.done` を出力する。
- Relayは `response.done`、接続タスク終了名、端末close codeを記録する。

**確認方法**

1. 会話を行い、音声再生が終わった後に40秒以上待つ。
2. `Relay keepalive sent` が継続し、`Relay WebSocket disconnected` が出ないことを確認する。
3. 再発時はRelayログで `connection task completed` と `device disconnected` を確認する。
   `tasks=device_to_realtime` / `code=1005` なら、RelayやFoundryの応答終了ではなく
   端末側トランスポート切断として扱う。

**運用上の注意**

ファームウェアと `relay/local_manager.py` の両方を更新する。Relay設定の変更は、
Relay Managerを再起動しただけでは有効にならない。管理UIからRelay本体を停止・開始するか、
`run-manager.ps1 -Restart` の後にRelay本体を開始する。

### 18. K151の首サーボとESP-NOW MiniJoyCリモコンを統合した

M5Stack K151は自作Stack-chanでよく使われるPWMサーボ構成ではなく、CoreS3と
フィードバック付きシリアルサーボを使う完成品StackChanである。最初にI2C接続の
MiniJoyCとして扱うと、公式のワイヤレスコントローラとは通信できない。

**ハードウェア前提**

- 首サーボはSCS0009で、CoreS3のGPIO 6（Servo_TX）/ GPIO 7（Servo_RX）を使う
  シリアルサーボバスに接続される。
- 水平（X/yaw）はサーボID 1の連続回転軸、垂直（Y/pitch）はサーボID 2の可動軸である。
- PCA9685などのI2C PWMサーボドライバは使わない。GPIO 11/12もサーボ制御用ではない。
- 垂直軸の安全範囲は5〜85度（内部表現では50〜850）に制限する。

**実装**

- `StackChan-BSP` の `M5StackChan.Motion` に首の実移動を委譲する。顔の表情変更で
  リモコン操作を代用しない。
- 公式のESP-NOWワイヤレスコントローラは、20バイトのEspressif ESPNOWヘッダに続いて
  8バイトの制御ペイロードを送る。先頭は対象ID、続くlittle-endian値は yaw、pitch、
  speed、最後はボタン/レーザー状態である。
- 受信側は対象IDが `0`（ブロードキャスト）または設定したReceiver IDの場合だけ受け付け、
  yawを-1280〜1280、pitchを50〜850へクランプする。
- FACE画面でのみ最新の受信値をMotionへ反映する。設定画面などで意図せず首を動かさない。

**ESP-NOWとWi-Fiの注意**

ESP-NOWはWi-Fiと同じ無線・チャネルを共有する。Relay用Wi-Fiへ接続した後にESP-NOWを
初期化し、そのWi-Fiチャネルを使う。チャネル1へ強制固定すると、RelayのAPが別チャネルの
場合にRelay通信を壊す。

- Settings画面に実際のWi-FiチャネルとReceiver IDを表示する。
- MiniJoyC送信側のチャネルをその表示値へ、送信先IDをReceiver IDまたは0へ設定する。
- 現在の既定Receiver IDは1。画面上でIDを変更した場合は送信側も合わせる。

**確認手順**

1. K151をRelay用Wi-Fiへ接続し、Settings画面でチャネルとReceiver IDを確認する。
2. MiniJoyCを同じチャネル・対象IDに設定する。
3. FACE画面でスティックを動かし、首が物理的に動くことを確認する。
4. 垂直軸が安全範囲の外へ行かないこと、Relay音声セッション中もリモコンで首だけを
   操作できることを確認する。

### 19. CoreS3上面タッチから会話とスリープを操作できるようにした

画面ページに依存せず会話へ戻れる操作と、明示的に会話・再生を止めて休止姿勢へ移す操作を
追加した。

**現行仕様**

- 上面を1回タップするとFace画面へ移り、Relay接続済みなら聞き取りを開始する。
- 2秒以内に上面を2回タップすると、マイクと再生を止めて`conversation.pause`を送り、
  首を伏せたスリープ姿勢へ移る。
- Settingsの音量ボタンは短いタップで1段階、500 ms以上の長押しで連続調整する。
- 画面タップによる会話操作、カメラ領域、横スワイプは従来どおり維持する。

### 20. 猫顔に段階的な待機動作を追加した

会話していない間も端末が固まって見えないように、Face画面の待機を`Active`、
`LookingAround`、`Sleeping`の3段階に分けた。

**現行仕様**

- Face画面の`ready`または`listening`でカメラワイプが非表示のときだけ自動待機する。
- 30秒間操作や検出済み発話がないと`LookingAround`へ移り、2〜15秒間隔で猫の視線と首を
  ランダムに動かす。
- 見回し開始から5分後に`Sleeping`へ移り、首をホーム位置へ戻して、閉じた目、呼吸、
  `Z`表示を描画する。
- タッチ、または聞き取り中に3フレーム連続で音声活動を検出すると`Active`へ復帰する。
- 自動睡眠は会話を止めない。上面ダブルタップによる明示的スリープだけが
  `conversation.pause`を送り、マイクと再生を停止する。

**設計判断**

アバターの待機表現と会話セッションの停止は別の状態として扱う。見た目が眠っていても
聞き取り中なら発話で自然に復帰でき、利用者が明示的に休止させた場合だけ会話を止める。

### 21. CoreS3へUSB Relay経路を追加した

PCとCoreS3をUSB接続し、Windows上のブリッジから既存のローカルRelayを利用できるように
した。Wi-Fi実装は置き換えず、Settings画面で`AUTO`、`WI-FI`、`USB`を選択し、選択値を
NVSへ保存する。上位のRelayメッセージとPCM形式は両経路で共通にし、物理転送だけを
切り替える構成とした。

**実機で確認したハードウェア条件**

- CoreS3はESP32-S3（240 MHz）、16 MB Flash、8 MB PSRAMを搭載する。PlatformIOが表示する
  RAM上限327,680 bytesは静的配置に使える領域の指標であり、チップの全RAM量や実行時の
  空きヒープを表す値ではない。
- USB追加後の実測ビルドは、静的RAM 116,964 / 327,680 bytes（35.7%）、Flash
  1,895,933 / 6,553,600 bytes（28.9%）だった。
- 実際のUSB処理はESP32-S3内蔵USB Serial/JTAGのHWCDCを使う。`Serial.begin(115200)`の
  `115200`は互換API上の設定値であり、USBの物理転送速度を115.2 kbit/sへ制限する値ではない。
- マイク・スピーカー音声はPCM16 / 24 kHz / monoで、実データ量は48,000 byte/sである。
  20 msの音声は480 sample、960 byteになる。
- USBは書き込み、端末ログ、Relay制御、双方向PCMで同じSerial/JTAG経路を共有する。
  専用の「音声用COMポート」が別に存在するわけではない。

ハードウェア仕様は[M5Stack CoreS3公式仕様](https://docs.m5stack.com/en/core/CoreS3)、
USB Serial/JTAGの性質は
[Espressif公式ガイド](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/usb-serial-jtag-console.html)
も参照する。後者は、USB Serial/JTAGが再構成可能なUSB-OTG stackではなく、Serial/JTAG用の
固定機能controllerであることを明記している。

**メモリ量で引っかかる点**

- HWCDCのRX/TXリングは各16 KiBへ拡張し、必ず`Serial.begin()`より前に設定する。開始後に
  サイズを変更しても、既に確保されたバッファへ反映されない実装がある。
- COBS処理は受信encoded、受信decoded、送信raw、送信encodedの最大約8 KiB領域を持つ。
  これにHWCDCのRX/TXリングを加えると、USB用の実行時確保は静的RAMのビルド表示に出ない。
- カメラ、画面Sprite、スピーカー再生バッファ、Wi-Fi/ESP-NOWも同時にヒープを使用する。
  ビルド時の`RAM 35.7%`だけを見て余裕があると判断せず、実機で`free heap`と
  `largest free block`を測る。特にWi-FiとUSBが短時間重なる経路切替時を測定対象にする。
- PSRAM総量に余裕があっても、USBドライバやDMA、一部のArduino/FreeRTOS構造体は内部RAMを
  必要とする。PSRAM残量だけでは安定性を判断できない。

**Serialをバイナリ通信へ転用するときの注意**

USB Serial/JTAGにはBoot ROM、Arduino Framework、既存の`Serial.print*`ログも流れる。
JSONやPCMをそのまま書くと、起動ログが途中へ混ざった時点で境界を復元できなくなる。

現行USB封筒は次を持つ。

- 先頭・末尾のゼロdelimiterとCOBS encoding
- Protocol Version、Frame Type、16 bit Sequence
- 32 bit Payload Length、最大8,192 byteのPayload
- CRC32

送信側はdelimiterを含む1フレームを1回の`Serial.write()`へまとめ、HWCDCの送信mutex区間で
別ログが割り込む可能性を下げる。受信側はCOBSとCRCが成立しない部分をraw log/noiseとして
捨て、次のゼロdelimiterから再同期する。単に改行区切りやJSONの括弧数で復元しようと
しない。

VID `0x303A`はEspressif端末候補を見つけるためにだけ使う。同じVIDの別機器をStack-chanと
誤認しないよう、Protocol VersionとDevice IDを返すハンドシェイクが成功して初めてCOMを
占有する。Device TokenはRelay認証へ必要だが、ログやManager状態JSONには出力しない。

**COMを開くだけで再起動・再列挙する場合がある**

PC側でDTR/RTSをfalseにしても、初回列挙、書き込み直後、USB Serial/JTAGドライバの状態に
よってはCoreS3が一度リセットまたは再列挙する。最初のCOM番号が永続する前提や、一度の
`serial.Serial.open()`で必ず端末応答が得られる前提を置かない。

- ブリッジは候補ポートを再走査し、端末応答を最大12秒待つ。
- ホストは0.5秒間隔でprobeを送る。端末は最後のprobeから1.5秒を超えたらホスト不在と
  判定する。
- ブリッジ再起動時は、端末に古いUSB接続状態が残っていても`HOST_CLOSE`で同期し直して
  新しいWebSocketを開く。
- USB抜去やCOM消失は例外終了ではなく、通常の再検出状態として扱う。

**音声は「帯域に余裕がある」だけでは安定しない**

FoundryやRelayはPCMを実時間より速いburstで返すことがある。TCP/WebSocketは受信側の
backpressureを使えるが、PCからHWCDCへ8 KiBずつ連続writeすると、CoreS3の16 KiB RXリングを
短時間で埋められる。メインloopがスピーカー処理や画面描画をしている間にリングが溢れると、
音声のバリつき、欠落、早送りに聞こえる。

現行ブリッジは出力PCMを960 byte（20 ms）へ分割し、最初に100 ms（約4.8 KiB）だけ先行させ、
以後は48,000 byte/sでpaceする。8 KiBという値は「USBプロトコルで許す最大payload」であり、
毎回8 KiB送るべき推奨音声chunkではない。

CoreS3側でも、`playRaw()`へ渡したメモリを再生完了前に上書きしてはいけない。通信受信
バッファと非同期再生バッファの寿命を分け、複数バッファを循環利用する。USB frame境界は
音声の意味的境界ではなく、到着順の連続PCMとして扱う。

現行ファームウェアの`write_all()`は短いwriteを再試行するが、最大1秒間メインloopを
占有しうる。通常はPC側pacingと小さいchunkで回避しているが、長時間のホスト停止や大きな
上りburstまで保証する仕組みではない。カメラ・UI・音声を止めるほどの待ちが観測されたら、
USB I/Oを専用FreeRTOSタスクと上限付きqueueへ分離する。

**AUTO/Wi-Fi/USBは選択状態と実経路を分ける**

- `AUTO`は起動後3秒間USBホストを待ち、利用可能ならUSB、利用できなければWi-Fiを開始する。
- `USB`固定はUSBホストが消えてもWi-Fiへ切り替えず、USB復帰を待つ。
- `WI-FI`固定はUSBケーブルとブリッジが存在してもWi-Fiを維持する。
- AUTOのUSB使用中はWi-Fiを停止し、同じ無線を使うESP-NOWもdeinitする。USB切断後に
  Wi-Fiへ戻す場合は、STA接続とチャネル確定後にESP-NOWを再初期化する。
- 会話中にUSBが利用可能になっても即時切替せず、応答完了後の`ready`で次の
  `audio.start`を送る前に切り替える。

「ユーザーが選んだモード」「現在使っている物理経路」「Wi-Fiリンク状態」「USBホスト状態」
「Relay/Agent状態」を1個のenumへ詰め込まない。経路切替では録音停止、再生停止、旧Relay切断、
無線停止/開始、新Relay接続の順序を守る。切替は新しいRelay/Foundryセッションになるため、
会話文脈を保持できるものとして扱わない。

Relay側ではDevice ID単位で接続を排他し、新しいUSB/Wi-Fi接続が成立したら残っている旧接続を
閉じる。端末側のTCP切断検出だけに任せると、切替時に同じDevice IDのFoundryセッションが
二重化する。

**Windowsブリッジで発生した固有問題**

- `pyserial`の`baudrate=115200`はAPIとして必要でも、HWCDCの実効帯域を表さない。
- COM read/writeは専用threadで行い、WebSocketと状態機械はasyncio event loopで扱う。
  シリアルのblocking readをevent loopへ直接置かない。
- 音声frameごと（20 ms間隔）にManager用状態JSONを書いていたため、不要なディスクI/Oと
  event loopの停止が発生した。状態更新は最大4回/秒へ制限する。
- WindowsではManagerが状態JSONを読んでいる瞬間に一時ファイルを`replace()`すると
  `PermissionError`になることがある。この観測用ファイルの失敗でUSB reader taskを
  終了させると「ブリッジが周期的に落ちる」症状になる。状態書き込みはbest effortとし、
  例外を通信処理へ伝播させない。
- ブリッジ、PlatformIO upload、serial monitorは同じCOMを同時に開けない。Managerを
  COM所有者とし、ファームウェア書き込み直前にブリッジを停止し、成功・失敗にかかわらず
  `finally`で再開する。手動monitor時も先にManagerからブリッジを停止する。

**設定と障害切り分けで引っかかる点**

Wi-Fi RelayのPCアドレスはファームウェアへコンパイルされる。PCのDHCPアドレスが変わったら
`deploy-from-relay.ps1`で現在のLAN IPv4を選び直して再書き込みする。`0.0.0.0`や
`127.0.0.1`をStack-chanの接続先には使えない。書き込みスクリプトはLAN側`/healthz`を確認し、
COMを一時解放してuploadし、その後ブリッジを再開する。

USB固有かを比較するときは、画面を`WI-FI`へ切り替えるだけではPC上のUSBブリッジprocessは
残る。データ経路だけを除外する試験と、Managerからブリッジ自体を停止する試験を分ける。
今回のブラウザFunctionのChromium例外はWi-Fiデータ経路でも再現しており、COBS/CRCやUSB PCM
転送を直接原因とは断定できなかった。時間的にUSB追加後に現れた問題でも、端末経路、PCの
兄弟process、Relayの起動contextを段階的に外して判断する。

**確認済み範囲と残る耐久試験**

COM3実機でUSB接続、USB停止後のWi-Fi復帰、USB再開後の再接続、CRC不一致0、Sequence欠損0を
確認した。これらは基本的な実現可能性を示すが、30分の双方向連続転送、USB抜き差し100回、
PCスリープ・復帰20回、カメラ同時使用時の最小空き内部heapは未完了である。短時間の成功を
量産・長時間運用の保証へ読み替えず、リリース前に耐久値を測定して記録する。

### 22. WindowsのブラウザFunctionを対話プロセスへ分離した

Stack-chanからURLを開くFunctionを実行すると、Relayは成功を返した後にChromeまたはEdgeが
`0x80000003`のアプリケーションエラーダイアログを表示した。失敗したChromium processは
ダイアログの背後で生存していたため、`Popen.pid > 0`を成功条件にすると障害を見逃した。

**切り分けで得た知見**

- ChromiumをRelayの子processとして直接起動する経路で再現した。Chrome固有ではなくEdgeでも
  同じ例外クラスが発生したため、実行ファイルの選択だけでは解決しなかった。
- USB経路で発生した問題でも、Wi-Fi WebSocket経路で再現すればCOBS/CRC、USB音声転送、COMの
  データ破損は発生条件から外せる。ただしPC上のUSBブリッジprocessは残りうるため、端末を
  `WI-FI`へ切り替える試験と、Manager APIでブリッジ自体を停止する試験を分ける。
- `explorer.exe URL`をRelayから起動するとChromiumのクラッシュは消えたが、ページは開かなかった。
  同じWindows Session 1にいるだけでは、管理対象processから対話型Explorerへの引き渡しを
  保証できない。
- 対話型PowerShellからExplorerへの正常な引き渡しでも終了コード`1`が返った。Explorerの
  終了コードや子processのPIDだけでは、利用者のブラウザにページが表示されたことを証明できない。
- ネイティブ例外ダイアログが開いている間はWindows Error Reportingのイベントが確定せず、
  processも応答中に見える場合がある。イベントログはダイアログを閉じた後に採取する。

**対処と現行仕様**

- WindowsのブラウザFunctionは、RelayからChromiumを直接起動せず、`127.0.0.1:8787`だけで
  listenするLocal Managerの専用エンドポイントへ検証済みURLを送る。
- Managerは対話ユーザーのcontextで`explorer.exe URL`を実行し、既存の既定ブラウザと
  プロファイルへ引き渡す。任意の実行ファイル名、オプション、shell commandは受け付けない。
- Relay側とManager側の両方で、`http`/`https`限定、URL内資格情報の拒否、任意のドメイン
  許可リストを検査する。IPC境界を追加しても、呼び出し側だけの検証へ依存しない。
- brokerへ接続できない、要求を拒否された、または起動処理が失敗した場合はFunction errorを
  返す。process生成だけで`status=opened`を返さない。
- ManagerとRelayは別processなので、実装変更後は両方を再起動する。Relayだけの再起動では
  Manager側のbroker変更は読み込まれない。

**確認手順**

1. Managerのbrokerへ安全なURLを直接POSTし、既存ブラウザプロファイルで開くことを確認する。
2. USBブリッジをManager APIで完全停止し、Wi-Fi接続ログを確認してFunctionを3回実行する。
3. Stack-chanをUSBへ切り替え、ManagerでCOMポートと`relay_connected=true`を確認して、
   Functionを3回実行する。
4. 各回でページ表示、アプリケーションエラーダイアログがないこと、Function結果、孤立した
   Chromium processがないことを確認する。

今回の実機確認では、Wi-FiとUSBの両方で3回連続して既存Chromeセッションにページが開き、
アプリケーションエラーダイアログは発生しなかった。自動テストはRelay全体で69件成功し、
CoreS3 firmware buildも成功した。

## ファームウェアを書き込んだ場合の期待動作

1. 起動後、Relayへ接続し `hello` を送る。画面は待機状態になる。
2. タップで会話を開始すると、マイクを有効化して listening になる。
3. 発話後は thinking / searching / speaking へ遷移し、その間マイクを止める。
4. 応答が自然終了すると、スピーカーの再生終了後に自動で次の listening へ戻る。
5. どの会話状態でもタップすると、再生とマイクを止め、Relayへ
   `conversation.pause` を送り、待機状態になる。次のタップまでは自動再開しない。
6. Foundryだけが再接続した場合、会話が有効なら `session.reconnected` → `ready` の後に
   listening を再開する。

## 既知の制約

- FoundryのWebSocket再接続は新しいRealtimeセッションである。会話履歴そのものは
  再接続先へ復元されない。
- AECなしのため、全二重の音声割り込みは標準では有効化しない。
- 出力を通常時に捨てない方針のため、再生速度より生成速度が大幅に速い場合は遅延が
  増える。バックログ警告を監視する。
- CoreS3向けのコンパイルは確認済みだが、マイク・スピーカーの実機連続運用は別途確認が
  必要である。

## 確認・運用手順

1. Relay変更後は `relay\\run-manager.ps1 -Restart` で再起動する。
2. `stackchan/include/secrets.hpp` にLAN IP、ポート、`RELAY_USE_TLS` を設定する。
3. `stackchan` ディレクトリで `pio run`、実機接続後に `pio run -t upload` を実行する。
4. 問題時は管理UIのRelayログとヘッドセットログをコピーし、状態遷移
   (`ready` / `listening` / `thinking` / `searching` / `speaking`) と直前のイベントを確認する。
