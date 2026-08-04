# 勤怠ボード（/board）アーキテクチャ

NeeSa 向けの勤怠状況モニタ画面。誰がどの会社/部署で業務しているかを一目で表示する。
URL: https://133-125-93-39.sslip.io/board （管理画面ダッシュボードは `/`）

## データフロー
1. `neesa_lw.get_today_shifts(target_date)` … LINE WORKS カレンダーから当日シフトを取得し、
   氏名→(会社,部署)で振り分け（[lineworks-calendar.md](lineworks-calendar.md) 参照）。全員 `status="scheduled"`。
2. 当日のみ `neesa_kot.apply_today(groups, now)` … KoT 打刻で各人の `status` を色付け＋打刻のみ者を追加。
3. `app.py` の `/board` が会社→部署でまとめて `templates/board.html` に描画。30秒自動更新。

## 状態（status）と色
- `working`（出勤中・緑 #27ae60）: 出勤打刻あり・未退勤
- `done`（退勤済・青 #3498db）: 退勤打刻あり or `totalWork>0`（修正申告等で確定）
- `scheduled`（予定・灰）: 打刻なし
- ※ ドット列は廃止し、右の状況テキスト（出勤中/退勤済/予定）で表現。会社バー色: NeeSa青/アソビバ緑/ディアメント赤

## マッピング設定（すべて `neesa_lw.py` の定数）
- `DEPT_MAP` … 氏名 → (会社, 部署)。未登録は `DEFAULT_GROUP`（NeeSa/発送部）＋「未分類」通知
- `COMPANY_ORDER` / `DEPT_ORDER` … 表示順
- `EXCLUDE_NAMES` … ボードに出さない人
- `REMOTE_NAMES` … 🏠リモート表示
- `SCHEDULE_BASED_NAMES` … KoT打刻でなく**LINEシフトの時間**で状態判定（開始前=予定/時間内=出勤中/終了後=退勤済）。
  例: 山藤（打刻特殊）、三鹿（リモート）、歩（坂本歩＝役員・KoTなし・@121専属）
- `CROSS_KOT_NAMES` … NeeSa KoTでなく**トレコレKoT**(既存 `KOT_TOKEN`)で打刻する人（例: 宮崎, 河村）
- `CROSS_KOT_FULLNAME` … トレコレKoT側で同姓が複数いる人の確定（例: 河村→河村彩佳）
- `KOT_NAME_ALIAS` … カレンダー表示名 → KoTフルネーム（旧姓等でズレる人。例: 佐藤→佐々木果歩）
- `KOT_FULLNAME` / `KOT_EXTRA_FULLNAMES` … NeeSa KoT側の同姓解決（例: 大井→大井夏美、大井蒼太→「蒼太」別表示）
- @121 は専用カレンダーを正式ソース化（`parse_at121`）。詳細は [lineworks-calendar.md](lineworks-calendar.md)

## KoT 色付けの重要事項（`neesa_kot.py`）
- **KoT は 2 アカウント**: トレコレ(`KOT_TOKEN`・33名5店舗) と NeeSa(`NEESA_KOT_TOKEN`・別会社) は別物。
  ボードの色付けは基本 NeeSa KoT。`CROSS_KOT_NAMES` の人だけトレコレKoTから取得。
- **禁止時間帯（JST 08:30–10:00 / 17:30–18:30）**: KoT API が 403。打刻取得不可。
  → `apply_today` は色付けをスキップし全員 `scheduled`（灰）のまま返す（全員赤等の誤検知防止）。
  → 管理画面ダッシュボードも打刻状況は「—」表示（未打刻＝赤にしない）。
- 同姓は「打刻あり優先」で解決。確定が要る人は `KOT_FULLNAME`/`CROSS_KOT_FULLNAME` でピン。
- 打刻のみ者（シフトなし＆打刻あり）はボードに追加表示。ただし `SCHEDULE_BASED_NAMES` は
  打刻が特殊なので「打刻のみ」では出さない（カレンダーにシフトがある日だけ時間で判定）。

## 管理画面（`/` ほか・`app.py` + `templates/base.html`）
- `/` ダッシュボード（店舗カード・シフト・打刻状況）、`/staff` `/shifts` `/logs` `/stores` `/settings`
- `/my` 個人メニュー（本人ログイン系。OAuth／別途）
- ブランドは「勤怠ボード」に統一、ログアウトボタンは非表示（パスコード認証は廃止）
- HTTPS: sslip.io 無料サブドメイン + Let's Encrypt（certbot自動更新）。HTTPは全てHTTPSへ301

## よくある運用
- 誤通知リセット: Supabase SQL Editor `DELETE FROM alerts_sent WHERE alert_date='YYYY-MM-DD';`
- 反映: 編集 → `git push` → VPSで `git pull` → `sudo systemctl restart kot-alert`
- Nginx設定のバックアップを `/etc/nginx/sites-enabled/` に置かない（`include *` で誤読込。
  置き場所は `/home/ubuntu/nginx-backups/`）
