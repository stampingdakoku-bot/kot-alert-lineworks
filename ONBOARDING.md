# KoT Alert / 勤怠ボード — 引き継ぎガイド

King of Time（KoT）の打刻データをもとに、LINE WORKSへ出退勤アラートを自動送信し、
さらに全社の勤怠状況をモニタ表示する「勤怠ボード」を提供するシステムです。
このドキュメントは新しい担当者がスムーズに引き継ぐための手順書です。
（システムの詳細仕様・変更履歴は同リポジトリの `CLAUDE.md` に集約されています。まずそちらも一読してください）

---

## 1. まず動いているものを見る（URL）

- 勤怠ボード（NeeSa・モニタ表示用）: https://133-125-93-39.sslip.io/board
- 管理画面ダッシュボード（トレコレ）: https://133-125-93-39.sslip.io/
  - `/staff` スタッフ管理 ・ `/shifts` シフト ・ `/logs` ログ ・ `/stores` 店舗設定 ・ `/settings` 設定
- 参考: 駐車場位置スプレッドシート（Google, 閲覧権限が要る）
  https://docs.google.com/spreadsheets/d/10L9zCxOSFLLT-ASPkyJQzEnaeVb-ePACDPL47WiESIk/edit?gid=2014155853#gid=2014155853

HTTP(`http://133.125.93.39`)でアクセスしても自動でHTTPSへリダイレクトされます。

---

## 2. 引き継ぎチェックリスト（前任者にやってもらう＝アクセス付与）

新担当者が作業するには次のアクセスが必要です。**★は前任者しか付与できないもの**。

- [ ] ★ **GitHub**: `stampingdakoku-bot/kot-alert-lineworks`（Private）に Collaborator 追加
- [ ] ★ **本番VPSのSSH**: `ubuntu@133.125.93.39`（さくらVPS）に新担当者の公開鍵を登録
      （`~ubuntu/.ssh/authorized_keys` に追記、または前任者が鍵を共有）
- [ ] ★ **Supabase**: プロジェクト `aujxtiyvdywabtnkvswm`（東京リージョン）への招待
- [ ] ★ **管理用Googleアカウント**: `Stamping.dakoku@gmail.com`（KoT/各種ログイン起点）
- [ ] **KoT WebAPI**: 新しい作業環境から叩く場合、許可IPに注意（本番は 133.125.93.39 を登録済み。
      ローカルから直接叩くのは基本しない。VPS上で実行する）
- [ ] **LINE WORKS**: 2テナント（avivastars=トレコレ/アソビバ、works-42585=NeeSa）の管理者権限が要る場合のみ

> 認証情報（トークン・秘密鍵）は **リポジトリには入っていません**。すべて本番VPSの
> `/home/ubuntu/kot-alert-lineworks/.env` と `*.pem` にあります（gitignore済み）。
> SSHアクセスさえ得れば、そこから参照できます。**チャットやメールに秘密情報を貼らないこと。**

---

## 3. 本番環境の全体像

- さくらVPS `ubuntu@133.125.93.39`（Ubuntu 22.04, 1Core-1GB）
- 構成: **Nginx(:80/:443) → Gunicorn(127.0.0.1:5000) → Flask(app.py)**
- HTTPS: 無料サブドメイン `133-125-93-39.sslip.io`（sslip.io, 登録不要）＋ Let's Encrypt（certbot自動更新）
- 定期実行: `checker.py` を cron で10分ごと（アラート送信本体）
- DB: Supabase(PostgreSQL) `aujxtiyvdywabtnkvswm`
- systemdサービス名: **`kot-alert`**（Web/管理画面）

```bash
ssh ubuntu@133.125.93.39
sudo systemctl status kot-alert      # 状態
sudo systemctl restart kot-alert     # 再起動（デプロイ後）
tail -f /home/ubuntu/kot-alert-lineworks/logs/cron.log   # checker.pyログ
```

---

## 4. 開発〜デプロイの流れ

**推奨（新担当者が自分のGitHub SSH鍵を持っている場合）＝ふつうのGit運用**
1. `git clone git@github.com:stampingdakoku-bot/kot-alert-lineworks.git`
2. 編集 → `git add <file>` → `git commit` → `git push origin main`
3. 本番反映: VPSで `cd /home/ubuntu/kot-alert-lineworks && git pull && sudo systemctl restart kot-alert`

**注意（この前任者PC特有の事情）**
- 前任者のWindows機には **GitHubのSSH鍵が無く**、push/pullを直接できませんでした。
  そのため「ローカルでcommit → `git bundle` でVPSへ転送 → VPSでpush」という回避策を使っていました
  （詳細は `CLAUDE.md` / メモ参照）。新担当者が自分の鍵でcloneできるなら、この回避策は不要です。
- **ローカルの古いクローンをそのままscpで上書きしない**こと（本番を巻き戻す事故が過去に発生）。
  必ずGit経由で同期する。

---

## 5. 主要ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | Flask管理画面（`/` ダッシュボード, `/staff`, `/logs`, `/board` など） |
| `checker.py` | アラート送信本体（cronで10分ごと）v3.0 |
| `db_supabase.py` | Supabase操作 + テンプレJSON管理 |
| `kot_api.py` | トレコレKoT WebAPIクライアント（`KOT_TOKEN`） |
| `neesa_kot.py` | NeeSa KoTクライアント（`NEESA_KOT_TOKEN`）＝勤怠ボードの色付け |
| `neesa_lw.py` | NeeSa LINE WORKSカレンダー読取＋ボードのマッピング設定 |
| `lw_api.py` | トレコレ側 LINE WORKS Botクライアント |
| `templates/board.html` | 勤怠ボード（NeeSa, キオスク） |
| `templates/base.html` `dashboard.html` 他 | 管理画面テンプレート |
| `alert_templates.json` | アラート文言テンプレート（管理画面から編集可） |
| `.env` / `*.pem` | 秘密情報（**git管理外・VPSのみ**） |

**勤怠ボードのマッピングを直したい時**は基本 `neesa_lw.py` の以下を編集:
`DEPT_MAP`（名前→会社/部署）, `EXCLUDE_NAMES`, `REMOTE_NAMES`, `SCHEDULE_BASED_NAMES`,
`CROSS_KOT_NAMES`（トレコレKoT側で打刻する人）, `KOT_NAME_ALIAS`（旧姓等の別名）, `DEPT_ORDER` など。

---

## 6. ハマりどころ（重要）

- **KoT API 禁止時間帯（JST 08:30–10:00, 17:30–18:30）**: この間はAPIが403。打刻が取れないので
  ボードは全員グレー（予定）、ダッシュボードは「—」表示になる（仕様）。時間帯を過ぎれば復旧。
- **KoTが2アカウント**: トレコレ(`KOT_TOKEN`・33名5店舗)と NeeSa(`NEESA_KOT_TOKEN`・別会社)は別物。
- **同姓・旧姓**: 河村(彩佳/遥華)など同姓が居るため `CROSS_KOT_FULLNAME` でフルネーム固定。
  改姓でカレンダー名とKoT名がズレる人は `KOT_NAME_ALIAS` で紐付け（例: 佐藤=旧姓佐々木果歩）。
- **Nginx**: `include /etc/nginx/sites-enabled/*` なので、このディレクトリに設定のバックアップを
  置かないこと（server_name重複で誤読込）。バックアップは `/home/ubuntu/nginx-backups/`。
- **誤通知時のリセット**: Supabase SQL Editorで `DELETE FROM alerts_sent WHERE alert_date = 'YYYY-MM-DD';`

---

## 7. 引き継ぎ完了の確認

- [ ] 新担当者が自分の鍵でGitHubからcloneできる
- [ ] 新担当者がVPSにSSHでき、`sudo systemctl status kot-alert` が見える
- [ ] 新担当者が試しに軽微な変更をcommit→push→VPSでpull→restart→反映を1往復できた
- [ ] Supabase / 管理Googleアカウントにアクセスできる

これらが通れば引き継ぎ完了です。困ったらまず `CLAUDE.md`（詳細仕様・全変更履歴）を参照してください。
