# KoT Alert LINE WORKS System

## システム概要
KoT（King of Time）の打刻データをもとに、LINE WORKSでスタッフへ
出退勤アラートを自動送信するシステム。

## アカウント情報
- 管理Googleアカウント: Stamping.dakoku@gmail.com
- GitHub: stampingdakoku-bot/kot-alert-lineworks
- Supabase: aujxtiyvdywabtnkvswm.supabase.co（東京リージョン）

## サーバー情報
- 本番VPS: ubuntu@133.125.93.39（さくらVPS 石狩第1ゾーン 1Core-1GB）
- OS: Ubuntu 22.04
- 月額: 1,980円

## アーキテクチャ
- Nginx（:80）→ Gunicorn（:5000）→ Flask（app.py）
- checker.py が cron で10分ごとに実行
- DBはSupabase（PostgreSQL）

## ディレクトリ構成
```
/home/ubuntu/kot-alert-lineworks/
├── checker.py          # メインチェッカー v3.0
├── db_supabase.py      # Supabase DB操作 + テンプレートJSON管理
├── app.py              # Flask管理画面
├── config.py           # 設定（シークレットは.env）
├── kot_api.py          # KoT WebAPI クライアント
├── lw_api.py           # LINE WORKS Bot APIクライアント
├── mapping.py          # マッピング管理CLI
├── alert_templates.json # アラート文言テンプレート（7種別、管理画面から編集可能）
├── requirements.txt
├── .env                # 秘密情報（gitignore）
├── private_key.pem     # LINE WORKS JWT署名用（gitignore）
└── templates/          # Flask HTMLテンプレート
```

## Supabaseテーブル

| テーブル | 用途 |
|---------|------|
| `employees` | 従業員マスタ（KoTから同期、27名） |
| `mappings` | employee_key → lw_account_id マッピング（20件） |
| `alerts_sent` | 通知送信ログ（flow_type別） |
| `store_calendars` | 店舗カレンダー設定（4店舗） |
| `reminder_tracking` | リマインド追跡 |
| `alert_settings` | アラート設定（通知ON/OFF、回数上限、時刻等） |

※ alert_templatesテーブルは不使用（JSONファイル `alert_templates.json` で管理）

## 管理画面
- URL: http://133.125.93.39/
- パスコード: .envのADMIN_PASSCODE参照
- 機能:
  - `/` — ダッシュボード（店舗カード・出勤バッジ・60秒自動更新・最終更新時刻表示）
  - `/staff` — スタッフ管理（マッピング・除外・異体字不一致警告バナー）
  - `/logs` — アラートログ（種別・日付フィルタ・未申請者抽出・通知リセットモーダル）
  - `/shifts` — 週間シフト表示
  - `/stores` — 店舗カレンダー設定
  - `/settings` — アラート設定（通知ON/OFF・時刻・文言テンプレート編集）

## 通知フロー

出勤側（例: 13:00開始）
- 13:00 出勤アラーム（全員）
- 13:10〜 打刻なし検知（最大4回）

退勤側（例: 22:00終了）
- 22:00 退勤アラーム（全員）
- 22:10〜 超過警告（最大4回）
- 退勤後: 乖離通知 → 申請リマインド（最大2回）

管理者サマリー
- 23:00 速報（坂本達也宛）
- 翌10:10 申請漏れチェック

## アラート文言テンプレート
- `alert_templates.json` で管理（7種別: clockin_alarm, late_clockin, clockout_alarm, overtime, deviation, request_reminder, morning_check）
- checker.py がメッセージ生成時にJSONから読み込み、`{shift_start}`, `{shift_end}`, `{count}`, `{clock_out}`, `{diff}` を差し込み
- 管理画面 `/settings` から差し込みボタン方式で編集可能
- テンプレート未設定時はデフォルト文言にフォールバック

## 異体字対応
- `KANJI_VARIANTS` マップ（app.py内）で異体字を正規化
- 対応文字: 𠮷→吉、髙→高、濱→浜、晴→晴、嶋→崎、邉/邊→辺
- `_normalize_name()` で正規化し、`name_map` 構築時に通常漢字でも登録
- LINE WORKSカレンダーのsummaryは通常漢字、KoTの従業員名は異体字のケースに対応

## cron設定
```
0,10,20,30,40,50 10-22 * * * cd /home/ubuntu/kot-alert-lineworks && venv/bin/python3 checker.py >> logs/cron.log 2>&1
0 23 * * * cd /home/ubuntu/kot-alert-lineworks && venv/bin/python3 checker.py >> logs/cron.log 2>&1
```

## KoT API
- ベースURL: https://api.kingtime.jp/v1.0
- 禁止時間帯: 8:30-10:00, 17:30-18:30（JST）
- 許可IP: 133.125.93.39を登録済み
- timeRecord構造: 各レコードに `code`（1=出勤, 2=退勤）、`time`（ISO 8601）、`divisionName`（店舗名）

## LINE WORKS Bot
- Bot ID: 11845418
- Domain ID: 400183322
- Service Account: vwm4y.serviceaccount@avivastarscorporation
- Bot名: 勤怠アラート

## 店舗カレンダー（Supabaseのstore_calendarsテーブルで管理）
- 山口: 22時閉店
- 楽々園: 21時閉店
- 周南久米: 22時閉店
- フジグラン: 21時閉店

## 運用コマンド
```bash
# SSH接続
ssh ubuntu@133.125.93.39

# サービス操作
sudo systemctl restart kot-alert
sudo systemctl status kot-alert

# ログ確認
sudo journalctl -u kot-alert -n 50 --no-pager
tail -f /home/ubuntu/kot-alert-lineworks/logs/cron.log

# 手動実行
cd /home/ubuntu/kot-alert-lineworks
venv/bin/python3 checker.py

# 今日の通知レコードクリア（誤通知時）
# Supabase SQL Editor で:
# DELETE FROM alerts_sent WHERE alert_date = 'YYYY-MM-DD';
```

## 注意事項
- HTTPのみ（HTTPS未設定、ドメイン取得後に対応予定）
- mappingsが空のスタッフはLINE WORKS通知されない
- KoT API禁止時間帯はphase2（打刻チェック）がスキップされる
- private_key.pemは再発行済み（2026/3/18）
- 繰り返しカレンダーイベントはdateTimeが取れないことがあり、summaryから正規表現で時刻を解析
- 打刻データのdivisionNameで店舗フィルタ後0件ならフォールバック

## 構築・更新ログ

### 2026-03-20 初期構築
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

**初日稼働ログ（2026-03-20）**:
- 通知正常稼働（乖離通知1名: 内田雅大/山口、申請リマインド2回、速報送信、翌朝チェック完了）
- エラーなし（全API呼び出しHTTP/2 200 OK）

### 2026-03-21 機能追加・バグ修正

**バグ修正**:
- シフト開始前の「未打刻」誤表示修正（繰り返しイベントのdateTime欠損時、summaryから正規表現で時刻再構築、before_shiftをbool()キャスト）
- 別店舗の打刻時刻混入バグ修正（timerecordsをdivisionNameで店舗フィルタ）
- 全員未打刻表示バグ修正（KoT timeRecordのcode/time構造に対応、clockIn/clockOutキーは存在しない）
- 異体字（𠮷→吉等）でカレンダー名と従業員名が不一致になるバグ修正（KANJI_VARIANTS + _normalize_name()）
- 未申請者フィルタで内田が表示されない問題修正（旧: request_reminder有無 → 新: KoT残業申請有無で判定）

**新機能**:
- ダッシュボード: 店舗カードに出勤中人数バッジ（緑=全員/橙=一部/灰=0名）
- ダッシュボード: 60秒自動更新 + 最終更新時刻表示
- ダッシュボード: 出勤状況ドットサイズ拡大（16px→22px）
- ログページ: 通知リセットモーダル（POST /logs/reset、日付+スタッフ+種別で絞り込み削除）
- ログページ: 種別プルダウンに「未申請者」追加（deviation通知あり＆KoT残業申請なしで抽出）
- スタッフページ: 異体字不一致警告バナー（DB登録名と正規化後の名前を対比表示）
- 設定ページ: アラート文言テンプレート編集機能（差し込みボタン方式、alert_templates.json管理）
- 設定ページ: 時刻入力UI改善（72px幅、16pxフォント、ラベル上配置）
- checker.py: メッセージ生成をDBテンプレートベースに（デフォルト文言フォールバック付き）

## 既知の課題
- HTTPS未対応（ドメイン取得後に対応予定）
- マネフォビジネス給与API連携検討中
- KoT overtimes API未検証
