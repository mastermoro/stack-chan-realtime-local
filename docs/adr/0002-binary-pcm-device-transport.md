**日本語** | [English](../en/adr/0002-binary-pcm-device-transport.md)

# ADR-0002: デバイスと Relay の間で生の PCM を WebSocket バイナリフレームとして送信する

ステータス: 承認済み

USB Relay モードにより追補: Wi-Fi では引き続き WebSocket バイナリフレームを使用する。USB ルートでは、同じ生 PCM バイナリメッセージのセマンティクスをシリアルエンベロープ内で維持し、Windows ブリッジが Relay WebSocket との変換を行う。

## 決定

Stack-chan は PCM16 24 kHz モノラル音声を WebSocket バイナリフレームとして送信する。JSON は制御メッセージにのみ使用する。Base64 変換は、Realtime API と通信する際に Relay のみが行う。

## 結果

- ESP32/CoreS3 の帯域幅と CPU のオーバーヘッドを削減できる。
- デバイスプロトコルを Foundry 固有のイベントスキーマから独立させられる。
- バイナリのデバイストランスポートと Realtime の JSON/base64 イベントとの変換は Relay が担う。
