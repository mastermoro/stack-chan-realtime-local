[English](en/development.md)

# 開発ワークフロー

## ブランチ運用

- `main`: 保護された統合ブランチ。
- 機能ブランチ: `agent/<description>` または `feature/<description>`。
- 変更はプルリクエストでマージする。
- 可能な限り、ファームウェア、Relay、インフラストラクチャの変更を個別にレビューできる状態に保つ。

## チェック

Relay:

```bash
cd relay
pip install -e '.[dev]'
ruff check app tests
pytest
```

ファームウェア:

```bash
cd stackchan
pio run
```

## Windows LAN Relay の実行

ローカル Relay 環境では `relay/.venv` と `relay/.env` を使用する。

通常の初回実行では、リポジトリルートから `setup.cmd` を起動する。
ネイティブの Windows セットアップ UI は Python の依存関係をインストールする前でも動作し、
`relay/.env` と、Git 管理対象外の Stack-chan 認証情報ヘッダーの両方を設定する。

```powershell
cd relay
.\setup-windows.ps1 -WithHeadset
.\run-local.ps1 -Reload
```

デフォルトでは `0.0.0.0:8080` で待ち受けるため、信頼できる LAN から Stack-chan が接続できる。
次のコマンドでプロセスをローカルから確認する:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
```

`healthz` は HTTP プロセスが動作中であることを確認する。`readyz` が `ready` を返すのは、
`relay/.env` の `AZURE_OPENAI_ENDPOINT` と 2 つのデプロイ名に実際の値が設定されている場合のみである。
デバイスセッションにはさらに、`AZURE_OPENAI_API_KEY`、またはサインイン済みの Azure CLI セッションなど、
ローカルで利用可能な `DefaultAzureCredential` のいずれかが必要となる。

開発用の直接実行をこの PC のみに制限するには、次を指定する:

```powershell
.\run-local.ps1 -ListenAddress 127.0.0.1 -Reload
```

LAN Relay の場合は、Relay PC からファームウェアをデプロイする:

```powershell
cd stackchan
.\deploy-from-relay.ps1
```

このスクリプトは、優先される物理デフォルトルートの IPv4 アドレスを選択し、Git 管理対象外の
`include/relay_endpoint.hpp` を生成して `/healthz` を確認した後、ファームウェアをビルドして
アップロードする。`include/secrets.hpp` を書き換えることはない。VPN や複数のアダプターによって
ルートが曖昧になる場合は、経路を明示的に選択する:

```powershell
.\deploy-from-relay.ps1 -StackChanAddress 192.168.1.50
.\deploy-from-relay.ps1 -InterfaceAlias "Wi-Fi"
.\deploy-from-relay.ps1 -RelayAddress 192.168.1.10
```

`-StackChanAddress` は、そのデバイスへの到達に使用するローカル送信元アドレスを Windows に問い合わせる。
アプリケーションのペイロードは送信されない。`-ResolveOnly` はファイルを変更せずに選択結果を表示し、
`-BuildOnly` はアップロードせずに生成とビルドを行う。デフォルトは `ws://` である。
`-UseTls` は、デバイスが証明書を信頼する TLS エンドポイントでのみ使用すること。
平文 WebSocket のリスナーを信頼できる LAN の外部に公開してはならない。

## ローカル Relay Manager UI

Manager を起動し、ブラウザーで `http://127.0.0.1:8787` を開く:

```powershell
cd relay
.\run-manager.ps1
```

コード更新後に実行中の Manager を置き換えるには、次を使用する:

```powershell
.\run-manager.ps1 -Restart
```

`-Restart` は、まず既存の Manager に管理対象 Relay の停止を要求する。その後、選択したポート上の
プロセスがこの Relay Manager に属すると確認できた場合に限って、そのプロセスを停止する。
別のアプリケーションを終了することはない。

UI は Relay 子プロセスを起動および停止し、その PID と終了コードを表示するとともに、ローカルの
stdout/stderr ログを追尾する。Manager UI 自体は `127.0.0.1` にバインドされる一方、Relay 子プロセスは、
Stack-chan が LAN から接続できるよう `0.0.0.0:8080` にバインドされる。ステータス表示はローカルの
`http://127.0.0.1:8080/healthz` エンドポイントを確認し、UI が管理する Relay と UI 外部で起動された
Relay を区別する。管理対象 Relay は Manager の終了時または再起動時に停止される。外部 Relay は
その旨が表示され、Manager によって終了されることはない。

同じ UI でヘッドセットテストも利用できる。選択したローカルマイク（またはデフォルトのマイク）から
PCM16 / 24 kHz / mono で音声をキャプチャし、デバイスとしてローカル Relay に送信して、返された音声を
選択した出力デバイスで再生する。新しい環境をセットアップするときは、オプションのローカル依存関係を
インストールする:

```powershell
cd relay
.\.venv\Scripts\python.exe -m pip install -e '.[dev,headset]'
```

このテストには、`relay/.env` に設定済みの Foundry エンドポイント、デプロイ名、デバイストークンが必要だが、
Stack-chan ハードウェアは必要ない。まず `テストを起動` で 1 つの Relay/Realtime セッションを確立し、維持する。
続いて `会話を開始` を押すと、連続した半二重会話が始まる。server VAD が各発話を検出し、Foundry が思考中または
発話中の間はマイク入力が停止し、各応答の完了後に自動的にリスニングへ戻る。`会話を一時停止` では同じセッションと
そのコンテキストが維持される。セッションを終了してよい場合にのみ `テストを停止` を使用すること。

## Stack-chan の操作と UI

CoreS3 ファームウェアは半二重で動作する。Face 画面は通常の会話画面であり、リップシンクを表示し続ける。
表示のちらつきを避けるため、この画面ではタブと診断情報が非表示になる。横方向にスワイプすると Status、Face、
Settings の間を移動できる。Settings ではデバイス側の音量を調整でき、Status では認証情報を表示せずに現在の
ネットワークと Relay の状態を確認できる。

Face 画面をタップして連続会話を制御する。応答のたびに再度タップする必要はない:

- `READY`: ディスプレイをタップして会話とマイクストリーミングを開始する。
- `LISTENING`: タップして会話を終了し、マイクストリーミングを停止する。
- `THINKING`、`SEARCHING`、`SPEAKING`: タップして応答をキャンセルし、会話を終了する。

Face 画面の左上 64×64px の領域はカメラワイプ用に予約されている。タップすると CoreS3 カメラの
ピクチャーインピクチャーを表示して顔追跡を開始する。もう一度タップするとワイプを非表示にし、追跡を停止して、
頭部をホーム位置へ戻す。それ以外の領域では、前述の会話操作が有効である。

### 猫顔と待機動作

猫顔は Face 画面の標準アバターである。Relay の `ui.mode=face` により、会話コンテキストを
維持したまま猫らしい話し方へ切り替える。端末側では会話状態と `emotion` に応じて目、口、
頬、検索中の動きを描画する。

自動待機は、Face 画面で状態が `READY` または `LISTENING`、かつカメラワイプが非表示の
場合だけ動作する。

1. 最後の操作または検出済み発話から30秒後、`LookingAround`へ移り、猫の待機顔を表示する。
2. `LookingAround`では2〜15秒のランダムな間隔で視線と首を動かす。
3. `LookingAround`開始から5分後、首をホーム位置へ戻して`Sleeping`へ移り、閉じた目、呼吸、
	`Z`表示を約700 msごとに更新する。
4. タッチ操作、または`LISTENING`中に音声活動を3フレーム連続で検出すると`Active`へ戻る。
	カメラワイプが非表示なら、首もホーム位置へ戻す。

自動`Sleeping`は表示上の待機状態であり、会話セッションを一時停止しない。上面ダブルタップの
明示的スリープは別操作で、キャプチャと再生を止めて`conversation.pause`を送り、首を低い
スリープ姿勢へ動かす。

CoreS3 のトップタッチセンサーでは、どの UI ページからでも操作できる。1 回タップすると Face を開いて
オーバーレイを非表示にし、Relay 接続中であればリスニングを開始する。2 秒以内にもう一度トップをタップすると、
会話を一時停止し、キャプチャと再生を停止して、頭部をスリープ時のピッチへ動かす。Settings では、音量のマイナスと
プラスの操作は 500 ms 長押しするとリピートする。短くタップした場合は従来どおり音量が 1 段階変わる。

Server VAD が各ユーザー発話の終了を判定する。応答の生成中と音声の再生中はマイクが無効になり、
Relay が `response.done` に続いて `ready` を送信すると自動的に再開する。

## Windows ローカル Function Calling

ローカル Relay は Foundry に `open_browser_url` を登録できる。デフォルトでは無効である。
`relay/.env` で有効化し、必要に応じてホストを制限する:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

このツールは HTTP(S) URL のみを受け付け、PowerShell や任意のコマンドを公開しない。
別の Windows アクションを追加する前に `docs/local-tools.md` を参照すること。

顔の表情は Relay の neutral、happy、sad、angry、surprised、sleepy の各感情イベントに応じて変化する。
Web 検索中は穏やかな検索アニメーションを表示する。Status には Relay 接続通知、簡潔なエラー詳細、
Web 検索で返された最初のタイトルも表示される。接続が失われた場合、Wi-Fi と WebSocket は自動的に再接続する。

## シークレット

次の項目は絶対にコミットしないこと:

- `relay/.env`
- `stackchan/include/secrets.hpp`
- `stackchan/include/relay_endpoint.hpp`
- Azure API キー
- Wi-Fi 認証情報
- デバイストークン
- キャプチャした PCM/WAV ファイル
