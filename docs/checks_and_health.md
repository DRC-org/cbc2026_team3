# 点検とヘルスの全体像

「機体が正常か」を答える仕組みは 4 系統ある。**目的も頻度も判定者も違う**ので、
どれか 1 つに寄せることはできない。この文書はその 4 つの境界と、どこを触ると
何が壊れるかを 1 箇所にまとめる。

個別の設計判断の理由は `CLAUDE.md` に、実装の経緯は `docs/impl_plan.md` にある。
ここが答えるのは **「今どうなっているか」と「どこを見ればよいか」** だけ。

---

## 4 系統の早見表

| | 答える問い | 頻度 | 判定するのは | 実装 |
|---|---|---|---|---|
| ① 受動ヘルス監視 | 今 CAN とモータは生きているか | 20Hz 配信 | サーバー | `lib/health.py` + `CANManager.health()` |
| ② 統合動作確認 | 指令したら本当に動くか | 準備中に 1 回 | シーケンスエンジン + 人 | `robots/motor_check.py` |
| ③ 常駐保護 | 壊れる前に力を抜けるか | 200 / 50 / 20Hz | 各周期タスク | `lib/control/` |
| ④ 指差喚呼 | ①②が見られないものを人が見たか | 試合前 | 人 | `config/checklist.yaml` |

**②と④は対になっている。** 動作確認は全アクチュエータを動かすが、自動で合否を
出せるのは到達判定を持つ軸だけ。持たない軸（DC 基板の `duty`、電磁弁の `on_off`）は
「指令を出した」ところまでしか保証できないので、鳴ったか・回ったかは④が受け持つ。

---

## ① 受動ヘルス監視 — 生きているか

```
CANManager._last_rx_at (デコード成功時だけ更新)
        │
        ▼
CANManager.health(thresholds)      ← しきい値は config/system.yaml の health
        │  FAULT > STALE > WARNING > OK の順で判定
        ▼
RobotServer._compute_health()      ← 例外は必ず DOWN へ倒す
        │
        ├──> 20Hz WS 配信 (state.health)  ──> web: evaluateHealth()
        ├──> _diff_health() ──> health_change (変化時のみ)
        └──> GET /health (200 / 503)
```

**受動**なので、モータを動かさずに分かることしか answer しない。「フィードバックが
来ているか」「ドライバが異常フラグを立てているか」「温度が閾値を超えたか」の 3 つ。

判定を書いてよい場所は 2 つだけ。**関数ではなくファイル単位の境界**である:

- サーバー側 — `lib/can_manager.py` の `health()`（優先度もここ）
- UI 側 — `web/src/lib/healthVerdict.ts`（`evaluateHealth()` の見出しチップ、
  `summarizeMotors()` のモータ一覧サマリー、`motorTempTone()` の温度色）

**画面の部品側に判定を書き足してはならない。** 一度 `MotorSummary` が自前で
「異常 N 件 / All operational」を出しており、FAULT のモータが行では赤バッジなのに
サマリーだけが緑を出していた（同じ画面に食い違う 3 つの表示が並んだ）。

しきい値の既定値は `lib/config_schema.py` の `HealthThresholds` にしかない。
**4 値は必ず 1 組で運ぶ**（バラすと 3 本だけ配線した経路が作れる）。

**UI が使う温度 2 値（`temp_warning_c` / `temp_critical_c`）は `server_info` で配る。**
接続直後に 1 度だけ届く（`lib/server.py` の `_server_info_dict()`）。UI 側にフォールバック値は
置かない —— 持つと config を変えても画面だけが古い境界で判定する二重管理が戻り、同じモータに
ついてサーバーと UI が違う答えを出す。**届いていない間は `neutral`（色を付けない）に倒す**。
適当な既定値で「正常」とも「警告」とも言わない。UI が使わない値（`feedback_timeout_ms` /
`tx_error_threshold`）は配らない（配ると「配られているのだから使ってよい」という別の写しの
根拠になる）。

**高温を UI が数え直してはならない。** 温度警告はサーバーが `temp_warning_c` を見て
`MotorHealth.state = warning` として既に配信している。`evaluateHealth()` が数えるのは
配信された健全性だけで、UI が別に数えると同じ 1 基が 2 件として計上される。

**UI はサーバーより楽観的な結論を出してはならない。** サーバーは健全性を計算できな
かったとき `overall=down` + 内訳空で「判定不能」を配る。内訳だけを見て「異常なし」を
返すと、そのフェイルセーフが画面上で消える。

---

## ② 統合動作確認 — 動くか

**両ハンドを 1 本のシーケンスで順に駆動する。** 機体ごとに独立していた頃は 2 つを
同時に起動でき、可動域の重なる位置で干渉しえた。

```
Monitor の設定面 [動作確認]
        │  motor_check_start (WS) / POST /motor_check
        ▼
RobotServer._motor_check_deny_reason()   ← 起動できるかの唯一の判定
        │  ①未登録 ②試合中 ③緊急停止中
        │  ④どれかが手動 ⑤どれかがシーケンス実行中 ⑥既に実行中
        ▼
RobotServer._start_motor_check()
        │  全ロボットの position_loop / target_refresher を pause
        ▼
MotorCheckSequence.run()          ← robots/motor_check.py
        │
        ├── 零点確定 (lib/sequence/homing.py)
        ├── メインハンド: y 軸 → 回転 → グリッパ → 壁 → コンベア
        ├── サブハンド: アーム → 補助ハンド → 電磁弁 ×6 → ポンプ ×2
        └── 両ハンドを初期姿勢へ戻す
        │
        ▼
motor_check_state (変化時のみ配信)  ──> web: useMotorCheck()
```

### 判定はシーケンスエンジンがそのまま担う

`move_to` が既に 3 つの判定を持っている。動作確認のために足した実装は無い。

| 軸の種類 | 到達判定 | 失敗すると |
|---|---|---|
| `position` / `velocity` | `tolerance` で判定 | `SequenceTimeoutError` |
| 左右直結ペア | 上記 + `sync_tolerance` | `AxisSyncError` |
| `duty`（DC 基板） | **無し**。`settle_s` の固定待ち | 落ちない → ④で目視 |
| `on_off`（電磁弁） | **無し**。`settle_s` の固定待ち | 落ちない → ④で打音 |

### 確認専用の値を持たない

運用で使う位置名（`home` / `open` / `run` …）へ動かす。かつて `motor_check.magnitude`
という専用の駆動量があり、位置定数とずれた瞬間に「動作確認で動く位置と運用で使う位置が
別物」になって確認が意味を失っていた。**その値は config ごと消えている。**

### 排他は全ロボットに掛かる

1 本のシーケンスが両機を動かすので、片方だけ見ると確認中にもう一方が手動で動かされる。
起動ゲート（`_motor_check_deny_reason`）も、送信経路の停止（`pause`）も、手動切替の
拒否も、全ロボットが対象。

### 起動の窓

タスクを作ってから `run()` が駆動を始めるまでに停止が届きうる。`Sequence.run()` は
冒頭で停止イベントを `clear()` するので、**サーバー側のフラグ
（`_motor_check_abort_requested`）で覚えておかないとその 1 通が消える**。
消えると「止めたはずなのに全アクチュエータが順に駆動される」。

---

## 零点確定（ホーミング）

②の最初のステップ。電源投入位置をそのまま原点にすると、前の試合の終了姿勢や搬送中に
手で動かしたぶんがそのまま座標のずれになる。位置定数は全て原点からの相対値なので、
ずれた原点のまま走らせると全ステップが同じだけずれた場所へ動く。

設定は `config/*_positions.yaml` の `axes.<軸>.homing`。**動作確認固有の値ではなく
軸の機構的性質**（どちら向きに、どれだけ動かせば原点に当たるか）なので位置定数と同居する。

```yaml
homing:
  sensor: origin_sensor   # config/<robot>.yaml の sensors: に登録した名前
  direction: -1           # +1 か -1 のみ
  search_distance: 30.0   # [軸の unit] これを超えたら失敗として止める
  step: 0.5               # 1 回の移動量
  settle_s: 0.05
```

**「当たるまで動かす」ので、止める仕組みが 3 つ要る。**

1. **`search_distance` の上限** — 配線が抜けたセンサは「いつまでも当たらない」形でしか
   現れない。**これが唯一の無人の歯止め**なので、省略できない必須キーにしてある
2. **センサ鮮度の事前確認** — 途絶していれば 1 歩も動かさない
3. **緊急停止** — 目標値を送る `AxisHandle` が既にインターロックを通る

失敗したら原点を確定せずシーケンスを止める。原点がずれたまま走るより、動かないまま
止まって操縦者に知らせるほうが安全。既にセンサに触れていれば動かさずに確定する
（機構端で始まったときに押し込まない）。

**現在の対象は `y_axis` だけ。** `rotate` のリミットスイッチは未装着で、
`config/main_hand_positions.yaml` に有効化手順をコメントで残してある。

---

## ③ 常駐保護 — 壊れる前に力を抜く

②とは独立に、待機中も手動操縦中も動作確認中も走り続ける。

| 周期 | 実装 | 何をするか |
|---|---|---|
| 200Hz | `control/position_loop.py` + `control/sync_guard.py` | 偏差超過・途絶で電流 0 にラッチ |
| 50Hz | `control/sync_monitor.py` | 偏差超過（2 サンプル）で**全体緊急停止** |
| 20Hz | `control/target_refresh.py` | 自作モタドラへ目標値を再送（生存通知） |

**偏差の境界そのものは `lib/axis_sync.py` の `SyncGroup.violation()` 1 箇所。**
3 層とも同じ関数を呼ぶ。層ごとに違うのは頻度と超過後の扱い（debounce・ラッチ・効果）
だけで、境界がずれてはならない。

---

## ④ 指差喚呼 — 人が見る

`config/checklist.yaml` のロールは `pre_match` 1 つで、Monitor の設定面に置く。
全項目の完了が試合開始のゲート（`can_start_match`）。

②が自動判定できないものを、ここが埋める:

| 項目 | なぜ自動判定できないか |
|---|---|
| `y_axis_sync` / `rotate_sync` | 左右のずれは③が監視するが、機構の目視は人にしかできない |
| `conveyor_run` / `conveyor_stop` | DC 基板はエンコーダも電流センスも持たない |
| `pumps_run` | 同上（ポンプも DC 基板） |
| `valves_closed` / `valves_actuate` | 電磁弁基板は弁が開いたかを観測できない |
| `suction_hold` / `suction_release` | 吸着の成否を測るセンサが無い |
| `origin_sensor_react` | センサが死んでいても「いつまでも当たらない」としか出ない |
| `firmware_match` | 版の不一致は CAN 越しには「応答しない」としか見えない |

**②から外れたものが④で埋まっていることは `tests/test_robot_sequences.py` が固定する。**
（`test_checklist_covers_what_cannot_be_judged_automatically`）

---

## 変更するときの落とし穴

### 判定を 2 箇所に書かない

| 判定 | 唯一の持ち主 |
|---|---|
| 機体の健全性（UI 側） | `web/src/lib/healthVerdict.ts` |
| ヘルスのしきい値 | `lib/config_schema.py` の `HealthThresholds` |
| 左右ずれの境界 | `lib/axis_sync.py` の `SyncGroup.violation()` |
| 動作確認を起動できるか | `RobotServer._motor_check_deny_reason()` |
| 試合を開始できるか | `MatchState.can_start_match` |
| フェーズによる可否 | `lib/commands.py` の `CommandSpec` / UI は `lib/phase.ts` |

UI は理由を説明するだけで、可否を導出し直してはならない。一度これを `StartGate` で
やって、サーバーが「開始できる」と配信しているのに画面がボタンを殺す状態を作った。

### 名前の一意性に依存している

- **モータ名**はロボット横断に一意（サーバーが名前で引く）
- **軸名**もロボット横断に一意（統合動作確認が両機の位置定数を 1 表へ束ねる）
- **CAN ID** はバス単位でロボット横断に一意

3 つとも `tests/test_robot_sequences.py` が固定している。

### 「測ったように見える 0」を作らない

電流・温度・過電流・過熱は自作モタドラのプロトコルに無い。到達フラグも電磁弁基板は
立てない。**測れないものは運ばない** — 常に 0 の値を流すと、UI にもヘルス判定にも
測ったように見える値が入り込む。

### テストが噛むか確かめる

安全に直結する不変条件を触ったら、本番コードにわざと 1 行の不具合を入れて、狙った
テストが落ちることを確認してから戻す。層と「1 枚だけを見る条件」の対応は
`docs/impl_plan.md` の「変異テスト」節にある。
