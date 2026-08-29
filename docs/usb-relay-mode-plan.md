# USB Relayモード実装計画

[日本語](usb-relay-mode-plan.md) | [English](en/usb-relay-mode-plan.md)

更新日: 2026-08-29

状態: ソフトウェア実装済み（CoreS3実機の長時間・障害復旧ゲートは継続確認）

この文書はUSB Relayモードの設計判断、実装時の検討事項、受け入れ条件を残す履歴文書である。
現行の利用手順は`README.md`、通信仕様は`docs/protocol.md`を正とする。

## 目的

Wi-Fi/WebSocket経路を維持したまま、USB接続されたWindows PC上のRelayを
Stack-chanから利用できるUSB経路を追加した。

利用者はStack-chan本体のSettings（Volume調整）ページから`AUTO`、`Wi-Fi`、
`USB`を選択できる。USBモードはStack-chanがWi-Fiへ接続できない環境でも会話機能を
利用可能にする。

## 再検証結果

### 結論

ハードウェア能力には十分な余裕があり、USB Relayモードは実現可能である。ただし、
現在の`Serial`へそのままPCMを書き込む実装は採用しない。次の条件を満たすことを
本実装開始のゲートとする。

- 現在使用しているUSB Serial/JTAG（HWCDC）で、フレーム欠損を検出できる試験実装を作る。
- 音声の2倍以上の帯域余裕、長時間安定性、切断復旧、ホスト停止時の非ブロッキング性を
  CoreS3実機とWindowsで確認する。
- USB I/Oをメインループから分離し、音声・画面・カメラ処理を停止させない。
- Wi-Fi接続状態とRelay/Agent状態を分離し、USB切断後のWi-Fi起動・接続を独立して扱う。
- USB/Wi-Fi切替時に同じDevice IDのFoundryセッションが二重化しないことを保証する。

### 確認したハードウェアと現行環境

| 項目 | 確認結果 | 評価 |
| --- | --- | --- |
| SoC | ESP32-S3、Xtensa LX7デュアルコア、最大240 MHz | USBフレーミング処理に十分 |
| メモリ | CoreS3は16 MB Flash、8 MB PSRAM。SoC内部SRAMは512 KB | 数十KBのキュー追加は可能だが内部RAM実測が必要 |
| USB | USB 2.0 Full-Speed、固定機能USB Serial/JTAGとUSB OTGを搭載 | 48 KB/s音声に対して物理帯域は十分 |
| 現在のUSB設定 | `ARDUINO_USB_MODE=1`、`ARDUINO_USB_CDC_ON_BOOT=1` | `Serial`は通常UARTではなくHWCDC |
| 現在のArduino Core | Arduino-ESP32 2.0.17系 | HWCDC長時間転送を実機ゲート必須とする |
| 現在の静的使用量 | 既存ELFでDRAM data約52 KB、bss約65 KB、Firmware約1.89 MB | Flash余裕は大きい。動的内部RAMは別途測定する |

CoreS3公式仕様は240 MHz、16 MB Flash、8 MB PSRAM、USB Type-CのOTG/CDC対応を
明記している。ESP32-S3公式仕様は512 KB SRAM、Full-Speed USB OTG、USB Serial/JTAGを
明記している。

- [M5Stack CoreS3公式仕様](https://docs.m5stack.com/en/core/CoreS3)
- [ESP32-S3公式データシート](https://documentation.espressif.com/esp32_s3_datasheet_en.pdf)
- [ESP32-S3 USB Serial/JTAG公式ガイド](https://docs.espressif.com/projects/esp-idf/en/release-v5.3/esp32s3/api-guides/usb-serial-jtag-console.html)

### 帯域評価

現在の音声はPCM16、24 kHz、mono、20 ms単位である。

| 内容 | 値 |
| --- | ---: |
| 1フレーム | 480 samples / 960 bytes |
| 1秒あたり | 50 frames |
| 音声ペイロード | 48,000 bytes/s |
| COBS相当の符号化、ヘッダー、CRCを含む概算 | 約49 KB/s |
| 現行半二重で必要な主方向帯域 | 約49 KB/s |
| 将来全二重化した場合の合計 | 約98 KB/s |

`monitor_speed = 115200`および`Serial.begin(115200)`はUSB CDCの回線速度上限ではない。
USB Serial/JTAGはUSBの固定機能CDC-ACMとしてPCへ直結されるため、以前の
「115200 bpsでは不足」という評価は撤回する。

一方、USB Serial/JTAGには小さな内部バッファとホスト側バックプレッシャーによる短時間停止の
注意事項がある。また、Arduino-ESP32公式リポジトリにはESP32-S3 HWCDCでの欠損報告と修正履歴が
あるため、理論帯域だけで可否を決めない。

- [USB Serial/JTAGのバッファ制約](https://docs.espressif.com/projects/esp-idf/en/release-v5.3/esp32s3/api-guides/usb-serial-jtag-console.html#data-buffering)
- [Arduino-ESP32 HWCDC欠損報告](https://github.com/espressif/arduino-esp32/issues/9378)

### USB方式の選択

第一案は現在有効な固定機能USB Serial/JTAG（HWCDC）を維持する。これは同じCOMポートで
ログ、通信、`esptool.py`による自動書き込みを扱えるため、現行運用への影響が最小である。

USB OTG/TinyUSB CDCへの切替は第一案にしない。固定機能USB Serial/JTAGとは同時使用できず、
アプリ起動後のUSBデバイス再列挙、VID/PIDやCOM番号の変化、自動書き込み手順への影響がある。
HWCDCが性能ゲートを通過できない場合だけ、第二案として評価する。

### 実現可能性判定

| 領域 | 判定 | 条件 |
| --- | --- | --- |
| USB物理帯域 | 可 | 実機で2倍以上の余裕を確認する |
| CPU性能 | 可 | USB I/Oと符号化を専用タスクへ分離する |
| Flash/PSRAM | 可 | 現状から見て十分な余裕あり |
| 内部RAM | 条件付き可 | Wi-Fi、カメラ、USB同時使用時のヒープを測定する |
| Windowsブリッジ | 可 | `pyserial`とWebSocketで実装可能 |
| 現行Wi-Fi維持 | 条件付き可 | Wi-Fi状態とAgent状態の分離が必須 |
| ESP-NOW | 可 | USB実経路中は明示的に停止する |
| 自動書き込み | 可 | ManagerによるCOM所有権管理が必須 |

### 接続実機での基礎確認

2026-08-29にWindowsへ接続したCoreS3で、USBデータ経路の列挙と現行ログを確認した。

| 項目 | 実測結果 |
| --- | --- |
| COMポート | `COM3` |
| USB ID | `VID_303A&PID_1001&MI_00` |
| Windowsドライバー | Microsoft `usbser.inf`、状態Started |
| ポートオープン | 115200指定で成功。HWCDCなので指定値は物理速度を制限しない |
| 8秒のログ受信 | 1,797 bytes |
| 実機ログ | PSRAM enabled、Wi-Fi connected、ESP-NOW channel 9を確認 |

最初のポートオープン中に`USB_UART_CHIP_RESET`とBoot ROMログを1回観測した。2回目の
ポートオープンでは再現しなかったため、ポートオープンが原因とは断定しない。ただし、
初回列挙やManager起動時に端末が一度再起動・再列挙してもブリッジが復旧できることを
受け入れ条件へ追加する。

現行ファームウェアにはUSB音声トランスポートがまだないため、連続PCM性能はこの確認では
測定できない。性能ゲートは試験用ファームウェアとブリッジを作成した後に実施する。

## 維持する既存仕様

- Wi-Fi接続時は従来どおりRelayの`/v1/realtime`へWebSocket接続する。
- RelayとFoundry間のプロトコル、端末認証、会話状態、Web検索、ローカルFunctionの
  外部仕様を変更しない。
- 音声形式はPCM16 little endian、24 kHz、monoを維持する。
- JSON制御メッセージと音声フレームの意味は既存プロトコルを維持する。
- Wi-Fi固定モードでは現在の動作を回帰させない。

## 確定要件

### モード選択

Settings（Volume調整）ページに`AUTO`、`Wi-Fi`、`USB`の3状態スイッチを追加する。
選択値と、現在実際に使用している通信経路は区別して表示する。

選択値は本体の不揮発領域へ保存し、再起動後も維持する。未保存または保存値が無効な
場合の既定値は`AUTO`とする。

| 選択モード | Relay経路 | Wi-Fi動作 | 経路切断時 |
| --- | --- | --- | --- |
| `AUTO` | USB優先 | USB使用中は停止 | USB切断後にWi-Fiを起動して復帰 |
| `Wi-Fi` | Wi-Fi固定 | 接続と再接続を継続 | Wi-Fiの復旧を待ち、USBへ退避しない |
| `USB` | USB固定 | 接続と再接続を停止 | USBの復旧を待ち、Wi-Fiへ退避しない |

### AUTOモード

- 起動時はUSBブリッジとのハンドシェイク成立を最大3秒待つ。
- 3秒以内に成立しなければWi-Fiを実経路としてRelayセッションを開始する。
- この3秒間はWi-Fiを起動せず、USBが成立しなかった時点でWi-Fi接続を開始する。
- Wi-Fiで会話中にUSBが利用可能になった場合、会話終了後の待機状態まで切替を保留する。
- USBへ切り替えるときは旧Wi-Fi Relay接続とESP-NOWを終了してからWi-Fiを停止する。
- USBが実経路のときにUSB接続が失われた場合、進行中の会話を停止し、Wi-Fiを起動して
  接続完了後に新しいRelayセッションを開始する。
- USB切断からWi-Fi復帰までは、Wi-Fi関連付けとRelay接続のため数秒以上かかりうる。

### 手動切替とセッション

- 手動切替は選択後すぐに開始する。
- 録音と再生を停止し、現在のRelay接続を安全に閉じる。
- 選択した経路で新しいRelay/Foundryセッションを開始する。
- 切替前の会話コンテキストは引き継がない。
- `USB`固定中にケーブルが抜けた場合はUSB再接続を待つ。
- `Wi-Fi`固定中に接続を失った場合はWi-Fi再接続を待つ。

### USBモード

- Stack-chan本体がWi-Fiへ接続できなくても会話機能を利用可能にする。
- 初期対応では1台のWindows PCにつきUSB接続するStack-chanは1台までとする。
- Wi-Fi接続端末の既存の同時接続動作には新しい制限を加えない。
- USBが実際のRelay経路である間、ESP-NOWリモコンによる首操作は対応対象外とする。
- USBの利用可否はVBUSだけで判断せず、PC側ブリッジとのハンドシェイク成立で判断する。

### Windows USBブリッジ

- Relay ManagerでRelayを起動するとUSBブリッジも自動起動する。
- Stack-chanのCOMポートを自動検出し、ホットプラグとCOM番号変更へ追従する。
- 複数の候補を一意に特定できない場合は接続せず、Managerへ理由を表示する。
- USB上の端末通信を`ws://127.0.0.1:8080/v1/realtime`へ中継する。
- 既存のDevice IDとDevice Tokenによる認証を維持し、資格情報をログへ出力しない。
- Relay停止時はブリッジも停止する。

### ログとCOMポート管理

- USB通信、端末ログ、制御メッセージを識別可能なフレームとして多重化する。
- USB通信中の端末ログはブリッジで分離し、Relay Manager上で閲覧可能にする。
- ファームウェア書き込みまたは直接シリアルモニターを開始するときは、ブリッジを一時停止して
  COMポートを解放する。
- 書き込みまたはモニター終了後は端末を再検出してブリッジを自動復帰する。

## 現行Wi-Fi仕様への影響分析

### 現在の密結合

現行実装は起動時に必ず`WiFi.mode(WIFI_STA)`と`WiFi.begin()`を実行し、Wi-Fi接続後に
ESP-NOWとRelay WebSocketをまとめて開始する。また、Wi-Fi再試行上限へ達すると
Agent状態そのものを`Error`へ変更する。この構造のままではUSB切断後のWi-Fi起動や、
USB固定からAUTO/Wi-Fiへの切替を安全に表現できない。

次の状態を独立させる。

- ユーザー選択: `AUTO`、`Wi-Fi`、`USB`
- 実Relay経路: `None`、`Wi-Fi`、`USB`
- Wi-Fiリンク: `Off`、`Connecting`、`Connected`、`Backoff`、`Failed`
- USBリンク: `Detached`、`Enumerated`、`Handshaking`、`Ready`
- Relay/Agent状態: 現在の`Disconnected`から`Error`まで
- 保留切替: `None`、`SwitchToUsbWhenReady`

AUTOでUSBが実経路の間はWi-Fiリンクを`Off`に保つ。USB切断時はRelay/Agentを
`Connecting`相当へ遷移させてからWi-Fiを開始する。Wi-Fi固定モードでは現在の再試行間隔と
失敗表示を維持する。USB固定からAUTOまたはWi-Fiへ移るときは、Wi-Fi再試行回数と
失敗ラッチをリセットする。

### ESP-NOW

現行ESP-NOWはWi-Fi APへ接続した後、APと同じチャネルで初期化される。この順序は
Wi-Fi実経路で維持する。USBへ切り替える前に`esp_now_deinit()`を完了し、受信済みの
首操作を破棄してからWi-Fiを停止する。USBが実経路の間はESP-NOWを開始しない。

Wi-Fiへ戻る場合は、STA接続完了とチャネル確定後にESP-NOWを再初期化する。
ESP-NOWは現在のWi-Fiチャネルを使用する必要があるという既存条件を変えない。

- [ESP-NOW公式ガイド](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/network/esp_now.html)
- [ESP32-S3 Wi-Fi公式ガイド](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/wifi-driver/overview.html)

### 会話状態と切替タイミング

現行コードは`response.done`後に自動で次のListeningへ戻るため、単に`Ready`を待つだけでは
USB切替の機会を失う。USB切替が保留されている場合は、`response.done`後のReady遷移で
次の`audio.start`を送る前に保留切替を実行する。

手動切替、USB抜去、Wi-Fi切断では次の順序を守る。

1. 新しい録音を停止する。
2. 再生を停止し、音声送受信キューを破棄する。
3. 現在のRelay接続を閉じる。
4. ESP-NOWとWi-Fiを必要に応じて停止・開始する。
5. 新経路をハンドシェイクし、新しいRelayセッションを開始する。

### Relayセッション二重化

Relayは現在、同じDevice IDからの複数WebSocketを排他しない。経路切替時に古いTCP接続の
切断検出が遅れると、Foundryセッションが一時的に二重化しうる。端末側では必ず旧経路を
閉じてから新経路を開始し、Relay側にもDevice ID単位の接続世代または排他管理を追加する。
新接続を受け付けたら旧接続を終了し、異なるDevice ID同士の同時接続には影響させない。

## 実装アーキテクチャ

```text
Stack-chan
  ├─ WiFiWebSocketTransport ────────┐
  └─ UsbSerialTransport             │
             │                      │
             v                      │
      Windows USB Bridge            │
             │ WebSocket            │
             └──────────────────────┤
                                    v
                       Local Relay /v1/realtime
                                    │
                                    v
                            Microsoft Foundry
```

Stack-chanのRelayクライアントからトランスポート固有処理を分離する。上位層は既存の
接続通知、JSON、PCM送受信インターフェースだけを使用し、選択されたトランスポートの
違いを意識しない構造にする。

USBフレームには少なくとも次の種別を設ける。

- ハンドシェイクと接続状態
- JSON制御メッセージ
- PCMバイナリ音声
- ハートビート
- 診断ログ
- エラー通知

フレームはCOBSなどゼロ区切りで再同期できる方式を第一候補とし、Version、Type、Sequence、
Payload Length、Payload、CRC32を持たせる。SequenceとCRCでHWCDCまたはホスト処理による
欠落・破損を観測できるようにする。

制御フレームは確認応答と再送を可能にする。リアルタイム音声は遅延を増やす再送を行わず、
破損フレームを破棄して欠損数を計測する。USB接続前のBoot ROMやFrameworkログは
フレーム外データとして現れる可能性があるため、ブリッジは区切りとCRCが一致するまで
読み飛ばして再同期する。

既存の25か所の`Serial.print*`呼び出しは共通Loggerへ置き換える。USBハンドシェイク後は
ログフレームとして送信し、音声キューより低い優先度を与える。最終形式はCoreS3での
帯域・CPU負荷検証後に確定する。

## 実装フェーズ（履歴）

以下は実装開始時のチェックリストであり、未完了を示すものではない。実機性能ゲートと
受け入れ試験は、環境依存の継続確認項目として残している。

### 1. USB技術検証

- 現行のUSB Serial/JTAG（HWCDC）で連続PCMを安定して送受信できる実効帯域を測定する。
- `Serial.begin(115200)`の値ではなく、実際のUSB転送量と遅延を測定する。
- TX/RXリングバッファを`begin()`前に拡張し、4 KB、8 KB、16 KBを比較する。
- USB読み書きとフレーム処理を専用FreeRTOSタスクへ置き、メインループからキューで接続する。
- 960 byte/20 msの実トラフィック、制御、ログを加えた負荷で検証する。
- USB抜去、PC再起動、Stack-chan再起動後の再列挙を確認する。
- PCブリッジが読み取りを停止した状態でも、画面、タッチ、音声、ウォッチドッグを停止させない。
- PlatformIOによる書き込み時のCOMポート挙動を確認する。
- カメラとUSB、および経路切替の短い過渡状態でWi-FiとUSBを同時に有効化したピーク内部
  ヒープを測定する。
- 次の性能ゲートを通過できない場合、本実装へ進まずArduino Core更新またはTinyUSB案を評価する。

性能ゲート:

- 100 KB/s以上を各方向で30分連続転送し、CRC不一致、Sequence欠損、短いwriteを記録する。
- 48 KB/sずつの双方向同時転送を30分行い、フレーム境界の破損を0件にする。
- 実音声相当でUSB受信からブリッジのWebSocket送信までの追加遅延をp99 20 ms以下にする。
- 音声キュー滞留を通常40 ms以下、障害時でも100 ms以下に制限する。
- USBタスクは短いwriteを検出して処理し、メインループを20 ms以上連続停止させない。
- カメラとUSB使用時、およびWi-Fi/USB切替過渡状態で内部空きヒープ64 KB以上、
  最大連続領域32 KB以上を残す。
- USB抜き差し100回とPCスリープ・復帰20回で、手動リセットなしに再接続する。
- 初回列挙後の最初のCOMオープンで端末が再起動しても、自動再列挙して接続を成立させる。

### 2. プロトコルと状態遷移の仕様化

- USBフレーム形式、最大長、タイムアウト、ハートビート、再同期方法を定義する。
- `AUTO`、`Wi-Fi`、`USB`の状態遷移表を作成する。
- ユーザー選択、実Relay経路、Wi-Fiリンク、USBリンク、Agent状態を別々に定義する。
- Ready、Listening、Thinking、Searching、Speakingの各状態における切替動作を定義する。
- 切断と手動切替時の録音停止、再生停止、セッション終了順序を定義する。
- 制御フレームの再送と、音声フレームの破棄・欠損計測方針を定義する。

### 3. Stack-chanファームウェア

- Relay通信をトランスポート非依存のインターフェースへ分離する。
- 既存WebSocket実装を`WiFiWebSocketTransport`相当として維持する。
- USBフレーミング、Sequence/CRC、送受信キュー、ハンドシェイクを実装する。
- USB I/O専用タスクを追加し、メインループではブロッキングwriteを行わない。
- USB TX/RXキューに上限と優先度を設け、音声、制御、ログの順に扱う。
- モード制御とフォールバック状態機械を実装する。
- Wi-Fi失敗状態をRelay/Agent状態から分離する。
- Transportへ明示的な`end()`を追加し、旧経路の切断完了後に新経路を開始する。
- Settingsページへ3状態スイッチ、選択値、実接続経路、待機・エラー状態を追加する。
- 選択値を不揮発保存する。
- USB実接続中はESP-NOWをdeinitし、受信済み操作も破棄する。
- `Serial.print*`を、USBフレームへ切替可能な共通Loggerへ置き換える。

主な変更候補:

- `stackchan/include/relay_client.hpp`
- `stackchan/src/relay_client.cpp`
- `stackchan/src/main.cpp`
- USBトランスポート用の新規ヘッダーと実装

### 4. Windows USBブリッジ

- `pyserial`を直接依存へ追加し、COMポート検出とStack-chanハンドシェイクを実装する。
- VID/PIDだけで確定せず、候補ポート上のProtocol VersionとDevice ID応答で端末を識別する。
- USBフレームとWebSocket Text/Binary Frameを双方向変換する。
- Device IDとDevice Tokenを既存Relayの認証ヘッダーへ設定する。
- USBとWebSocketそれぞれの切断・再接続を管理する。
- 音声に対するバックプレッシャーとキュー上限を設け、遅延の無制限な蓄積を防止する。
- ログフレームを通信フレームから分離する。
- CRC不一致、Sequence欠損、短いread/write、キュー滞留、再接続回数を計測する。
- COM切断時は対応するRelay WebSocketを直ちに閉じる。
- Relay gatewayへDevice ID単位の接続世代管理を追加し、新接続時に残存する旧接続を終了する。

### 5. Relay Manager統合

- Relayのready確認後にUSBブリッジを起動し、停止時はブリッジをRelayより先に停止する。
- 検出COM、Device ID、ハンドシェイク、WebSocket接続状態を表示する。
- USB端末ログとブリッジログを表示する。
- ポート競合、複数候補、認証失敗、Relay未起動を判別して表示する。
- ブリッジの異常終了を監視し、安全な範囲で自動再起動する。

主な変更候補:

- `relay/local_manager.py`
- USBブリッジ用の新規Pythonモジュール
- `relay/tests/`以下のテスト

### 6. 書き込み・モニター連携

- `deploy-from-relay.ps1`からManagerへCOM解放を要求できるようにする。
- 書き込み終了後は成功・失敗にかかわらず再検出を開始する。
- 直接シリアルモニター用にブリッジの一時停止・再開操作をManagerへ追加する。
- COMポートを強制的に奪わず、使用中の場合は安全に失敗理由を表示する。

### 7. テストと文書化

- 状態機械、USBフレーム、認証、ログ分離を単体テストする。
- 疑似シリアルとローカルWebSocketを用いたPC統合テストを追加する。
- USB切断後にWi-Fiを初期化し、接続成功・失敗をAgent状態へ正しく反映することをテストする。
- 同じDevice IDの新接続が旧Relayセッションを確実に終了することをテストする。
- ホスト読み取り停止、短いwrite、CRC破損、途中切断を注入する。
- CoreS3実機で長時間音声と障害復旧を試験する。
- Wi-Fi固定モードについて既存テストと実機回帰確認を行う。
- README、開発手順、プロトコル、トラブルシューティングを更新する。

## 受け入れ試験

| ケース | 期待結果 |
| --- | --- |
| AUTOでUSB接続済みの状態から起動 | 3秒以内にUSBを選択し、Wi-Fi Relayセッションを作らない |
| AUTOでUSBなしの状態から起動 | Wi-Fiで従来どおり接続する |
| AUTOのWi-Fi会話中にUSBを接続 | 会話中は維持し、待機状態でUSBへ切り替える |
| AUTOのUSB会話中にケーブルを抜去 | 会話を停止し、Wi-Fiを起動して接続後に復帰する |
| USB固定中にケーブルを抜去 | Wi-Fiへ切り替えずUSB再接続を待つ |
| Wi-Fi固定中にUSBを接続 | USBへ切り替えずWi-Fiを維持する |
| USB固定、Wi-Fi/APなし | USB経由で会話できる |
| AUTOのUSB使用中 | Wi-FiおよびESP-NOWが停止している |
| USB切断後にWi-Fi接続が失敗 | 定義した再試行後にエラーを表示し、USB再接続も監視する |
| USBからWi-Fiへ切替 | ESP-NOWをSTA接続後のチャネルで再初期化する |
| 手動でモード変更 | 現セッションを終了し、新規セッションで接続する |
| 切替時に旧TCP切断検出が遅延 | 同一Device IDのFoundryセッションを二重化しない |
| Stack-chanを再起動 | 前回選択したモードを復元する |
| Relay Managerを再起動 | Relay、ブリッジ、USB端末接続が自動復旧する |
| ファームウェアを書き込む | COMを解放して書き込み、完了後にブリッジが復帰する |
| USB通信中にログを出力 | 音声を破損させずManagerでログを確認できる |
| PCブリッジが一時的に読み取り停止 | メインループを停止せず、上限内で復旧または切断する |
| 初回COMオープンで端末が再起動 | 再列挙後にブリッジが自動接続する |
| Wi-Fi固定で長時間会話 | USB追加前と同等に動作する |

## リスクと対策

### USB実効帯域

`115200`はHWCDCの物理帯域制限ではなく、物理帯域には余裕がある。一方、固定機能
USB Serial/JTAGとArduino HWCDCは小さいFIFO/リングバッファを経由するため、ホスト側の
読み取り停止や短いwriteを正しく扱わないと欠損またはメインループ停止が起きる。
専用タスク、拡張バッファ、Sequence/CRC計測を実装し、性能ゲートを通過させる。

### Arduino Core固定版

現在はArduino-ESP32 2.0.17系に固定されている。公式リポジトリにはHWCDC欠損の修正履歴が
あるため、現固定版で性能ゲートを通過できない場合は、場当たり的な再送で隠さずCore更新を
別ブランチで評価する。Core更新時はM5Unified、StackChan-BSP、WebSockets、カメラ、音声、
書き込みをすべて回帰試験する。

### COMポートの排他利用

ブリッジ、PlatformIO、シリアルモニターは同じCOMポートを同時利用できない。
Managerを所有者として明示的な解放・再取得手順を実装する。

### 切替時の音声残留

古い経路の受信キューが新しいセッションで再生されないよう、切替開始時に録音、再生、
送受信キューを順番に停止・破棄する。

### ログによる音声遅延

音声とログに優先度を設け、音声を優先する。ログキューには上限を設け、過負荷時は古い
診断ログを破棄して会話品質を守る。

### 内部RAMとタスク競合

8 MB PSRAMには余裕があるが、USBドライバ、Wi-Fi、FreeRTOSキューの一部は内部RAMを使う。
カメラ推論、画面描画、音声、USB、およびWi-Fi/USB切替の過渡状態で空きヒープと最大連続領域を
測定する。USB用内部RAMは原則32 KB以内とし、性能ゲートの下限を割る構成は採用しない。

### Wi-Fiオンデマンド復帰

AUTOのUSB実経路中はWi-Fiを停止するため、USB会話中のCPU/RF負荷とWi-Fi再試行の干渉は
避けられる。一方、USB切断後はWi-Fi初期化、AP関連付け、WebSocket、Foundry接続を順に行うため、
即時復帰はできない。画面へ復帰段階を表示し、各段階にタイムアウトを設ける。

Wi-Fi接続に失敗してもUSBの再接続監視は継続する。USBが先に復旧した場合はWi-Fi接続試行を
停止し、USBを実経路として再開する。

### Relayセッションの二重化

経路切替で旧WebSocketが残ると、同じ端末に複数のFoundryセッションが生成されうる。
端末側のclose順序だけに依存せず、Relay gatewayでもDevice ID単位に最新接続だけを有効にする。

### 既存Wi-Fi動作への回帰

Wi-Fi実装を削除・置換せず、共通インターフェースの一実装として残す。Wi-Fi固定モードの
自動テストと実機試験を完了条件に含める。

## 完了条件

- 受け入れ試験がすべて成功する。
- USB経路でWi-Fiなしの連続会話が安定して動作する。
- USB性能ゲートをすべて通過し、測定結果が文書化されている。
- USB抜去、PC再起動、端末再起動後に定義どおり復旧する。
- Wi-Fi固定モードで既存機能の回帰がない。
- AUTOのUSB使用中はWi-Fi/ESP-NOWが停止し、USB切断後にWi-Fiがオンデマンド起動する。
- 経路切替時に同一Device IDのFoundryセッションが二重化しない。
- Device Tokenなどの秘密情報がログへ出力されない。
- セットアップ、モード切替、書き込み、障害対応の手順が文書化されている。

## 実装・実機検証結果（2026-08-29）

本計画の基本実装を完了した。CoreS3ファームウェアは内部RAM 116,964 / 327,680 bytes（35.7%）、Flash 1,895,933 / 6,553,600 bytes（28.9%）でビルドでき、COM3のESP32-S3 USB Serial/JTAG経由で実機へ書き込んだ。

実機のAUTO試験では、USB経路を停止するとStack-chanのWi-FiアドレスからRelay WebSocketへ接続し、USBブリッジを復帰すると旧Wi-Fiセッションを閉じてPC内USB経路へ戻った。USB復帰後の計測はCRC/フレーム破損0、Sequence欠損0だった。ブリッジの即時再起動時も、同期用`HOST_CLOSE`から新しいUSBセッションを自動確立することを確認した。

自動テストはPython 60件成功（ローカル`.env`未設定を前提とする既存2件は実環境設定済みのため除外）、Ruffおよび`git diff --check`成功。USBコーデック、分割read、CRC破損からの再同期、ログ混在、Device ID排他、Manager Supervisorを対象に含む。

長時間連続音声、USB抜き差し100回、PCスリープ20回、カメラ同時使用時の空きヒープ計測は耐久試験項目として残る。これらは基本機能の実現可否を妨げるものではないが、リリース判定では本書のGo/No-Go基準を適用する。
