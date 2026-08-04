# docs — KoT Alert / 勤怠ボード 開発ドキュメント

このシステムを触るClaude/人が最初に読むための背景資料です。
（コードの詳細仕様・全変更履歴はリポジトリ直下の `CLAUDE.md`、引き継ぎ手順は `ONBOARDING.md`）

## このシステムは何か
King of Time（KoT）の打刻データをもとに LINE WORKS へ出退勤アラートを自動送信し、
全社の勤怠状況をモニタ表示する「勤怠ボード」を提供する Flask アプリ。

- 本番: さくらVPS `ubuntu@133.125.93.39`（Nginx→Gunicorn→Flask, systemd `kot-alert`）
- 公開URL: https://133-125-93-39.sslip.io/ （管理画面） ・ /board （NeeSa勤怠ボード）
- DB: Supabase `aujxtiyvdywabtnkvswm`

## ドキュメント一覧
- **[lineworks-auth.md](lineworks-auth.md)** — LINE WORKS の認証モデル（Service Account と OAuth の違い）。
  「サーバー間認証」と「ユーザー本人ログイン」の混同を解くための必読資料。
- **[lineworks-calendar.md](lineworks-calendar.md)** — カレンダーAPIの使い方・カレンダー一覧（ID）・
  シフトの記入形式（メイン／@121）・繰り返し(RRULE)の扱い・再利用できる関数。
- **[attendance-board.md](attendance-board.md)** — 勤怠ボードのデータフロー・マッピング設定・
  KoT色付け（KoT 2アカウント・禁止時間帯）・同姓/旧姓の扱い。

## 関連コード（リポジトリ直下）
- `neesa_lw.py` — NeeSa LINE WORKS カレンダー読取＋ボードのマッピング設定
- `neesa_kot.py` — NeeSa KoT（打刻）→ ボードの色付け
- `kot_api.py` / `lw_api.py` — トレコレ側 KoT / LINE WORKS Bot クライアント
- `app.py` — Flask（`/`, `/board`, `/staff`, `/my` など）
- `templates/board.html` `dashboard.html` `base.html`
