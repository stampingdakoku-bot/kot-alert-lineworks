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
├── checker.py      # メインチェッカー v3.0
├── db_supabase.py  # Supabase DB操作
├── app.py          # Flask管理画面
├── config.py       # 設定（シークレットは.env）
├── kot_api.py      # KoT WebAPI クライアント
├── lw_api.py       # LINE WORKS Bot APIクライアント
├── mapping.py      # マッピング管理CLI
├── requirements.txt
├── .env            # 秘密情報（gitignore）
├── private_key.pem # LINE WORKS JWT署名用（gitignore）
└── templates/      # Flask HTMLテンプレート
```

## 管理画面
- URL: http://133.125.93.39/
- パスコード: .envのADMIN_PASSCODE参照
- 機能: ダッシュボード/スタッフ管理/アラートログ/店舗設定

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

## cron設定
```
0,10,20,30,40,50 10-22 * * * cd /home/ubuntu/kot-alert-lineworks && /usr/bin/python3 checker.py >> logs/cron.log 2>&1
0 23 * * * cd /home/ubuntu/kot-alert-lineworks && /usr/bin/python3 checker.py >> logs/cron.log 2>&1
```

## KoT API
- ベースURL: https://api.kingtime.jp/v1.0
- 禁止時間帯: 8:30-10:00, 17:30-18:30（JST）
- 許可IP: 133.125.93.39を登録済み

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
python3 checker.py

# 今日の通知レコードクリア（誤通知時）
# Supabase SQL Editor で:
# DELETE FROM alerts_sent WHERE alert_date = 'YYYY-MM-DD';
```

## 注意事項
- HTTPのみ（HTTPS未設定、ドメイン取得後に対応予定）
- mappingsが空のスタッフはLINE WORKS通知されない
- KoT API禁止時間帯はphase2（打刻チェック）がスキップされる
- private_key.pemは再発行済み（2026/3/18）

## 積み残し
- HTTPS対応（ドメイン取得後）
- マネフォビジネス給与API連携検討中
- KoT overtimes API未検証
