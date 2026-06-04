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
- 22:00 退勤アラーム（全員）＋打刻申請の案内付き
- 22:10〜 超過警告（最大4回）— 打刻申請手順を案内
- 退勤後: 乖離通知（退勤打刻あり＆超過時）
- 申請リマインド（最大2回）:
  - パターンA: 退勤打刻なし＆overtime開始分数経過＆打刻申請なし
  - パターンB: 退勤打刻あり＆15分以上超過＆打刻申請なし
  - overtimeアラート最終送信から設定間隔分後に送信（重複防止）

## 時間外申請フロー
- 2026-03-23以降、時間外申請を「スケジュール事前登録」から「KOT打刻申請（修正申告）」に一本化
- スタッフへの案内手順: タイムカード → 該当日の詳細 → 打刻申請 → 新規 → 申請メッセージ入力 → 申請
- checker.pyのrequest_reminderは退勤打刻なし・15分超過の2パターンでトリガー

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

### 2026-03-23 打刻申請フロー一本化

**方針変更**:
- 時間外申請を「スケジュール事前登録」から「KOT打刻申請（修正申告）」に一本化

**文言変更（alert_templates.json）**:
- clockout_alarm: 打刻申請手順の案内を末尾に追加
- overtime: 打刻申請手順を案内する端的な文言に変更
- request_reminder: 打刻申請の案内文言に変更（差し込み変数不使用のシンプルな文言）

**ロジック変更（checker.py）**:
- request_reminderのトリガー条件を変更:
  - パターンA: 退勤打刻なし＆overtime開始分数経過＆打刻申請なし
  - パターンB: 退勤打刻あり＆シフト終了から15分以上超過＆打刻申請なし
- overtimeアラートとrequest_reminderの重複防止（最終overtime送信から設定間隔分後に送信）
- db_supabase.pyにget_last_alert_time()関数を追加

## clock_error_reminder 本番稼働（2026-06-04 初回自動実行 正常確認済み）
- cron: `0 8 * * *` で checker.py --clock-error を毎日実行（--dry-runは2026-06-03に除去済み・本番モード）
- ログ出力先: logs/clock_error_dryrun.log（※dry-run時代のファイル名のまま。中身は本番ログ。将来 clock_error.log にリネーム予定）
- 申請除外ロジック: status=='applying' のみ除外（isDeleteは問わない）。approved/rejectedは除外しない（KOT側のisErrorで自然制御される）。commit 0b6f5a3
- 二重送信ガード: was_alert_sent(employee_key, 'clock_error_reminder', alert_date) が alerts_sent を参照してSKIP判定。clock_error_tracking の remind_count はエスカレーション段数のみ制御（SKIP判定には使わない）
- alerts_sent の flow_type CHECK制約に 'clock_error_reminder' を追加済み（2026-06-03、Supabase SQL Editorで手動ALTER）
- マッピング漏れ5名を督促対象として登録済み（矢幡4011・金広1021・河野1022・杉村4012・日高4013）。味志祥太朗4014は退職予定のため未登録。LW IDはUUID形式必須（ログインID av.xxxxx@... 形式は不可）
- 初回自動実行(06-04 08:00)結果: 解消2名(白井・内田)が送信スキップ＝resolved化、未修正3名(佐久間・河村遥華・吉武)が2通目、新規1名(大田和斗)が1通目。除外勢(宮崎・西森=is_excluded、applying19件)漏れなし。Exit 0完走

## break_warning 実装プラン（45分休憩 確認通知）※本番稼働中（2026-06-04 実装完了・初回手動送信済み）

### 目的
KOTの自動休憩付与ルール（稼働6:00〜7:59→45分、8:00以上→1時間、6h未満→休憩なし）のうち、45分休憩が付いた日を検出し本人と管理者に「念のため確認」を促す。45分は「本来1時間休憩だったのに早めの退勤打刻ミス等で8h未満に計算された」可能性があり、損するのは雇用者側。clock_error(isError)とは別物＝打刻が正常(isError=false)でも45分なら拾う。一次報告のみ・エスカレーション無し（clock_errorと違い段階督促しない。放置されても会社が損するだけで本人は損しないため）。

### 検出ロジック
- 給与サイクル(16日→前日、月跨ぎ対応)を全従業員走査。clock_errorと同じ期間計算を流用。
- 条件: daily-workings の breakTime == 45（分単位。実データ確認済み、秒ではない）かつ is_excluded=false。
- 除外: is_excluded6名。applying中の修正申告がある勤務日も除外（clock_errorの申請除外関数 get_pending_timerecord_dates を流用、status=='applying'のみ）。
- KOT仕様メモ: breakTimeはトップレベルのフラットな数値。自動付与/手動入力の区別はAPI上不可だが、運用上手動休憩は入れない前提なので breakTime==45 をそのまま条件に使う。6h未満は休憩0なので自動的に対象外。

### 通知
- 本人: 個別DM。flow_type 'break_warning'、二重送信ガードは was_alert_sent(employee_key, 'break_warning', 勤務日) で判定（勤務日ごと1回、二度言わない）。remind_count等のエスカレーション管理は不要。
- 管理者: 既存の send_daily_report()（checker.py:453-586、13:10発火、グループチャンネル LW_GROUP_CHANNEL_ID へ送信）に「45分休憩 確認対象」セクションを追記する形で相乗り。新規グループ送信は作らない。
- 発火: checker.py:437-444 の 13:10台 時刻判定ブロックに run_break_warning() 呼び出しを追加（本人DMもこのタイミング）。新cron行は作らない。前日分は確定済みなので翌日13:10で拾う。

### 本人向け文面（たたき台）
◯月◯日の勤務に45分休憩が記録されています。内容に間違いはありませんか？
もし打刻ミス等で実際と異なる場合は、KING OF TIMEで修正申告をお願いします。
（注: 「通常は1時間休憩のはず」の文言は入れない。水津悠斗のような正当な短時間勤務=常態45分の人がいるため混乱回避）

### 実装順序（前回clock_errorの教訓を反映）
1. 【最優先・コードより先】alerts_sent の flow_type CHECK制約に 'break_warning' を追加するALTERをSupabase SQL Editorで実行。前回これを後回しにして本番送信がExit 1中断＋二重送信リスクになった。
2. 検出ロジック run_break_warning() を checker.py に実装。
3. 本人DM + was_alert_sentガード。
4. send_daily_report() に管理者セクション追記。
5. 13:10時刻判定ブロックに呼び出し追加。
6. dry-run検証 → KOT実データと突き合わせ → 手動1回 → cron自動化（13:10同居なので新cron不要、コードpushで有効化）。

### 検証時の期待値（調査時点 2026-05-16〜のサイクル）
breakTime==45 は 5件2名（水津悠斗4件・西口恵1件、全件山口店、全件isError=false・申告なし）。本人通知対象＝この5件、管理者レポートにも同5件が載るはず。60分休憩178件は対象外。

### 本番化結果（2026-06-04 完了）
- 実装はステップ1〜5を順に完了。コミット: 64fb1fc(プラン)→38d9703(ステップ2検出)→dc61f31(ステップ3本人DM)→05ffba8(ステップ4管理者セクション)→b38a229(ステップ5本番化)。
- alerts_sent CHECK制約に 'break_warning' 追加済み(Supabase手動ALTER、2026-06-04)。
- 検出: run_break_warning() が給与サイクル走査でbreakTime==45を抽出。get_break45_for_date(date_str,all_emps)ヘルパーをsend_daily_reportと共用。
- 本人DM: 勤務日ごとに個別DM、was_alert_sent(ek,'break_warning',勤務日)で二重送信ガード。alert_dateは勤務日(clock_errorは実行日と異なる)。
- 管理者: send_daily_report(13:10台発火)に「45分休憩 確認対象」セクション追記、前日分のみ・0件時も0件表示。
- 発火: checker.py の13:10台ブロック(now.hour==13 and 10<=minute<20)に run_break_warning(dry_run=False) を組み込み。順序は send_daily_report→run_break_warning。新cron行なし、pushで有効化。13:10窓は10分刻みcronで1日1回のみ発火。
- 初回手動送信(2026-06-04 19:34): 水津悠斗4件(05-17,05-18,06-01,06-03)・西口恵1件(05-24)=5件2名、全件送信成功・alerts_sent記録済み。失敗/マッピングなし/スキップ0件。
- 実送信前の自分宛テスト(坂本9998)で文面・着弾確認済み(record_alert呼ばずDB非記録)。
- LW UUID修正: 水津・西口のlw_account_idをログインID形式からUUID形式に更新(/users/{loginId} APIで取得)。水津=ae907a50-1ec2-4af4-1ab5-0442e53994f9、西口=a84486af-626d-4593-1fb8-042503356746。
- 初回自動発火: 2026-06-05 13:10予定。既送信5件はガードでスキップ、前日06-04の新規該当者のみ本人DM+管理者まとめ掲載。
- 既知の軽微点: get_break45_for_date はisErrorを返さないため run_break_warning は5タプルのisError位置にFalseプレースホルダを渡している(ログ用途のみ・breakTime==45は実データで常にisError=false・実害なし)。

## 既知の課題
- HTTPS未対応（ドメイン取得後に対応予定）
- マネフォビジネス給与API連携検討中
- KoT overtimes API未検証
