# LINE WORKS 認証モデル（Service Account と OAuth の違い）

LINE WORKS の API 認証には**目的の違う2種類**があります。ここを混同すると詰まります。
（チャットで出た「Service Account認証しか見当たらない／OAuthタブが無い」問題の答えもここ）

## 1. Service Account 認証（サーバー間・本システムが使用中）

**用途**: サーバーがユーザー操作なしにAPIを叩く（＝勤怠ボードのカレンダー読取・Bot送信）。
**仕組み**: JWT(RS256)でアサーションを作り、トークンを取得（Authorization Code不要・ログイン画面なし）。

- 発行体(iss)=Client ID、対象(sub)=Service Account、秘密鍵(private key)で署名
- トークンURL: `https://auth.worksmobile.com/oauth2/v2.0/token`
  `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`
- API ベース: `https://www.worksapis.com/v1.0`
- 実装: `neesa_lw.py` の `_create_jwt()` / `get_access_token()`（scope は `calendar.read` 等）
- **リダイレクトURIは不要**（ユーザーログインが無いため）

これが「今画面に出ている Service Account 認証」です。**ユーザー本人が誰か、は分かりません**
（サーバーの権限で読む）。勤怠ボードのように「全員のカレンダーを表示するだけ」ならこれで十分。

## 2. OAuth 2.0 認証（ユーザー本人ログイン・MCP等で必要）

**用途**: スタッフ本人がログインして「自分の」データを操作する（本人識別が必要な機能）。
**仕組み**: Authorization Code フロー（ブラウザでログイン→リダイレクト→トークン交換）。

- 認可URL: `https://auth.worksmobile.com/oauth2/v2.0/authorize`
  （`client_id`, `redirect_uri`, `scope`, `response_type=code`, `state`）
- トークンURL: 同上 `/oauth2/v2.0/token`（`grant_type=authorization_code` + `code` + `redirect_uri`）
- **リダイレクトURI**の事前登録が必要（例: `https://133-125-93-39.sslip.io/auth/callback`）。
  コンソール登録値と、コードで使う値が**完全一致**していること（末尾スラッシュ等も）。
- 本人識別に必要なスコープ: `openid` / `email` / `user.profile.read` / `user.read`

### 重要: 同じアプリ内に両方ある
LINE WORKS Developer Console では、**1つのアプリに Service Account と OAuth の両方**が同居します。
Client ID / Client Secret は共通で、**grant_type を使い分ける**だけです。
「OAuthタブ」という独立タブが無くても、アプリ設定内の **OAuth Scopes / Redirect URI** の設定欄が
OAuth 用です。スコープ一覧に `openid` 等を追加→保存、Redirect URI を登録、で OAuth が使えます。

## 認証情報の在り処（秘密情報・git管理外）
本番VPS `/home/ubuntu/kot-alert-lineworks/.env` と `*.pem`：
- NeeSaテナント: `NEESA_LW_CLIENT_ID` / `NEESA_LW_CLIENT_SECRET` / `NEESA_LW_SERVICE_ACCOUNT` /
  `NEESA_LW_DOMAIN_ID` / `NEESA_LW_PRIVATE_KEY_PATH`（鍵=`neesa_private_key.pem`）
- トレコレ/アソビバテナント: `LW_CLIENT_ID` / `LW_CLIENT_SECRET` / `LW_SERVICE_ACCOUNT_ID` /
  `LW_BOT_ID` / `LW_DOMAIN_ID` / `LW_PRIVATE_KEY_PATH`（鍵=`private_key.pem`）
- **チャットやGitに貼らないこと。** SSHでVPSに入れば参照できる。

## テナント情報
- NeeSa（＋ディアメント/@121）: ドメイン `works-42585` / Domain ID `400183117` /
  Service Account `3zgca.serviceaccount@works-42585` / 起点ユーザー `s-tatsuya2015@works-42585`
- NeeSaのOAuth Scope は `bot / bot.message / calendar / calendar.read` に加え、
  ユーザーログイン用途なら `openid / email / user.profile.read / user.read` を追加する
- トレコレ/アソビバ: 別テナント（avivastars）。Bot ID `11845418` / Domain ID `400183322`

## まとめ（どちらを使う？）
- **表示だけ・全員のカレンダー/打刻を読む** → Service Account（本システムの `neesa_lw.py` が実例）
- **スタッフ本人がログインして自分の予定を登録/操作**（例: `lineworks-calendar-mcp`, `/my`）
  → OAuth（Redirect URI + openid/user 系スコープ）
