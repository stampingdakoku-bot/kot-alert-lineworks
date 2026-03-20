# KoT Alert - LINE WORKS 打刻アラートシステム

## システム概要

KoT（King of Time）の打刻データとLINE WORKSカレンダーのシフト情報を照合し、
打刻漏れ・超過勤務・申請漏れをLINE WORKSメッセージで自動通知するシステム。

- **運用VPS**: 133.125.93.39（さくらVPS 石狩第1ゾーン / Ubuntu 22.04）
- **旧VPS**: 49.212.220.117（cron停止済み、trecole-priceのみ稼働）
- **DB**: Supabase（東京リージョン）`aujxtiyvdywabtnkvswm.supabase.co`
- **GitHub**: `stampingdakoku-bot/kot-alert-lineworks`（プライベート）
- **管理アカウント**: Stamping.dakoku@gmail.com
- **管理画面**: `http://133.125.93.39/`（パスコード認証）

## アーキテクチャ

```
[cron 10分間隔] → checker.py → KoT API (打刻取得)
                              → LINE WORKS API (カレンダー取得 + メッセージ送信)
                              → Supabase (従業員/マッピング/ログ/店舗設定)

[ブラウザ] → Nginx:80 → Gunicorn:5000 → app.py (Flask管理画面)
                                        → Supabase (CRUD)
```

## ファイル構成

| ファイル | 説明 |
|---------|------|
| `checker.py` | v3.0 メインチェッカー（定刻アラーム＋打刻検知＋申請確認＋速報＋翌朝チェック） |
| `db_supabase.py` | Supabase DB操作層（全テーブルのCRUD） |
| `config.py` | 設定（シークレットは全て.envから読み込み） |
| `app.py` | Flask管理画面（パスコード認証付き） |
| `kot_api.py` | King of Time API連携 |
| `lw_api.py` | LINE WORKS API連携（JWT認証＋メッセージ送信） |
| `mapping.py` | KoT↔LWマッピングユーティリティ |
| `templates/` | Jinja2テンプレート（login, dashboard, staff, logs, stores） |
| `private_key.pem` | LINE WORKS サービスアカウント秘密鍵（git管理外） |
| `.env` | 全シークレット（git管理外） |

## Supabaseテーブル

| テーブル | 用途 |
|---------|------|
| `employees` | 従業員マスタ（KoTから同期、27名） |
| `mappings` | employee_key → lw_account_id マッピング（20件） |
| `alerts_sent` | 通知送信ログ（flow_type別） |
| `store_calendars` | 店舗カレンダー設定（4店舗） |
| `reminder_tracking` | リマインド追跡 |

## checker.py 通知フロー

### 出勤側（例: 13:00開始）
| 時刻 | flow_type | 内容 |
|------|-----------|------|
| 13:00 | `clockin_alarm` | 出勤アラーム（全員1回） |
| 13:10〜 | `late_clockin` | 出勤打刻なし検知（10分刻み、最大4回） |

### 退勤側（例: 22:00終了）
| 時刻 | flow_type | 内容 |
|------|-----------|------|
| 22:00 | `clockout_alarm` | 退勤アラーム（全員1回） |
| 22:10〜 | `overtime` | 超過警告（10分刻み、最大4回） |
| 退勤打刻後 | `deviation` | 乖離通知（シフト終了と退勤打刻のズレ） |
| 乖離通知後 | `request_reminder` | 申請リマインド（最大2回） |

### 定期レポート
| 時刻 | flow_type | 内容 |
|------|-----------|------|
| 23:00 | — | 管理者へ本日の速報 |
| 翌10:10 | `morning_check` | 前日の申請漏れチェック |

管理者LW ID: `sakamoto.tatsuya@avivastarscorporation`

## 店舗設定（store_calendars）

| 店舗 | 閉店 | カレンダーAPI用ユーザー |
|------|------|----------------------|
| 山口 | 22:00 | wa9127@avivastarscorporation |
| 楽々園 | 21:00 | av.75304@avivastarscorporation |
| 周南久米 | 22:00 | av.56572@avivastarscorporation |
| フジグラン | 21:00 | av.26103@avivastarscorporation |

## cron設定

```
0,10,20,30,40,50 10-22 * * * cd /home/ubuntu/kot-alert-lineworks && /home/ubuntu/kot-alert-lineworks/venv/bin/python3 checker.py >> logs/cron.log 2>&1
0 23 * * * cd /home/ubuntu/kot-alert-lineworks && /home/ubuntu/kot-alert-lineworks/venv/bin/python3 checker.py >> logs/cron.log 2>&1
```

## .env 必要な環境変数

```
SUPABASE_URL=https://aujxtiyvdywabtnkvswm.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
FLASK_SECRET_KEY=...
ADMIN_PASSCODE=...
KOT_TOKEN=...
LW_CLIENT_ID=...
LW_CLIENT_SECRET=...
LW_SERVICE_ACCOUNT_ID=vwm4y.serviceaccount@avivastarscorporation
LW_BOT_ID=11845418
LW_PRIVATE_KEY_PATH=/home/ubuntu/kot-alert-lineworks/private_key.pem
LW_DOMAIN_ID=400183322
```

## サービス管理

```bash
# Flask管理画面
sudo systemctl status kot-alert
sudo systemctl restart kot-alert
sudo journalctl -u kot-alert -n 50 --no-pager

# checker.pyログ
tail -50 ~/kot-alert-lineworks/logs/cron.log
tail -50 ~/kot-alert-lineworks/logs/alert.log

# checker.py手動実行
cd ~/kot-alert-lineworks && venv/bin/python3 checker.py

# Nginx
sudo systemctl status nginx
sudo nginx -t && sudo systemctl reload nginx
```

## 管理画面

- URL: `http://133.125.93.39/`
- 認証: パスコード（.env ADMIN_PASSCODE）
- `/` — ダッシュボード（本日の通知サマリー、最近の通知）
- `/staff` — スタッフ一覧・マッピング管理（追加・編集・削除）
- `/logs` — アラートログ検索（種別・日付フィルタ）
- `/stores` — 店舗カレンダー設定

## 2026-03-20 構築作業ログ

1. 新VPS初期セットアップ（Ubuntu 22.04、nginx、Python3）
2. GitHub SSH鍵登録、リポジトリclone
3. Python仮想環境作成、依存パッケージインストール
4. Supabaseテーブル作成（employees, mappings, alerts_sent, store_calendars, reminder_tracking）
5. 既存VPSのSQLiteデータ → Supabase移行（employees 27名、mappings 20件）
6. db_supabase.py 作成（SQLite → Supabase置換）
7. checker.py v3.0（STORE_CALENDARSハードコード廃止、DB関数統一）
8. config.py シークレットを.env化
9. Flask管理画面作成（ダッシュボード、スタッフ、ログ、店舗設定）
10. パスコード認証追加
11. Nginx + Gunicorn + systemd設定
12. 既存VPSのcron停止、新VPSにcron移設
13. GitHubにpush
