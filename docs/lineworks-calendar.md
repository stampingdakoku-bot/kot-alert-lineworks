# LINE WORKS カレンダー API とシフトの記入形式

勤怠ボードのシフトは LINE WORKS のカレンダーから読んでいます。ここでは API の使い方、
カレンダーの一覧（ID）、シフトの記入形式、繰り返しの扱い、再利用できる関数をまとめます。
（認証は [lineworks-auth.md](lineworks-auth.md) 参照。本システムは Service Account 読取）

## API エンドポイント（API ベース `https://www.worksapis.com/v1.0`）
- **カレンダー一覧**: `GET /users/{userId}/calendar-personals`
  → 本人＋共有されているカレンダーを返す（`calendarPersonals` 配列, 各 `calendarId` / `calendarName`）
  ※ `GET /calendars` は404、`/users/{id}/calendars` は calendarIds 必須なので、一覧はこの API を使う
- **イベント取得**: `GET /users/{userId}/calendars/{calendarId}/events?fromDateTime=&untilDateTime=`
  → `events[].eventComponents[]` に各予定。実装は `neesa_lw.get_calendar_events()`
- 起点ユーザー: `s-tatsuya2015@works-42585`（坂本達也。共有範囲のカレンダーが見える）

## カレンダー一覧（works-42585 / 坂本が閲覧可能・2026-08時点）
シフトに使う主なもの：

| calendar_id | 名前 | 用途 |
|---|---|---|
| `b6cc3c42-23e0-462c-a5e7-ca3c272f12bc` | 合同会社NeeSa | メインのシフト |
| `dfe29717-15f2-4fce-92b7-2b4baa37f4a2` | AceCosme 発送メンバー | 発送のシフト |
| `c_400183117_8750460c-3816-4fdd-88f9-169e3e1d57b9` | @121 spa&mart | ディアメント/@121 のシフト（別形式） |

その他（参考・個人/共有カレンダー）: 坂本達也 / 藤原真奈美 / 伊藤敬子 / 濵口亜沙美 / 三鹿景子 /
藤井菜乃花 / 物販確認用… / アソビバスターズ 発送メンバー / イーバリュー 発送メンバー 等が
`calendar-personals` に出る。共有カレンダーIDは `c_400183117_...` 形式、個人は UUID 形式。

## シフトの記入形式（ここが重要・2系統ある）

### A. メインのシフトカレンダー（合同会社NeeSa / AceCosme発送）
`summary` が「時間レンジ + 名前」のテキスト。時刻は summary から正規表現で解析。
- 例: `9-14 兼田` / `11:00-21:00 内田` / `930-1730山藤`
- 語尾に `@121`（全角`＠121`も）が付くと、その日はその人を @121 枠へ移す（マーカー方式）
- 非シフト（`有給 加藤` / `X休み` 等）は None 扱い
- 実装: `neesa_lw.parse_shift()` ＋ 時刻整形 `_fmt_time()`（`1530`→`15:30`, `9`→`9:00`）

### B. @121 spa&mart カレンダー（形式が違う・専用パーサ）
メインの「9-17 名前」形式ではない。実データ（例）：
- **時間指定イベント**（`start/end` に dateTime）: `平田　出勤可`(09:30-15:30) / `平田` / `歩`
- **テキスト時間**: `930-1330久保田` / `9-1645松田`
- **終日イベント**（`date` のみ・時刻なし）: `田中` / `久保田` / `松田`
- **注記・兼務**: `松田 兼任` / `松田兼任` / `松田5階兼任` / `歩 4Fにいるので…`
- **除外すべき**: `定休日`（繰り返し含む） / `有給 久保田` / `🎉オープニングイベント`

→ 名前は注記/兼務語を除去して抽出、時刻は「テキスト時間 ＞ イベントのdateTime ＞ なし(終日)」。
実装は `neesa_lw.py` の @121 専用パーサ（`parse_at121` 等）を参照。@121の人は
`(ディアメント, @121)` グループへ入れ、同名がメイン側にも出た日は @121 を優先。

## 繰り返し（RRULE）の扱い
繰り返し予定は `recurrence`（RRULE/EXDATE）を持ち、`dateTime` が欠けることがある。
`dateutil.rrule` で対象日に展開して該当分だけ採用し、名前で重複排除する。
- 実装: `neesa_lw._applies_on()`（時刻あり）／`_applies_on_at121()`（終日/日付ベース）
- 注意: 繰り返しマスタ＋EXDATE＋個別修正インスタンスが全部返ることがあるので、必ず対象日で絞る

## 再利用できる関数（`neesa_lw.py`）
- `get_access_token()` — Service Account JWT でトークン取得
- `get_calendar_events(calendar_id, from_dt, until_dt, user_id)` — 期間内イベントの平坦リスト
- `parse_shift(summary)` — メイン形式の解析
- `parse_at121(comp)` / `_clean_at121_name()` — @121形式の解析
- `_applies_on()` / `_applies_on_at121()` — 対象日判定（RRULE展開）
- `_fmt_time()` — 時刻表記の正規化

> ユーザー本人ログイン型（OAuth）の別プロジェクト（例 `lineworks-calendar-mcp`）で再利用する場合、
> **カレンダー取得〜解析ロジックはそのまま流用可**だが、**認証層は Service Account → OAuth に差し替え**が必要
> （[lineworks-auth.md](lineworks-auth.md) 参照）。
