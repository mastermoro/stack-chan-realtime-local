# stack-chan-realtime-local

日本語 | [English](README.en.md)

Stack-chanから同じローカルネットワーク上のWindows PCへ接続し、そのPC上のRelayを経由してMicrosoft Foundry Realtime APIを利用する音声エージェントです。

初めてセットアップする場合は、[Stack-chan 利用開始マニュアル](docs/how-to-start.md)に沿ってAzureリソースの準備、USB接続、設定、ファームウェア書き込みを進めてください。

元の[`mastermoro/stack-chan-realtime`](https://github.com/mastermoro/stack-chan-realtime)をベースに、Relayの主な実行場所をAzure Container AppsからWindows PCへ移しました。Foundry Realtime、Responses APIのWeb検索、Stack-chanファームウェアは維持しています。

## 構成

```text
Stack-chan (CoreS3)
  ├─ PCM16 / 24 kHz / mono
  ├─ Wi-Fi WebSocket ───────────────────┐
  └─ USBシリアル ─> Windows USBブリッジ ─┤
                                        v
Windows PC / Local Relay
  ├─ 端末認証・会話状態管理
  ├─ Foundry Realtime接続
  ├─ Responses API web_search
  └─ 登録制のローカルFunction
       └─ open_browser_url -> 既定ブラウザ
                 |
                 v
Microsoft Foundry
  ├─ Realtime model
  └─ Responses model + web_search
```

Windows側のFunctionは明示的に登録したものだけ実行します。任意のPowerShell、コマンド、ファイル操作をモデルへ公開しません。

## Windowsでの開始

前提:

- Windows 10または11
- Microsoft Foundry / Azure OpenAIリソース
- Realtime用とResponses用のモデルデプロイ
- APIキー、または`DefaultAzureCredential`で利用できるAzureログイン

リポジトリをクローンしたら、ルートにある`setup.cmd`をダブルクリックします。

セットアップGUIでは次をまとめて行えます。

- Python 3.11以上、Relay依存パッケージ、PlatformIOの確認とインストール
- Foundryのエンドポイント、デプロイ名、APIキーなどの入力
- Stack-chanのWi-Fi、端末ID、端末トークンの入力と安全なトークン生成
- Relay Managerの起動
- USB接続したStack-chanへのファームウェア書き込み

Pythonが未導入の場合は、Windowsの`winget`を使ってPython 3.12をユーザー領域へ導入します。APIキーを使わずAzure CLI認証を使う場合、Azure CLI自体は任意項目としてGUIに状態が表示されます。
2回目以降は、`pyproject.toml`とインストール項目に変更がなく依存関係が正常なら、パッケージの再インストールを省略します。強制的に更新する場合は`relay\setup-windows.ps1 -WithHeadset -Force`を実行します。Managerが既に動作している場合、「保存してManagerを起動」は二重起動せず既存の画面を開きます。

コマンドラインでセットアップする場合は従来どおり次を実行できます。

```powershell
cd relay
.\setup-windows.ps1 -WithHeadset
notepad .env
.\run-manager.ps1
```

ブラウザで`http://127.0.0.1:8787`を開き、Relayを起動します。Manager UIはPC内だけに公開され、Stack-chan用RelayはLAN向けに`0.0.0.0:8080`で待ち受けます。
Relayと同時にUSBブリッジも起動し、EspressifのUSB COMポートを自動検出します。
終了するときはManager UI下部の「Managerを終了」を押します。管理中のRelay、USBブリッジ、ヘッドセット・テストを停止してからManager自体を終了します。

`relay/.env`の最低限の設定:

```dotenv
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AZURE_OPENAI_REALTIME_DEPLOYMENT=gpt-realtime-2.1
AZURE_OPENAI_RESPONSES_DEPLOYMENT=gpt-5.6-terra
AZURE_OPENAI_API_KEY=

DEVICE_TOKENS_JSON={"stackchan-001":"長いランダムな端末トークン"}
```

ローカルPCのブラウザを音声で開けるようにする場合:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

許可ドメインを空にすると、任意のHTTP(S)ホストを開けます。最初は必要なドメインだけを列挙することを推奨します。

## Stack-chanの設定

初回だけ`stackchan/include/secrets.example.hpp`を`secrets.hpp`へコピーし、Wi-Fi資格情報と、`relay/.env`の`DEVICE_TOKENS_JSON`に登録したものと同じ端末トークンを設定します。`setup-windows.ps1`を使った場合はファイルが作成済みです。

```cpp
#define WIFI_SSID "..."
#define WIFI_PASSWORD "..."
#define DEVICE_ID "stackchan-001"
#define DEVICE_TOKEN "長いランダムな端末トークン"
```

RelayをManager UIで起動し、CoreS3をUSB接続して、Relay PC上で次を実行します。

```powershell
cd stackchan
.\deploy-from-relay.ps1
```

このスクリプトはWindowsの有効なデフォルト経路からLAN IPv4アドレスを選び、コミット対象外の`include/relay_endpoint.hpp`を生成してから、PlatformIOでビルド・書き込みします。Wi-Fi資格情報と端末トークンのファイルは上書きしません。書き込み前に`http://選択したIP:8080/healthz`も確認します。

複数NIC、VPN、特殊なネットワーク構成では選択方法を指定できます。

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

選択結果だけを安全に確認するには`-ResolveOnly`を使います。Relayをまだ起動していない状態で書き込む場合だけ`-SkipRelayCheck`を指定できます。

Windows Defender Firewallの確認が表示された場合は、信頼するプライベートネットワークだけを許可してください。ポート8080をインターネットへ転送しないでください。

## USB / Wi-Fiモード

Stack-chanの`NETWORK`タブにある`AUTO`、`WI-FI`、`USB`で接続経路を選択できます。選択は本体へ保存され、再起動後も維持されます。
同じタブの`Relay endpoint`をタップすると、Wi-Fi接続先のIPv4アドレスとポートを変更できます。対象フィールドをタップして`-` / `+`で値を調整し、`SAVE`を押すと本体へ保存してWi-Fi接続を張り直します。

- `AUTO`: 起動時にUSBを3秒待ち、利用できなければWi-Fiへ接続します。USB使用中はWi-FiとESP-NOWを停止し、USBが切れた時点でWi-Fiを起動します。Wi-Fiで会話中にUSBが見つかった場合は、応答終了後に切り替えます。
- `WI-FI`: 従来のWi-Fi接続だけを使用し、USBへ自動退避しません。
- `USB`: USB接続だけを使用し、ケーブルが抜けてもWi-Fiへ切り替えません。USB中はESP-NOWリモコンを使用できません。

経路を手動変更すると進行中の会話とコンテキストは終了し、新しい接続でセッションを開始します。ManagerのUSBブリッジ欄では、COMポート、端末ID、接続状態、破損フレームや連番欠損の統計、端末ログを確認できます。

ファームウェア書き込み時は`deploy-from-relay.ps1`がManager管理下のUSBブリッジを一時停止してCOMポートを解放し、終了後に自動再開します。手動でPlatformIOの書き込みやシリアルモニターを使う場合は、Managerの「ブリッジを停止」を先に押してください。

設計判断、ハードウェア検証条件、受け入れ試験は[`docs/usb-relay-mode-plan.md`](docs/usb-relay-mode-plan.md)に記録しています。

## Stack-chanの操作

- Face画面をタップすると会話を開始し、会話中にもう一度タップすると一時停止します。
- Face画面で待機中に30秒間操作や発話がないと、猫顔で周囲を見回します。その状態が
  5分続くと睡眠表示へ移り、タッチまたは発話で通常表示へ戻ります。
- CoreS3上面を1回タップするとFace画面へ移動して会話を開始します。
- CoreS3上面を2秒以内に2回タップすると会話と再生を止め、首を伏せたスリープ姿勢へ移ります。
- Settingsの音量`-` / `+`はタップで1段階、長押しで連続調整できます。
- Face画面左上のカメラ領域をタップすると、顔追跡の表示と停止を切り替えます。

## ブラウザFunction

ユーザーが「Windows PCでこのページを開いて」と明示的に依頼すると、Foundryは`open_browser_url`を呼び出せます。Relayは次を検査してからWindowsの既定ブラウザへ渡します。

- 機能が設定で有効か
- `http://`または`https://`か
- URLにユーザー名・パスワードが埋め込まれていないか
- 許可ドメイン内か

実装と拡張方法は[`docs/local-tools.md`](docs/local-tools.md)を参照してください。

## AEC・全二重化

CoreS3側AECに先立つ診断、固定リング、再生同期、状態管理、半二重フォールバックの設計は[`docs/aec-roadmap.md`](docs/aec-roadmap.md)にまとめています。依存固定と半二重の安全な基準ビルドまではこのリポジトリへ反映済みです。全二重I2SとESP-SR AECは実機ゲートを通して段階的に有効化します。

## 検査

```powershell
cd relay
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest

Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:8080/capabilities
```

## リポジトリ構成

```text
stackchan/  CoreS3ファームウェア
relay/      Windows LAN Relay、Manager UI、ローカルFunction
docs/       設計、プロトコル、運用
infra/      元リポジトリ由来の任意のAzure Container Apps構成
```

`infra/`は互換性と将来の選択肢のため残していますが、この派生版の標準運用では使用しません。

## セキュリティ

- `relay/.env`、`secrets.hpp`、生成された`relay_endpoint.hpp`、APIキー、端末トークンをコミットしない。
- RelayとStack-chan間の`ws://`は信頼できるLAN内だけで使う。
- Manager UIのポート8787は`127.0.0.1`から変更しない。
- ローカルFunctionは登録制にし、汎用shell実行を追加しない。
- ブラウザFunctionは必要なドメインだけ許可する。
- 公衆Wi-Fi、ゲストLAN、インターネット公開ではTLSと追加のアクセス制御を使用する。
