# Browser Functionクラッシュ — エージェント引き継ぎ

[日本語](browser-function-crash-handoff.md) | [English](en/browser-function-crash-handoff.md)

最終更新: 2026-08-29 16:12 JST

## 解決

2026-08-29に解決した。Chromiumを管理対象Relayプロセスの子プロセスとして起動すると
クラッシュしていた。Relayから`explorer.exe URL`を呼び出すとクラッシュは回避できたが、
両プロセスがWindows Session 1にあったにもかかわらず、対話型Explorerプロセスには
到達しなかった。対話型PowerShellプロセスから正常に引き渡した場合にも終了コード`1`が
観測されたため、このコードだけではページが開いたことを証明できない。

Windowsのブラウザーアクションは、検証済みのURLをループバック専用Local Managerの
専用エンドポイントへ送るようになった。ManagerはHTTP/HTTPS、資格情報、任意のドメイン
許可リストを再検査し、対話型プロセスのコンテキストから`explorer.exe URL`を実行する。
このエンドポイントはコマンドや実行ファイルの引数を受け付けない。

ManagerとRelayの両方を再起動した後、次を確認した。

- Managerブローカーの直接試験では、アプリケーションエラーダイアログなしに、既存の
   Chromeプロファイルで`https://example.com/`が開いた。
- USBブリッジを完全に停止した状態で、Wi-Fi経由のFunction呼び出しを3回連続で行い、
   いずれもダイアログなしで正常に開いた。
- StackchanをUSBモードへ切り替え、ブリッジがCOM3で`relay_connected=true`を報告した。
- USB経由のFunction呼び出しを3回連続で行い、いずれもダイアログなしで正常に開いた。
- 対象を絞った自動テストは13件成功、Relayの全テストスイートは69件成功した。変更した
   Pythonファイル4件すべてでRuffが成功し、`git diff --check`も成功した。

以降の節には、当初の障害証跡と調査履歴を残す。

## 目的

Stackchanが要求したURLを、アプリケーションエラーダイアログなしに対話ユーザーの既定の
Chromeセッションで開けるよう、WindowsのブラウザーFunctionを修正する。この修正は
Wi-FiとUSBの両Relayトランスポートで動作し、URL/ドメインのセキュリティ検査を維持する
必要がある。

ワークツリーをリセットまたはクリーンアップしない。USBモード実装とその後の修正は
現在コミットされていない。現在のGit `HEAD`は`a151dcd`である。

## 当時の障害

FunctionがChromiumプロセスを起動してモデルへ成功を報告した後、Windowsに次が表示された。

```text
chrome.exe - Application Error
The exception unknown software exception (0x80000003) occurred in the application
at location 0x00007FF9023D9D60.
```

ユーザーに表示された日本語ダイアログも同じ値だった。`0x80000003`はブレークポイント例外
である。障害プロセスはモーダルエラーダイアログの背後で生存し続けるため、
`Popen.pid > 0`は有効な成功判定ではない。

それ以前の再現では、`msedge.exe`でも同じ例外クラスが発生した。Chrome自体に固有の問題とは
判明しておらず、インストールされていた両ブラウザーはいずれもChromiumベースである。

再現時にインストールされていたバージョン:

- Google Chrome: `151.0.7922.175`
- Microsoft Edge: `151.0.4129.107`

## Wi-Fi/USB切り分け結果

確認済みのWi-Fi WebSocket経路でも障害が再現したため、発生条件にUSBフレーミングや音声転送は
必要ない。

当時のWi-Fi試験構成:

- Relay PCのIPv4: `192.168.1.107`
- StackchanのIPv4: `192.168.1.105`
- ファームウェアへコンパイルされたRelayエンドポイント: `ws://192.168.1.107:8080/v1/realtime`
- Relayログ: `192.168.1.105:57615 - "WebSocket /v1/realtime" [accepted]`
- ファームウェアログ: `Relay transport selected: Wi-Fi`、続いて`Relay WebSocket connected`
- Wi-Fi再現中のUSBブリッジ状態: `ready`、`relay_connected=false`

直近のWi-Fi再現時のログ:

```text
2026-08-29 15:50:28,752 INFO app.tools.local_actions
browser launch requested executable=chrome.exe pid=7052
2026-08-29 15:50:28,752 INFO app.tools.local_actions
local browser opened host=tenki.jp
```

その後、ユーザーがChromeの`0x80000003`ダイアログを確認した。ネイティブ例外ダイアログが
まだ閉じられていなかったため、PID 7052はメインウィンドウタイトルがないまま
`Responding=True`だった。

重要な未実施の切り分け試験として、端末データ経路はWi-Fiだったが、USBブリッジプロセスは
動作したままCOM3を探索していた。USBブリッジプロセスが影響しないと結論づける前に、
Manager APIで完全に停止してもう一度再現する必要があった。

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/usb/stop
```

必要に応じて、その後に次で再起動する。

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/usb/start
```

## ブラウザー実装の履歴

`HEAD`にコミットされているUSB実装前の処理は次だけである。

```python
@staticmethod
def _open_browser(url: str) -> bool:
    return webbrowser.open(url, new=2, autoraise=True)
```

関連ファイル: `relay/app/tools/local_actions.py`。

この実装は当時のWindows環境でEdgeを選択し、Edgeのアプリケーションエラーダイアログを
発生させた。ユーザーによると既定のブラウザーはChromeだったが、このプロセスは次を
読み取れなかった。

```text
HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice
```

`HKCR\ChromeHTML\shell\open\command`は読み取り可能で、内容は次だった。

```text
"C:\Program Files\Google\Chrome\Application\chrome.exe" --single-argument %1
```

その後、ローカルの未コミット回避策で`_windows_default_browser_executable()`を追加し、
Chromeを直接起動した。次の2案を試した。

1. `CREATE_NEW_PROCESS_GROUP`付きの`chrome.exe --new-window URL`: Chromeがクラッシュした。
2. `CREATE_NEW_PROCESS_GROUP`なしの`chrome.exe --single-argument URL`: コマンドラインからの
   直接スモークテストでは正常に引き渡したように見えたが、実際のFunction呼び出しでは
   引き続きChromeがクラッシュした。

Chromeを直接選択した後の実際のFunction再現:

```text
15:41:24 host=weathernews.jp pid=28312  # USB path, Chrome error
15:44:45 host=www.google.com pid=10384  # USB path, Chrome error after variant 2
15:50:28 host=tenki.jp pid=7052         # confirmed Wi-Fi path, Chrome error
```

したがって、現在の実行ファイル直接起動による回避策を修正済みとは扱わない。編集前に
`HEAD`と比較する。

```powershell
git diff HEAD -- relay/app/tools/local_actions.py
git show HEAD:relay/app/tools/local_actions.py
```

## プロセスコンテキスト

Managerは`relay/local_manager.py`から`subprocess.Popen()`でRelayを起動する。その後Relayは
`asyncio.to_thread()`内で`_open_browser()`を呼び出す。

このマシンでは、仮想環境ランチャーによりサービスごとに2つのPythonプロセスエントリが
生成される。

- `relay/.venv/Scripts/python.exe`
- the underlying `C:/Users/user/.platformio/python3/python.exe`

想定されるサービスの組はManager、Relay、USBブリッジの3つである。親プロセスと
コマンドライン情報を確認せず、これらの組をRelayの重複インスタンスと判断しない。

Relayプロセスはstdout/stderrを次へリダイレクトする。

```text
%TEMP%\stackchan-relay\relay.log
```

USBブリッジのログは次にある。

```text
%TEMP%\stackchan-relay\usb-bridge.log
```

Relayランチャー自体はUSB対応で実質的に変更されておらず、USB対応では兄弟ブリッジ
プロセスとManagerによる監督が追加された。そのため、トランスポート上でのURL破損より、
プロセスコンテキスト、Chromiumの状態/更新、注入モジュール、または追加ブリッジプロセス
との相互作用の可能性が高いと考えられた。

## Relayが誤って成功を報告していた理由

`LocalActionExecutor.execute()`は`_open_browser()`がtrueを返すと起動成功と判断する。当時の
実装は`subprocess.Popen()`から正のPIDが得られると直ちにtrueを返していた。起動完了を待たず、
終了コードの確認もネイティブ例外ダイアログの検出も行わない。そのため、Windowsが直後に
エラーを表示してもRelayログには`local browser opened`と記録された。

## 推奨していた調査順序

1. 変更のあるワークツリーを維持し、現在の端末が引き続きWi-Fiを使用していることを確認する。
2. USBブリッジプロセスを完全に停止し、Wi-Fi経由でFunctionを一度再現する。
3. エラーダイアログを閉じてから、Application Error / Windows Error Reportingイベントを
   照会する。モーダルダイアログが有効な間はイベントが空だったため、閉じた後に収集する。
4. 許可される場合は、障害が起きたChrome PIDのダンプ/モジュール一覧を収集する。通常の
   ワークスペースサンドボックスではChrome Crashpadのパスを読み取れず、承認が必要な
   可能性がある。
5. 同じURLについて、次の起動コンテキストを比較する。
   - 対話型PowerShell/Pythonプロセス
   - 管理対象Relay Function
   - Windows `ShellExecute`/Explorerへの引き渡し
   - 必要な場合は、小規模な対話ユーザー用ブラウザーブローカープロセス
6. 障害がRelayの親/ジョブ/環境に追随するのか、単にChromium 151と同時期に発生したのかを
   判断する。承認済み対話ツールコマンドから直接実行した場合は直ちには再現しなかったが、
   実際のRelay Functionでは一貫して再現した。
7. PIDを得ただけでFunctionが`opened`を返さないよう、起動/エラー検出を実装する。

Relayから生成した場合に限ってChromiumが一貫してクラッシュするなら、専用ブラウザー
ブローカーは妥当な代替アーキテクチャである。対話デスクトップのコンテキストでブローカーを
起動し、限定的なローカルIPCチャネルを介して検証済みHTTP/HTTPS URLだけを送る。任意の
コマンド実行は追加しない。

## 維持するセキュリティ制約

- URLは`http`と`https`だけを許可する。
- URLに埋め込まれた資格情報を拒否する。
- 任意設定の許可ドメイン制約を維持する。
- モデルが与えたURLから構築したシェルコマンドを実行しない。
- `.env`、`stackchan/include/secrets.hpp`、トークン、Wi-Fi資格情報をログや引き継ぎメモに
  露出させない。

## 関連ファイル

- `relay/app/tools/local_actions.py` — ブラウザーFunctionと当時の回避策
- `relay/tests/test_local_actions.py` — Function検証/ランチャーテスト
- `relay/app/session/orchestrator.py` — Functionディスパッチと結果処理
- `relay/local_manager.py` — RelayとUSBブリッジのプロセス監督
- `relay/app/usb/bridge.py` — 兄弟USBブリッジプロセス
- `relay/app/gateway/websocket.py` — トランスポート非依存の端末WebSocketエントリポイント
- `docs/usb-relay-mode-plan.md` — USB機能計画と実装メモ

## テストと運用上の注意

ローカル`.env`ではブラウザーアクションが有効なため、既定で無効と仮定するテストには
明示的な環境変数の上書きが必要である。

```powershell
cd relay
$env:LOCAL_BROWSER_TOOL_ENABLED = 'false'
.\.venv\Scripts\python.exe -m pytest tests/test_local_actions.py -q --basetemp=.pytest-tmp-browser
.\.venv\Scripts\python.exe -m ruff check app/tools/local_actions.py tests/test_local_actions.py
```

当時のランチャーテストは成功していたが、`Popen`をモックして引数の構築だけを確認するため、
Windowsネイティブの障害は対象外だった。

Relayコードの変更後は、新しいモジュールを読み込むためManager経由でRelayを再起動する。

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/stop
Invoke-RestMethod -Method Post http://127.0.0.1:8787/api/start
```

## 受け入れ条件

- Wi-Fi経由でブラウザーFunctionを3回連続で呼び出し、Chrome/Edgeのアプリケーションエラー
   ダイアログなしに要求したURLが開く。
- USB経由でも3回連続の呼び出しが同様に成功する。
- Functionが意図した対話型Chromeプロファイル/セッションを使用する。
- クラッシュして孤立したChromiumプロセスが残らない。
- 起動失敗を`status=opened`ではなくFunctionエラーとして報告する。
- 既存のURL検証/ドメイン制約と関連するすべての自動テストが維持される。
