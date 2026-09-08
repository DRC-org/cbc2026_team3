# データの流れ — 値がどう届き、どこで判定するか

**「今どうなっているか」だけ。** 理由は `docs/invariants.md` §8、サーバー側の詳細は `docs/architecture.md` の
「WebSocket プロトコル」章。画面は `docs/web/screens.md`、罠は `docs/web/pitfalls.md`。

---

## 経路

```
サーバー (lib/server.py の WsHub)
   ▼
hooks/useWebSocket.ts      接続・再接続・接続先切替だけ。メッセージの意味は解釈しない
   ▼                        3 秒間隔で自動再接続 / 世代番号で旧接続のイベントを弾く
lib/protocol.ts            parseServerMessage() — ワイヤ型と受信条件。読めなければ null / MALFORMED
   ▼
lib/robotReducer.ts        純関数。受信 → UI 状態の遷移
   ▼
hooks/useRobotSocket.ts    上の 3 つを束ねて 1 つの UI 状態にする
   ▼
context/RobotContext.tsx   購読頻度で 3 分割して配る
   ▼
画面 (pages/ · components/)
```

**`lib/` は最下層で `hooks/` を import してはならない。** 接続を張らずに受信条件と状態遷移を
検証できる性質は、依存の向きが片方向であることで成立している。

---

## サーバー → UI

7 種（`ServerMessage` の union）。

| メッセージ | 中身 | 頻度 |
|---|---|---|
| `state` | ロボット 1 台の全状態（モータ・シーケンス・安全機構・手動・センサ） | 20Hz × 2 台 |
| `match_state` | フェーズ・コート・指差喚呼・タイマー | 変化時 |
| `motor_check_state` | 動作確認の進捗・結果・拒否理由・除外ステップ | 変化時 |
| `server_info` | しきい値・`dev_tools` フラグ・ロボット一覧 | 接続直後 |
| `e_stop_state` | 緊急停止の有無と**理由** | 変化時 + 定期再配信 |
| `health_change` | ヘルス変化（`EventFeed` とトースト） | 発生時 |
| `command_rejected` | コマンド拒否（トースト） | 発生時 |

**`state` の `motors` と `steps` は素通し。** モータ名を UI へ書かない性質はそこで成立している。
**`motor_check_state` の `steps` だけは検査する** —— 空配列が「まだ読み込まれていない」という
別の意味を既に持っているため。

**`state.manual.axes[].positions` は `{ name, value }`。** 名前だけを配っていた頃は、
プリセットが可動範囲のどこを指すのかが画面から読めなかった（バーには現在値の線 1 本だけ）。
`value` は人間の単位で、コート別に定義された位置は**現在のコートの値**になる。
サーバー側は `ManualController._position_entries` が引き、**値を引けなくても配信を落とさない**
（20Hz の経路なので、1 つの `PositionLookupError` が全クライアントのテレメトリを止める）。
読めなかった値は `null` で載せる —— 0 で埋めると、可動範囲の下端に居ないプリセットが
下端に描かれる。**指令は名前のまま**（`manual_move`）。

**タイマーは「経過ミリ秒」で届く。** 開始時刻ではない —— 操縦者 2 名 + Monitor が別 PC で
繋がるので、時刻を配ると端末の壁時計のずれが 3 つのタイマーの食い違いになる。各デバイスは
経過 ms を起点に自分の単調時計（`performance.now()`）で進める。

---

## UI → サーバー

**語彙の正はサーバーの `lib/commands.py`（`CommandSpec`）。** UI は送る前に理由を説明するだけ。

| 分類 | コマンド |
|---|---|
| シーケンス | `sequence_start` / `sequence_jump` / `trigger` / `sequence_stop` |
| 緊急停止 | `e_stop` / `e_stop_release` |
| 試合運用 | `match_start` / `match_finish` / `match_reset` / `set_court` |
| 指差喚呼 | `checklist_set` / `checklist_reset` / `checklist_check_all`（`--dev-tools` 限定） |
| 動作確認 | `motor_check_start` / `motor_check_abort` |
| 手動操縦 | `set_operation_mode` / `manual_move` / `manual_set` / `manual_jog` |
| その他 | `reenergize_motors` / `health_check` |

UI から送る経路を持たないものもある（`checklist_reset` は準備中の `match_reset` と結果が
変わらない）。**押せないコマンドを context の API に残さない** —— 次に触る人が使える操作だと読む。

---

## 契約は 1 箇所

**`web/src/test/ws-contract.json` は生成物。手書き禁止。**

```bash
UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py   # 再生成
```

Python 側が**実物の `RobotServer` に配信させたメッセージ**を焼き付け、TS 側
（`web/src/test/wsContract.test.ts`）が同じファイルを `useRobotSocket` の**受信経路へ流し込む**。
型アサーションでは足りない —— 型が合っていても受信条件が弾けば画面には何も出ない。

**両方向を見る。** 「UI が読む値が実配信に在るか」だけでは、**サーバーが送っているのに TS が
知らない欄**を取りこぼす —— `health.detail` が型にすら無く、サーバーの「判定不能」を画面が
「異常なし」と表示していた。逆方向は実配信のキーを再帰的に列挙して宣言と突き合わせ、使わない
フィールドは `unused` に理由を書く。書かれていない欄が増えたら落ちる。

**ワイヤ型と受信条件は `web/src/lib/protocol.ts` にしかない。** 型だけを別ファイルに持つと
「型は合っているのに受信条件が弾く」状態が作れる（画面には何も出ないのに型検査は通る）。

---

## 受信境界の 3 値

**混同しない。操縦者が次に取る行動が違う。**

| 値 | 意味 | 画面での出方 |
|---|---|---|
| `null` | **測る手段が無い**（正当な測定結果） | `—` |
| `MALFORMED` | 配信が**読めなかった** | 異常側へ倒す。「判定不能」 |
| `undefined` | まだ**届いていない** | 未取得。異常にしない |

DC 基板・電磁弁基板はエンコーダも電流センスも温度センサも積んでおらず、CAN プロトコルに
フィールド自体が無い。そこへ `0.0` を流すと「測ったように見える 0」が画面にもヘルス判定にも
入る。倒すのは**サーバー側の配信境界**（`lib/server.py` の `_measured_only()`）で、UI は
`readMeasured()` を通して読む。

**`?? []` のような黙った既定値を置いてはならない**（何が起きるかは `docs/web/pitfalls.md`）。
検査を通す関数: `parseSafety` / `parseHealth`（+ `*ShapeErrors`）/ `parseChecklists` /
`parseExcludedSteps` / `parseMotorCheckSteps` / `parseSensors` / `parseManual` /
`readMeasured` / `readCommand` / `parseEnum`。

**`parseManual` が見るのは `positions` だけ**（他の欄も `manual.axes` の他の軸も素通し）。
`positions` は**形を変えた唯一の既存欄**なので、サーバーと `web/dist` の版がずれる窓が
実際にある —— 旧形式（素の文字列）は `value: null` へ落とし、操作を保ったまま
「そこがどこかは分からない」を描く。どちらの形でもない要素だけ落とす（名前を読めない
ボタンを出すより、ボタンが無い方が嘘をつかない）。

**位置を測れないモータの POS 欄にだけ、代わりに PC の指令値（`command`）を出す。**
実出力ではないので `→` と `title` で断る。**実測値があるモータには出さない**（M3508 は
緊急停止しても `command` が残るので、外すと停止中の POS に指令値が出る）。

---

## 時刻の単位は型名で分ける

サーバーの `time.time()` は**エポック秒**、`Date.now()` は**エポックミリ秒**。どちらも
`number` なので取り違えても型検査を通る。`web/src/lib/time.ts` が `EpochSeconds` / `EpochMs` を
分けて持つ。

- **受信境界（`lib/robotReducer.ts`）で必ず ms へ正規化する**
- UI 状態のフィールド名は `...Ms` で終わらせる。ワイヤ形式（`started_at` 等）だけが `EpochSeconds`

取り違えると動作確認の実施時刻が **1970-01-01** になり、指差喚呼「動作確認 完了」の唯一の
判断材料が嘘になる（`wsContract.test.ts` の `motor_check_done` が固定）。

秒表示の更新は固定間隔ではなく**秒境界に合わせて**起こす（固定間隔だと端末ごとに起床位相が
ずれ、同じ値を持っているのに繰り上がりが食い違って見える）。

---

## 判定の単一情報源

**同じ判定を 2 箇所に書かない。** 書くと「Monitor は READY と言うのに操縦者の画面は異常と言う」
状態が生まれる。

| モジュール | 判定するもの |
|---|---|
| `lib/healthVerdict.ts` | 機体の健全性、温度トーン、ワーク落下の恐れ、版番号未確認、失敗タスク |
| `lib/sequenceStatus.ts` | シーケンスの実行状態・進捗の算術・「先頭から再開」か |
| `lib/motorCheckStatus.ts` | 動作確認の完了判定 |
| `lib/phase.ts` | フェーズによる可否・レイアウト区分。`isDuringMatch()` は `lib/match_state.py` の `PHASES_DURING_MATCH` の写しで、**写しはここだけ** |
| `lib/checklistGroups.ts` | 指差喚呼の項目をどの区分へ置くか |
| `lib/syncVerdict.ts` | 左右ペア軸のずれ表示 |
| `hooks/useRemainingMs.ts` | 試合の残り時間（アンカーと秒境界の起床）。操縦者の `MatchTimer` と Monitor の `MatchStrip` が同じ値を出す |

### `evaluateHealth` の判定順

上から順に最初に当たったものを返す。**順序そのものが仕様。**

| # | 条件 | トーン | ラベル |
|---|---|---|---|
| 1 | 切断中 | `neutral` | 通信断のため判定不能 |
| 2 | 安全機構の issue | `error` | 種別 + 対象 |
| 3 | ヘルス未取得 | `neutral` | ヘルス未取得 |
| 4 | `MALFORMED` / 形が壊れている | `error` | 健全性 判定不能 |
| 5 | `down` のバスがある | `error` | CAN 停止 ○○ |
| 6 | `fault` のモータがある | `error` | モータ異常 N 件 |
| 7 | `overall === "down"`（内訳に理由が無い） | `error` | 健全性 判定不能 |
| 8 | `ok` でないバス・モータがある | `warning` | 要確認 N 件 |
| 9 | `overall !== "ok"` | `warning` | 要確認（サーバー判定 degraded） |
| 10 | それ以外 | `success` | 異常なし |

- **切断中に緑を出さない。** 手元にあるのは切れた瞬間の値。異常とも言い切れないので色を付けない
- **サーバーの判定より楽観的にならない**（#7 · #9）。`overall=down` はサーバーが健全性を
  判定できていない状態で、`success` を返すとフェイルセーフを UI が打ち消す
- **UI はしきい値のフォールバック値を持たない。** 正は config だけが持ち `server_info` で届く
  （`tempThresholdsOf`）。未配信の間は `neutral` に倒す

**試合を開始できるかを決めるのはサーバーの `can_start_match` だけ。** `checklists` から導出し
直してはならない（一度これを `StartGate` でやって、サーバーが「開始できる」と配信しているのに
画面がボタンを殺した）。

---

## context は購読頻度で 3 つに分ける

`state` は 50ms × 2 台で**毎秒 40 回**変わる。束ねると、モータ温度が 0.1℃ 動いただけで
チェックリストもタブもトーストも描き直される。

| フック | 中身 | 変化 |
|---|---|---|
| `useRobotStates()` | `states`（テレメトリ） | 毎秒 40 回 |
| `useRobotStatus()` | `connected` / `eStopActive` / `eStopReason` / `healthEvents` / `motorCheck` / `matchState` / `serverInfo` / `rejection` / `wsUrl` / `wsUrlSource` | 変化時 |
| `useRobotCommands()` | 送信関数 14 個 | ほぼ不変 |

`RobotProvider` は 3 つの Provider を入れ子にし、`status` と `commands` を `useMemo` で
**フィールド単位の依存**にしてある（`value` 自体を依存にすると、呼び出し側が毎描画で新しい
オブジェクトを組むため分割の意味が消える）。

**ただし分割だけでは効かない。** 親が再描画すると React は **memo の無い子を素通しで描き直す**。
外枠は `memo` した `AppShell`（`layouts/RootLayout.tsx`）に括り出し、**そこへテレメトリ由来の
props を渡さない**。同じ理由で `Clock` は独立した部品にしてあり、`TabBar` も LED の内容だけを
購読して畳む（内容が同じ間は同じ配列参照を返す）。

`RootLayout.test.tsx` と `RobotContext.test.tsx` が**再描画回数**で守っている。

---

## 送信経路

**送るものはすべて `sendOrReport` を通る。**

```ts
sendOrReport({ type: "trigger", robot: robotKey }, "トリガー")
```

`send` は切断中に `false` を返して黙るので、**戻り値を捨てると「押したのにボタンは有効なまま・
機体は動かない・トーストも出ない」になる。**

**切断中に楽観的更新をしない。** 緊急停止で無条件に状態を変えると、何も送っていないのに全画面へ
赤いオーバーレイが出る。`inset:0` なので、矛盾を示すはずの接続バナーごと覆い隠す。黙って捨てる
のも危険なので、送れなかったことは通知枠へ流す。

---

## 接続先の解決

`web/src/lib/wsUrl.ts` の `resolveWsUrl()`。**クエリ `?ws=` > localStorage > `VITE_WS_URL` >
ページ origin。** 既定が origin なのは、別 PC・タブレットからのアクセスを成立させるため。

UI から差し替えられる（`WsSettings`）。**接続表示そのものがボタン**で、切断バナーからも開く ——
繋がらないときに最初に見る場所を入口にしておけば、設定を探す先が 1 つで済む。
`?ws=` は**非永続の一時上書き**で、保存済み設定を壊さずに 1 画面だけ別機を見られる。

差し替えが要るのは「配信元 ≠ 制御プログラム」になる構成 —— vite dev を Tailscale 経由で開く、
配信済み UI から手元の制御 PC へ繋ぐ、**予備機へ切り替える**。どれも再ビルドせず現場で解決する。

接続先を切り替えると `useWebSocket` は**世代番号**で旧接続の `close` / `message` を弾く。
弾かないと旧 URL への再接続タイマーが走り、古いサーバーの状態で画面が上書きされる。

### 開発サーバー経由で繋ぐとき（`web/vite.config.ts`）

- **dev と preview の両方**を全インターフェースに bind（`host: true`）し、`allowedHosts` に
  `drc` と `.ts.net` を登録する。既定の localhost bind と Host ヘッダ検査の**両方**が
  Tailscale 経由を塞ぐため。別名は `VITE_ALLOWED_HOSTS`
- dev では `/ws` を 8080 へプロキシする（中継先は `DEV_WS_TARGET`）

| 開き方 | URL |
|---|---|
| Tailscale 経由の dev サーバー | `http://drc:5173` |
| 制御プログラム直結（`web/dist` を配信） | `http://drc:8080` |
| 1 画面だけ別機を見る | `?ws=drc:8080` |

---

## REST

HTTP のエンドポイントは 3 つだけ（`lib/server.py` の `create_app`）。

| ルート | 用途 |
|---|---|
| `GET /health` | ヘルスのスナップショット（UI は使わない。運用の確認用） |
| `GET` / `POST /motor_check` | 動作確認の状態取得・起動 |
| `GET /assets` + SPA フォールバック（`/{path:.*}`） | `web/dist/` の配信 |

`GET /ws` が WebSocket のハンドシェイク。SPA フォールバックより**先に**登録しないと
`/health` も `/motor_check` も index.html に吸い込まれて 200 HTML になる。

### OpenAPI を導入しない

1. **REST が 3 つしかない。** 規約を 1 つ増やして得るものが無い
2. **OpenAPI は WebSocket のメッセージを表現できない。** まとめたい本体（7 種 + 21 コマンド）が
   1 つも書けない（WS 向けの規格は AsyncAPI）
3. **既に OpenAPI より強い契約が回っている。** `ws-contract.json` は実配信から生成され、
   受信経路へ流し込まれ、双方向で突き合わされる。手書きのスキーマは**実装とずれても何も
   落ちない**ので、3 つ目の情報源が増えるだけになる

契約が食い違ったまま両側のテストが緑になる形は既に一度踏んでいる（`health_change` に `robot` が
載っていないのに UI が受信条件にしていて、ヘルス異常が 100% 捨てられたまま出荷しかけた）。

機械可読な契約が要るなら、**手書きではなく `ws-contract.json` からの生成**にすること。
