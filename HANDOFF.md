# kot-alert-lineworks 引き継ぎメモ（最終更新 2026-06-08）

次チャット開始時にこのファイルを読めば現状を把握できるように維持する。詳細な実装記録は CLAUDE.md 側にある。

## 稼働中の機能
1. **clock_error_reminder** — 朝8:00 cron、打刻エラー(isError)未対応者へDM＋段階督促。本番稼働中。remind_count>=3 の最終段テンプレを「最終警告」文面に差し替え済み(commit 8983651, 2026-06-08)
2. **break_warning** — 13:10、45分休憩(breakTime==45)検出→本人DM＋管理者まとめ掲載。2026-06-04本番化、初回手動送信5件完了・総務周知済み。詳細はCLAUDE.md「break_warning 実装プラン」参照
3. **send_daily_report 冒頭リマインド** — 13:10管理グループまとめの冒頭に総務チェック前日/締め前日リマインドを差し込み(commit 29987fc, 2026-06-08)。jpholiday で祝日判定。既存まとめ本体・送信処理・二重送信ガードは無変更、1日1通のまま
   - 14日 → 締め版「⚠️ 明日15日は勤怠の最終締め日です」（土日祝でも必ず送る）
   - 月曜/水曜 かつ当日・翌日とも祝日でない → 定例版「📋 明日は総務による勤怠のチェック日です」
   - 14日が月水と重なったら締め版を優先（定例は出さない）
   - 本番デビュー: 定例版=次の水曜(06-10)から、締め版=毎月14日から

## 直近の観察ポイント
- **2026-06-10(水) 13:10 が定例リマインドの初回自動発火**。まとめ冒頭に「📋 明日は総務による勤怠のチェック日です」が載るはず。logs/cron.log で確認
- **2026-06-14 13:10 が締め版リマインドの初回自動発火**。まとめ冒頭に「⚠️ 明日15日は勤怠の最終締め日です」が載るはず

## 環境・アクセス
- VPS: `ssh ubuntu@133.125.93.39` (port 22)、プロジェクト `/home/ubuntu/kot-alert-lineworks`、venv は venv/、`claude` はプロジェクトディレクトリで起動
- GitHub: stampingdakoku-bot/kot-alert-lineworks（git push可）
- Supabase: project ref **aujxtiyvdywabtnkvswm**（※別プロジェクト mjouvwdnxdtphfkngsjx=サロンアプリ と取り違え注意。SQL実行前にURL確認）。DDL後は `NOTIFY pgrst, 'reload schema';`
- 本番稼働: systemd `kot-alert.service` → `gunicorn --bind 127.0.0.1:5000 --workers 1 --timeout 120 --max-requests 500 app:app`
- crontab 2行のみ: `0 8 * * *`(checker.py --clock-error) と `0,10,20,30,40,50 10-22 * * *`(checker.py 通常)
- KOT APIブラックアウト: **17:30-18:30** と **8:30-10:00** JST（daily-workings が0件/403）。pytz未導入、時刻確認は `TZ=Asia/Tokyo date`
- 依存: jpholiday 1.0.3（祝日判定、2026-06-08導入、requirements.txt追記済み）
- LINE WORKS Bot送信: ログインID形式(av.xxxxx@, sakamoto.tatsuya@等)でもUUID形式でも送信成功する(200/201)。佐久間・吉武・坂本で確認済み(2026-06-08)。UUIDは `GET /users/{loginId}` APIで取得可（トークンに user.read スコープあり）

## 既知の課題（2026-06-04 調査済み）
1. **datetime.fromisoformat Python3.10互換 — 部分対応**
   - db_supabase.py の sent_at 経由は `_parse_iso()`(小数6桁正規化)で対処済み(commit fa72099)
   - checker.py / app.py / kot_api.py の fromisoformat 直接呼出し(計13箇所)は未経由だが、入力元(KOT API等)が標準ISO形式のため実害は出ていない。網羅対策は未実施
2. **無効な LINE WORKS user ID の 400 エラー繰り返し — 未解決**
   - send_message 失敗時は False を返しログ出すのみ。無効IDを記録して以後スキップする仕組みなし→次回も再送を試みる
   - 現状はマッピングをUUID形式に統一済みで発生しにくいが、退職者のUUIDがLINE WORKS側で削除された場合などに再発しうる
3. **Gunicorn worker OOM — 解決済み**
   - `--max-requests 500`(リーク対策の定期再起動)＋`--workers 1`(1GB VPSに適切)で対処済み

## 中長期の検討事項
- HTTPS未対応（ドメイン取得後に対応予定）
- マネフォビジネス給与API連携検討中（KOT全面置換の可能性）

## 進め方の原則（このプロジェクトで機能している流儀）
- **ALTER-before-code**: DB制約変更を最初に。後回しにすると本番送信がExit 1中断＋二重送信リスク（clock_errorで実際に発生した教訓）
- **実データ/dry-run検証 → コミット**: 各ステップを実データかdry-runで期待値と突き合わせてからコミット。新機能はステップ単位で刻む（break_warningは5ステップに分割）
- **送信前パリティ監査**: 新flowの送信パス(send_message/record_alert/UUID形式)を既存clock_errorと突き合わせてから実送信。break_warningではこれでUUID形式ブロッカーを事前発見した
- **最小実送信テスト**: 自分宛(坂本9998)に1通→着弾確認→実スタッフへ。テスト送信はrecord_alertを呼ばずalerts_sent非記録（本番ガードを汚さない）
- **Claude Codeへの指示**: 自然言語＋明示的ガード(「送信/コミットするな・止まって報告」)。heredocパッチは誤認されるので避ける。各ステップ検証後に都度コミット
