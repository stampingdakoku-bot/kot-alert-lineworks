# KoT Alert / 勤怠ボード — 引き継ぎ & セットアップ手順書（松田さん向け）

King of Time（KoT）の打刻データをもとに、LINE WORKSへ出退勤アラートを自動送信し、
全社の勤怠状況をモニタ表示する「勤怠ボード」を提供するシステムです。
このドキュメント1本で、松田さんのPCでのセットアップから日々の運用まで完結します。
（さらに詳しい仕様・全変更履歴は同リポジトリの `CLAUDE.md` に集約。Claude Codeなら自動で読み込まれます）

---

## 0. まず動いているものを見る（URL）

- 勤怠ボード（NeeSa・モニタ表示用）: https://133-125-93-39.sslip.io/board
- 管理画面ダッシュボード（トレコレ）: https://133-125-93-39.sslip.io/
  - `/staff` スタッフ管理 ・ `/shifts` シフト ・ `/logs` ログ ・ `/stores` 店舗設定 ・ `/settings` 設定
- 駐車場位置スプレッドシート（Google, 閲覧権限が要る）:
  https://docs.google.com/spreadsheets/d/10L9zCxOSFLLT-ASPkyJQzEnaeVb-ePACDPL47WiESIk/edit?gid=2014155853#gid=2014155853

`http://133.125.93.39` にアクセスしても自動でHTTPSへリダイレクトされます。

---

## 1. 引き継ぎで受け取るアクセス（前任者が付与）

| # | アクセス | 内容 | 状態 |
|---|---|---|---|
| ① | GitHub | `stampingdakoku-bot/kot-alert-lineworks` にコラボレーター招待（Write） | 招待送信済み → **メールのAcceptを押す** |
| ② | 本番VPS SSH | `ubuntu@133.125.93.39` に公開鍵を登録 | 下の手順2で自分の鍵を作り公開鍵を渡す |
| ③ | Supabase | プロジェクト `aujxtiyvdywabtnkvswm` へ招待 | 招待メールをAccept |
| ④ | 管理Googleアカウント | `Stamping.dakoku@gmail.com`（KoT等のログイン起点） | パスワードを安全に受領 |

> **認証情報（トークン・秘密鍵）はGitHubには入っていません。** すべて本番VPSの
> `/home/ubuntu/kot-alert-lineworks/.env` と `*.pem` にあります（gitignore済み）。
> SSHが通れば本人がVPS内で参照できます。**秘密情報はチャット/メールに貼らないこと。**

---

## 2. 松田さんのPCでの初期セットアップ

### 2-1. VPSアクセス用のSSH鍵を作る（1回だけ）
ターミナル（Mac/Linux）または PowerShell / Git Bash（Windows）で：
```bash
ssh-keygen -t ed25519 -C "ai_matsuda@neesa-vps"
```
保存先・パスフレーズはEnterで既定のままでOK。続いて**公開鍵の中身**を表示：
```bash
# Mac/Linux / Git Bash
cat ~/.ssh/id_ed25519.pub
# Windows PowerShell
type $env:USERPROFILE\.ssh\id_ed25519.pub
```
表示された `ssh-ed25519 AAAA... ai_matsuda@neesa-vps` の**1行を前任者へ送る**。
→ 前任者がVPSの `authorized_keys` に登録したら、以下でSSH接続を確認：
```bash
ssh ubuntu@133.125.93.39
```

### 2-2. リポジトリを clone（GitHubの①招待をAcceptした後）
自分のGitHubアカウントにSSH鍵を登録済みなら：
```bash
git clone git@github.com:stampingdakoku-bot/kot-alert-lineworks.git
cd kot-alert-lineworks
```
（HTTPSでcloneする場合は `https://github.com/stampingdakoku-bot/kot-alert-lineworks.git`）

### 2-3. ローカルで動かすことについて（注意）
- KoT WebAPIは**許可IPが本番VPS(133.125.93.39)限定**、秘密情報もVPSにあるため、
  **ローカルPCでフル動作させるのは基本できません**。
- 運用は「**ローカル/VPSで編集 → git → 本番VPSで反映**」が基本。VPS上で直接編集・実行も可。

---

## 3. 本番環境の全体像

- さくらVPS `ubuntu@133.125.93.39`（Ubuntu 22.04, 1Core-1GB, 月1,980円）
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

## 4. 開発〜デプロイの流れ（標準）

1. 編集 → `git add <file>` → `git commit -m "..."` → `git push origin main`
2. 本番反映:
   ```bash
   ssh ubuntu@133.125.93.39
   cd /home/ubuntu/kot-alert-lineworks
   git pull
   sudo systemctl restart kot-alert     # app.py/テンプレ変更時
   ```
   （`checker.py` はcron実行なのでpullだけで次回反映。Web/画面変更はrestartが必要）

**注意（事故防止）**
- ローカルの古いクローンをそのまま `scp` で本番へ上書きしない（本番を巻き戻す事故が過去に発生）。必ずGit経由で同期する。
- 前任者のPCはGitHub鍵が無く「git bundle でVPS経由push」という回避策を使っていた。松田さんが自分の鍵でcloneできるなら**この回避策は不要**、普通に push/pull してよい。

---

## 5. 主要ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | Flask管理画面（`/` ダッシュボード, `/staff`, `/logs`, `/board` など） |
| `checker.py` | アラート送信本体（cronで10分ごと）v3.0 |
| `db_supabase.py` | Supabase操作 + テンプレJSON管理 |
| `kot_api.py` | トレコレKoT WebAPIクライアント（`KOT_TOKEN`） |
| `neesa_kot.py` | NeeSa KoTクライアント（`NEESA_KOT_TOKEN`）＝勤怠ボードの色付け |
| `neesa_lw.py` | NeeSa LINE WORKSカレンダー読取＋**ボードのマッピング設定** |
| `lw_api.py` | トレコレ側 LINE WORKS Botクライアント |
| `templates/board.html` | 勤怠ボード（NeeSa, キオスク） |
| `templates/base.html` `dashboard.html` 他 | 管理画面テンプレート |
| `alert_templates.json` | アラート文言テンプレート（管理画面から編集可） |
| `.env` / `*.pem` | 秘密情報（**git管理外・VPSのみ**） |

**勤怠ボードのマッピングを直したい時**は `neesa_lw.py` の以下を編集:
`DEPT_MAP`（名前→会社/部署）, `EXCLUDE_NAMES`, `REMOTE_NAMES`, `SCHEDULE_BASED_NAMES`,
`CROSS_KOT_NAMES`（トレコレKoT側で打刻する人）, `CROSS_KOT_FULLNAME`（同姓の確定）,
`KOT_NAME_ALIAS`（旧姓等の別名）, `DEPT_ORDER` など。

---

## 6. ハマりどころ（重要）

- **KoT API 禁止時間帯（JST 08:30–10:00, 17:30–18:30）**: この間はAPIが403。打刻が取れず、
  ボードは全員グレー（予定）、ダッシュボードは「—」表示になる（仕様）。時間帯を過ぎれば復旧。
- **KoTが2アカウント**: トレコレ(`KOT_TOKEN`・33名5店舗)と NeeSa(`NEESA_KOT_TOKEN`・別会社)は別物。
- **同姓・旧姓**: 河村(彩佳/遥華)など同姓が居るため `CROSS_KOT_FULLNAME` でフルネーム固定。
  改姓でカレンダー名とKoT名がズレる人は `KOT_NAME_ALIAS` で紐付け（例: 佐藤=旧姓佐々木果歩）。
- **Nginx**: `include /etc/nginx/sites-enabled/*` なので、このディレクトリに設定のバックアップを
  置かないこと（server_name重複で誤読込）。バックアップは `/home/ubuntu/nginx-backups/`。
- **誤通知時のリセット**: Supabase SQL Editorで `DELETE FROM alerts_sent WHERE alert_date = 'YYYY-MM-DD';`

---

## 7. アカウント / 外部サービス一覧

- GitHub: `stampingdakoku-bot/kot-alert-lineworks`（= `Stamping.dakoku@gmail.com` に紐づくアカウント）
- 本番VPS: さくらVPS `ubuntu@133.125.93.39`
- Supabase: プロジェクト `aujxtiyvdywabtnkvswm`（東京リージョン）
- 管理Google: `Stamping.dakoku@gmail.com`（KoT/各種ログイン起点）
- KoT WebAPI: `https://api.kingtime.jp/v1.0`（許可IPに 133.125.93.39 登録済み）
- LINE WORKS: 2テナント（avivastars=トレコレ/アソビバ, works-42585=NeeSa）

---

## 8. Claude Code キックオフプロンプト（そのまま貼る）

clone後、リポジトリ内で Claude Code を起動し、最初にこれを貼ってください：

```
このリポジトリ(kot-alert-lineworks)の保守を引き継ぎました。
まず CLAUDE.md と ONBOARDING.md を読んで全体像を把握してください。
これは King of Time の打刻をもとに LINE WORKS へ勤怠アラートを送り、
勤怠ボード(https://133-125-93-39.sslip.io/board)をモニタ表示するシステムで、
本番は さくらVPS ubuntu@133.125.93.39（systemd: kot-alert / Nginx→Gunicorn→Flask）です。

作業ルール:
- 本番反映は「編集→git push→VPSで git pull→sudo systemctl restart kot-alert」。
- 秘密情報(.env, *.pem)はVPSのみ。チャットに貼らない・GitHubに入れない。
- KoT APIは禁止時間帯(JST 8:30-10:00, 17:30-18:30)がある。
- ローカルの古いコードをscpで本番へ上書きしない(必ずGit経由)。

まず、本番の稼働状況(systemctl status kot-alert)と、local/VPS/originのgit同期状況を確認して、
現状を要約してください。
```

---

## 9. 引き継ぎ完了チェック

- [ ] ①GitHub招待をAccept、自分の鍵でcloneできた
- [ ] ②VPSにSSH接続でき、`sudo systemctl status kot-alert` が見える
- [ ] ③Supabase / ④管理Googleにアクセスできる
- [ ] 軽微な変更を commit→push→VPSでpull→restart→反映、を1往復できた

これらが通れば引き継ぎ完了です。困ったらまず `CLAUDE.md`（詳細仕様・全変更履歴）を参照してください。
