[English](en/local-tools.md)

# Windows ローカルツール

Relay は、少数の許可リストに登録された Windows ローカルアクションを Foundry Realtime Function Calling に
公開できる。コマンドシェルは公開しない。

## ブラウザーツール

`relay/.env` で有効にする:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

登録される関数は次のとおり:

```json
{
  "name": "open_browser_url",
  "arguments": {
    "url": "https://learn.microsoft.com/"
  }
}
```

Relay は URL を検証し、ループバックのみで動作する Local Manager に送信する。
Manager は同じ検証を繰り返した後、URL を対話型の Windows Explorer セッションに渡し、
ユーザーのデフォルトブラウザーとプロファイルで開く。Manager と Relay の両方が実行中でなければならない。
受け付けるのは HTTP と HTTPS のみである。認証情報を含む URL は拒否される。
許可リストが設定されている場合は、完全一致するドメインとそのサブドメインが許可される。

## 別のローカルアクションの追加

各機能を名前付き実装として `relay/app/tools/local_actions.py` に追加する:

1. 対象を限定した JSON スキーマを `tool_definitions()` に追加する。
2. 名前を `supports()` に追加する。
3. 副作用を発生させる前に、すべての引数を検証する。
4. 小さな JSON シリアライズ可能な結果を返す。
5. 成功、拒否、無効状態のテストを追加する。
6. 個別の環境変数スイッチを追加し、デフォルトは無効のままにする。

汎用の `run_command`、`powershell`、`open_file`、または任意の Python 評価関数を追加してはならない。
次のような用途特化型のアクションを優先する:

- 許可リストに登録されたダッシュボードを開く
- 固定されたローカルステータスページを表示する
- 特定のホームオートメーションエンドポイントを操作する
- 専用 API を通じてリマインダーを作成する

メッセージの送信、商品の購入、データの削除、セキュリティ設定の変更、ローカルファイルの公開を行うアクションは、
モデルから利用可能にする前に、明示的な確認の仕組みを設計する必要がある。
