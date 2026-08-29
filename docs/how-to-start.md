**日本語** | [English](en/how-to-start.md)

# Stack-chan 利用開始マニュアル

このマニュアルでは、Stack-chan（M5Stack CoreS3）を初めて Windows PC にUSB接続してから、Microsoft Foundry の設定、ファームウェア書き込み、音声会話の開始までを順番に説明します。

## 1. 全体構成

標準構成では Relay を同じLAN内の Windows PC で実行します。Azure Container Apps、コンテナーレジストリ、独自サーバーは必要ありません。

必要なクラウドリソースは、次の2つのモデルデプロイを持つ Microsoft Foundry / Azure OpenAI リソースです。

- 音声会話用の Realtime 対応モデル
- Web検索用の Responses API 対応モデル

Stack-chan はUSBまたはWi-FiでWindows PCのRelayへ接続し、RelayだけがAzureへ接続します。

## 2. 用意するもの

- M5Stack CoreS3を使用したStack-chan
- データ通信対応のUSB Type-Cケーブル
- Windows 10またはWindows 11のPC
- PCとStack-chanが接続できる2.4 GHz Wi-Fi/LAN
- Microsoft Azureサブスクリプション
- このリポジトリを展開またはcloneしたフォルダー

> **Wi-Fiのハードウェア制限:** CoreS3に搭載されているESP32-S3は2.4 GHz帯のWi-Fiのみ対応し、5 GHz帯には接続できません。2.4 GHzと5 GHzでSSIDが分かれている場合は、必ず2.4 GHz側のSSIDを使用してください。5 GHz専用に設定されたアクセスポイントではWi-Fi接続を利用できません。

Windows PCには約3 GB以上の空き容量を確保してください。Python、PlatformIO、ESP32のビルドツールをセットアップ中に導入します。

## 3. Azureリソースを準備する

### 3.1 AIリソースを作成する

Azure PortalまたはMicrosoft Foundryポータルで、Azure OpenAIを利用できるFoundryリソースを1つ作成します。モデルの提供リージョンや利用可能なバージョンはサブスクリプションによって異なるため、RealtimeモデルとResponses対応モデルの両方をデプロイできるリージョンを選択してください。

リソース作成後、「Keys and Endpoint」または同等の画面から次を確認します。

- **Endpoint**: GUIの「Foundry エンドポイント」に入力するHTTPS URL
- **Key**: APIキー方式を使う場合にGUIへ入力するキー

Endpointはポータルに表示された値をそのまま使用します。例:

```text
https://my-resource.openai.azure.com
https://my-resource.services.ai.azure.com
```

### 3.2 モデルを2つデプロイする

Foundryのモデルカタログまたはデプロイ画面で次を作成します。

| 用途 | 必要なモデル | このリポジトリの既定デプロイ名 |
|---|---|---|
| 音声会話 | Realtime API対応モデル | `gpt-realtime-2.1` |
| Web検索 | Responses APIとWeb検索に対応するモデル | `gpt-5.6-terra` |

GUIへ入力する値は、モデルの表示名ではなく**作成したデプロイ名**です。別の名前でデプロイした場合は、その名前を入力してください。モデル名やバージョンの提供状況が変わった場合も、同じ能力を持つ利用可能なモデルを選び、そのデプロイ名を使用します。

### 3.3 認証方法を選ぶ

初回セットアップではAPIキー方式が簡単です。

- **APIキー方式**: 「APIキー」にリソースのキーを入力します。
- **Azureログイン方式**: APIキーを空欄にし、Azure CLIで`az login`を実行します。ログインしたユーザーには対象AIリソースで推論を実行できるロール（例: Foundry User）が必要です。

APIキーやアクセストークンをREADME、ソースコード、Git管理対象ファイルへ書かないでください。セットアップGUIはAPIキーをGit管理対象外の`relay/.env`へ保存します。

## 4. Stack-chanを初めてUSB接続する

1. Stack-chanの電源を切ります。
2. データ通信対応USB Type-CケーブルでCoreS3をWindows PCへ直接接続します。
3. Stack-chanの電源を入れます。
4. Windowsの「デバイス マネージャー」を開きます。
5. 「ポート (COM と LPT)」に`USB シリアル デバイス (COMx)`が表示されることを確認します。

USBハブ経由で認識が不安定な場合は、PC本体のUSBポートへ直接接続してください。COMポートが表示されない場合は、充電専用ではないケーブルへ交換し、別のUSBポートも試します。

## 5. セットアップGUIを開く

リポジトリ直下の[setup.cmd](../setup.cmd)をダブルクリックします。Windowsの警告が表示された場合は、ファイルの場所と発行元を確認してから実行してください。

### 5.1 プログラム

「1. プログラム」タブで状態を確認し、「必要項目をインストール」を押します。

- Python 3.11以上
- Relay Pythonパッケージ
- PlatformIO
- Git（clone後の実行だけなら必須ではありません）
- Azure CLI（APIキー方式では任意）

「PCヘッドセット・テストもインストール」は、PCのマイクとヘッドセットでRelayを確認したい場合に有効にします。2回目以降は依存関係に変更がなければ再インストールを省略します。

### 5.2 接続設定

「2. 接続設定」タブへ次を入力します。

| GUIの項目 | 入力する値 |
|---|---|
| Foundry エンドポイント | Azure/Foundryポータルに表示されたEndpoint全体 |
| Realtime デプロイ名 | 音声会話用に作成したデプロイ名 |
| Responses デプロイ名 | Web検索用に作成したデプロイ名 |
| APIキー | APIキー方式ではリソースキー。Azureログイン方式では空欄 |
| Web検索の国 | 通常は`JP` |
| タイムゾーン | 日本では`Asia/Tokyo` |
| 音声によるブラウザ起動 | Stack-chanからPCのブラウザを開く機能を使う場合だけ有効化 |
| ブラウザ許可ドメイン | 許可するホストをカンマ区切りで指定 |

ブラウザ許可ドメインを空欄にすると任意のHTTP(S)ホストが対象になります。最初は`microsoft.com,github.com`のように必要なドメインだけを指定してください。

### 5.3 Stack-chan

「3. Stack-chan」タブへ次を入力します。

| GUIの項目 | 入力する値 |
|---|---|
| 端末ID | 端末を識別する名前。通常は`stackchan-001` |
| 端末トークン | 「安全なトークンを生成」で作成した値 |
| Wi-Fi SSID | Stack-chanが接続する2.4 GHz Wi-FiのSSID。5 GHz専用SSIDは使用不可 |
| Wi-Fi パスワード | そのWi-Fiのパスワード |

端末IDと端末トークンはRelay側とファームウェア側へ同じ値が保存されます。端末トークンを第三者へ共有しないでください。

## 6. 保存してManagerとRelayを起動する

1. 「保存してManagerを起動」を押します。
2. ブラウザーで`http://127.0.0.1:8787`が開くことを確認します。
3. Manager画面で「Relayを開始」を押します。
4. 状態が「稼働中（管理中）」になることを確認します。

初回はWindows Defender Firewallの確認が表示される場合があります。自宅など信頼できる**プライベートネットワークだけ**を許可してください。Relayのポート8080をインターネットへ転送しないでください。

Relayを開始するとUSBブリッジも起動します。CoreS3が対応ファームウェアで動作していれば、ManagerのUSBブリッジ欄にCOMポートと端末IDが表示されます。

## 7. ファームウェアを書き込む

1. CoreS3がUSB接続されていることを確認します。
2. セットアップGUIの「ファームウェアを書き込む」を押します。
3. 開いたPowerShellでビルドと書き込みが完了するまで待ちます。
4. `Stack-chan deployment completed.`と表示されることを確認します。
5. 書き込み後、CoreS3が自動的に再起動するのを待ちます。

書き込み処理はRelay PCのLANアドレスを自動選択し、初期Relay endpointをファームウェアへ設定します。複数のLANアダプターやVPNがあり自動選択に失敗する場合は、PowerShellで明示します。

```powershell
cd stackchan
.\deploy-from-relay.ps1 -InterfaceAlias "Wi-Fi"

# またはRelay PCのIPv4アドレスを直接指定
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10

# COMポートも指定する場合
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10 -UploadPort COM3
```

## 8. Stack-chanの接続を確認する

書き込み後、画面上部に次の4タブが表示されます。

- `STATUS`: 接続状態、Stack-chanのIPアドレス、Relay endpoint
- `FACE`: 会話と表情表示
- `NETWORK`: USB/Wi-Fi経路とRelay endpoint
- `SETTINGS`: 音量とESP-NOWリモコン設定

`NETWORK`タブでは接続方法を選べます。

- `AUTO`: USBを優先し、USBがなければWi-Fiへ切り替える推奨設定
- `WI-FI`: Wi-Fiだけを使う
- `USB`: USBだけを使う

Wi-FiのRelay接続先を変更するには、`NETWORK`の`Relay endpoint`をタップします。IPv4アドレスの各欄またはポートを選択し、`-` / `+`で変更して`SAVE`を押します。値は本体へ保存され、Wi-Fi接続が張り直されます。保存しない場合は`CANCEL`を押します。

正常時はManagerのRelayログに端末のWebSocket接続が記録され、USB使用時はUSBブリッジが「Relay中継中」になります。

## 9. 会話を開始する

1. Stack-chanで`FACE`タブを開きます。
2. 画面をタップします。
3. `LISTENING`になったら話しかけます。
4. 応答の再生が終わると、次の発話を待ち受けます。
5. 会話中にもう一度タップすると一時停止します。

CoreS3上面を1回タップしてもFace画面へ移動して会話を開始できます。上面を2秒以内に2回タップすると、会話と再生を止めてスリープ姿勢へ移ります。

## 10. 終了する

Manager画面下部の「Managerを終了」を押します。管理中のヘッドセット・テスト、USBブリッジ、Relayを順に停止してからManager自体を終了します。

Stack-chanの電源を切る場合は、会話を一時停止してから電源を切ってください。

## 11. 困ったとき

### COMポートが見つからない

- データ通信対応のUSBケーブルか確認する
- USBハブを外してPCへ直接接続する
- CoreS3の電源を入れ直す
- デバイス マネージャーでUSBシリアルデバイスを確認する
- ManagerのUSBブリッジまたはシリアルモニターがCOMポートを使用中なら停止する

### Relay health checkに失敗する

- ManagerでRelayを開始したか確認する
- `http://127.0.0.1:8787`の状態とRelayログを確認する
- VPNや複数NICがある場合は`-InterfaceAlias`または`-RelayAddress`を指定する
- Windows Firewallでプライベートネットワーク上のPython/Relay通信を許可する

### Azureから401または403が返る

- Endpointが対象リソースの値と一致しているか確認する
- APIキー方式ではキーを再確認する
- Azureログイン方式では`az login`のアカウント、テナント、サブスクリプションを確認する
- ログインユーザーに対象AIリソースの推論ロールがあるか確認する

### モデルが見つからない

GUIへモデル名ではなく**デプロイ名**を入力したか確認します。Realtime用とResponses用を逆に入力していないかも確認してください。

### Stack-chanがRelayへ接続しない

- `STATUS`のRelayアドレスがWindows PCのLAN IPv4アドレスか確認する
- `NETWORK`を一度`USB`または`WI-FI`へ切り替えて接続経路を限定する
- Wi-Fi利用時はSSID、パスワードを確認し、5 GHz専用SSIDではなく2.4 GHz側を指定する
- 端末IDと端末トークンを変更した場合はファームウェアを再度書き込む
- ManagerのRelayログとUSBブリッジログを確認する

## 利用開始チェックリスト

- [ ] CoreS3がWindowsでCOMポートとして認識される
- [ ] Foundryエンドポイントを取得した
- [ ] Realtime用デプロイを作成した
- [ ] Responses/Web検索用デプロイを作成した
- [ ] APIキーまたはAzureログインを準備した
- [ ] setup GUIで依存関係と接続情報を保存した
- [ ] ManagerでRelayが「稼働中（管理中）」になった
- [ ] ファームウェア書き込みが完了した
- [ ] Stack-chanの`STATUS`またはManagerログで接続を確認した
- [ ] `FACE`画面から会話できた

詳細な運用・開発情報は[README](../README.md)、[開発ガイド](development.md)、[アーキテクチャ](architecture.md)を参照してください。
