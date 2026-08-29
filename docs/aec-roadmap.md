[English](en/aec-roadmap.md)

# AEC と全二重通信の実装ロードマップ

このリポジトリは Relay レイヤーではローカルファーストだが、音響エコーキャンセレーションは引き続き
CoreS3 上で実行する。Windows Relay は引き続き Foundry セッション、Function Calling、
ローカル PC アクションを担当する。

## 不変条件

- 既存の半二重経路を安全なフォールバックとして維持する。
- ネットワーク音声は PCM16、24 kHz、mono のままとする。
- AEC 処理には独立した 16 kHz の内部境界を使用する。
- 再生リファレンスは、音量とフォーマットの変換後に speaker TX 経路が受け付けた PCM からのみコピーする。
- 音声タスクでは JSON 解析、ブラウザー操作、画面描画、メモリ割り当て、ファイル書き込み、ネットワーク送信を行わない。
- 障害発生時はデバイスを再起動せず、機能を縮退させる。

## 目標パイプライン

```text
Foundry -> Windows Relay -> 24 kHz playback ring
  -> volume/limiter -> speaker TX
  -> accepted-TX reference timeline -> 24-to-16 kHz

CoreS3 mic RX -> capture ring -> 16 kHz
  -> PassThroughProcessor / EspSrAecProcessor
  -> 16-to-24 kHz -> Windows Relay -> Foundry
```

## 計画中のファームウェアファイル

```text
stackchan/include/audio/audio_types.hpp
stackchan/include/audio/audio_device.hpp
stackchan/include/audio/spsc_audio_ring.hpp
stackchan/include/audio/audio_pipeline.hpp
stackchan/include/audio/audio_processor.hpp
stackchan/include/audio/playback_clock.hpp
stackchan/include/audio/reference_timeline.hpp
stackchan/include/audio/audio_diagnostics.hpp
stackchan/include/audio/fallback_policy.hpp
stackchan/include/conversation_controller.hpp

stackchan/src/audio/core_s3_full_duplex_device.cpp
stackchan/src/audio/pass_through_processor.cpp
stackchan/src/audio/streaming_resampler.cpp
stackchan/src/audio/audio_pipeline.cpp
stackchan/src/conversation_controller.cpp
```

純粋なデータ構造とステートマシンには、CoreS3 ハードウェアへ接続する前に PlatformIO native テストを用意すること。

## マイルストーン

### A0 — 再現可能なベースライン

- Espressif プラットフォームとファームウェアライブラリのバージョンを固定する。
- 変更前の半二重ファームウェアを CI でビルドする。
- RAM と Flash の使用量を記録する。

ステータス: このリポジトリで実装済み。固定されたベースラインは CoreS3 向けにビルドできる。

### A1 — 診断

1 秒単位の集計メトリクスを追加する:

- mic、reference、processor-output の RMS/peak
- capture/playback/reference ring の充填量と high-water mark
- I2S timeout/drop の回数
- received、queued、submitted、estimated-played のサンプル数
- AEC/processor の平均実行時間と最大実行時間
- internal heap、largest block、PSRAM、task stack の low-water mark

詳細な PCM キャプチャは引き続きオプトインとし、絶対にコミットしてはならない。

### A2 — 固定音声リングとクロック

- 実行時にメモリを割り当てない固定容量の SPSC ring を追加する。
- `millis()` に依存せず、64-bit のサンプル位置を追跡する。
- WebSocket のフレーム境界と処理のフレーム境界を分離する。
- wrap、overflow、underflow、time conversion の native テストを追加する。

### A3 — Audio I/O の所有権

- マイクとスピーカーのライフサイクル管理をすべて `main.cpp` の外へ移す。
- `M5AudioHal` を従来の半二重バックエンドとして維持する。
- capture、TX acceptance、queue depth、capabilities、counters、stop/restart のための `AudioDevice` 契約を追加する。
- 現在の動作を `AudioPipeline + PassThroughProcessor` 経由にする。

### A4 — CoreS3 全二重ハードウェアゲート

- 共有 I2S RX/TX と両方の codec を所有する、機能フラグ付きバックエンドを追加する。
- AEC なしで ES7210 capture と AW88298 playback の同時動作を検証する。
- Wi-Fi、camera、servo に負荷をかけた状態で 10-second functional test と 30-minute soak test を実行する。

RX または TX の停止、drop の繰り返し、デバイスのリセットが発生する場合は先へ進まないこと。
このハードウェアゲートを通過するまでは、デフォルトを半二重のままにする。

### A5 — 再生リファレンスと同期

- speaker-TX-accepted PCM のみを取り出す。
- received、queued、submitted、DMA-buffered、estimated-played のサンプルを追跡する。
- リファレンスサンプルをタイムスタンプ付き timeline に保存する。
- 0–150 ms の範囲で診断用の delay search を追加する。
- 長時間動作時の mic/playback clock drift と reference fill を観測する。

### A6 — 会話状態とフォールバック状態

2 つの独立したステートマシンを維持する:

```text
Conversation:
Paused -> Listening -> Thinking/Searching -> Speaking -> Interrupting

Audio pipeline:
Stopped -> Starting -> HalfDuplex/FullDuplex
                    -> Degraded -> Restarting -> Failed
```

`conversation.pause` を送信するときは `output_audio.started` の item ID と estimated played samples を使用し、
Relay が未再生の出力をキャンセルして切り詰められるようにする。

フォールバック順序:

1. camera/servo/diagnostic の負荷を下げる
2. AEC の複雑度を下げる
3. ローカル barge-in を無効にする
4. audio pipeline を 1 回再起動する
5. 従来の半二重へ戻す

flapping を避けるため、上位モードへ復帰する前に安定期間を設ける。

### A7 — AEC 処理境界

- 状態を保持する 24/16 kHz resampling を追加する。
- 任意の I2S frame を processor が要求する chunk size に適合させる。
- 10 seconds および長時間の stream で正確な sample count を検証する。
- `PassThroughProcessor` を比較用およびフォールバック実装として維持する。

### A8 — ESP-SR AEC

初期候補:

- 1 つの mic と 1 つの playback reference
- 16 kHz の PCM16
- `AEC_MODE_FD_LOW_COST`
- filter length 4
- 16-byte aligned working buffer

raw mic、reference、processed output の測定値を用いて、normal および aggressive nonlinear processing を評価する。
Two-mic モードと high-performance モードは後続のチューニング対象であり、ベースライン要件には含めない。

## 受け入れテスト

| テスト | 要件 |
|---|---|
| Assistant audio only | self-interruption が発生しない |
| User interrupts at 30 cm | local playback が 200 ms 以内に停止する |
| User interrupts at 1 m | normal speech が検出される |
| Maximum speaker volume | false barge-in が繰り返し発生しない |
| Double talk | user speech が明瞭に聞き取れる |
| Camera, Wi-Fi, and servo active | WDT または I2S timeout が繰り返し発生しない |
| 30-minute soak | 単調な reference drift が発生しない |
| AEC init/runtime failure | 半二重で会話が継続する |

## 推奨する変更順序

1. diagnostics と native test environment
2. ring、sample clock、playback tracking
3. `AudioPipeline` の背後への従来の半二重実装の移行
4. playback item tracking と正確な pause/truncate
5. 機能フラグ付き CoreS3 全二重バックエンド
6. reference timeline、resampling、fallback policy
7. デフォルト無効の機能フラグの背後への ESP-SR AEC の実装

各段階で、既存の半二重フォールバックが引き続き利用できることを確認する。

