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

### 判定に使う数は「今も壊れているか」に答えられなければならない

`BusHealthInfo.tx_error_count` は**起動からの累計**で、表示専用である。判定は
`CANManager._tx_error_score`（失敗 +8 / 成功 -1 / 上限 255 の TEC 相当）が持つ。

累計で判定していた頃、物理緊急停止で DM3520 の電源が数秒落ちただけで
`tx_error_count` が 6320 まで積み上がり、**CAN が完全に復旧した後も
`dm3520_bus` が永久に DEGRADED のまま残った**（`ip -s link` は ERROR-ACTIVE・
bus-off 0 回・送受信ともエラー 0、フィードバックも 44ms 前まで届いていた）。
単調増加する数は「壊れたことがあるか」にしか答えられない。

### bus-off はエラーフレームでしか観測できない

python-can 4.6 の `SocketcanBus` は `state` を実装していない（基底クラスの既定
`BusState.ACTIVE` が返る）。つまり `health()` の `ERROR` / `PASSIVE` 分岐は
**SocketCAN では永久に成立しない**。実バスの bus-off は
`CANManager._handle_error_frame()` が SocketCAN のエラーフレームから拾う。

ラッチは**実通信（送信成功・フィードバック受信）で外す**。`restart-ms` が 0 の
インタフェースは復帰通知（`CAN_ERR_RESTARTED`）を送らないので、それ以外に
外す経路が無いと一度立った DOWN が永久に残る（上の `tx_error_count` と同じ壊れ方）。

**`restart-ms` は 0 にしてはならない。** 0 は「bus-off から自動復帰しない」で、
カーネル既定でもある。バス上に 1 台しか居ない構成（`can_dm3520`）では相手の電源断
だけで ACK が返らなくなり TEC が 256 に達する。値は `config/can_buses.yaml` の
`restart_ms`（既定 100ms）が持ち、`scripts/setup_can.sh` が `ip link` へ渡す。

### だが現行の CANable2 では `restart-ms` を設定できない

SocketCAN の `restart-ms` は**ドライバが `do_set_mode` を実装している場合しか
受け付けられない**。CANable2 が使う `gs_usb` は実装しておらず、カーネルは
`EOPNOTSUPP`（`Error: Device doesn't support restart from Bus Off.`）を返す。
手動の `ip link set <if> type can restart` も同じ理由で通らない。ドライバ側の
恒久的な制約なので、`config` を変えても回避できない。

`setup_can.sh` は `bitrate` と `restart-ms` を**別のコマンドに分けて**発行し、
非対応を検出したら `restart-ms` 0 のまま続行して警告する。1 コマンドに束ねていた
頃は、非対応環境で**1 本も up できなかった**（しかも `bitrate` は先に適用されるので
「設定に失敗したのに bitrate だけ入っている」形になり、原因が読み取れない）。
起動ログの `restart-ms=` は要求値ではなく**インタフェースから読み戻した実効値**。

したがって bus-off へ落ちたバスを戻す経路は `down`/`up` しかない。カーネルには
任せられないので、**userspace の常駐（`scripts/can_watchdog.sh`）が受け持つ**。
PC 側のラッチは実通信で外れる設計なので、バスさえ戻れば表示も戻る。

### 実測: CANable2 は bus-off を報告しないし、自動復帰もしない

2026-08-30 に `can_edulite`（CANable2 / STM32G431・`clock 170000000` = FDCAN）を
**CAN 側に何も繋がない状態**で up し、`cangen` で ACK の返らないフレームを送って確認した。

- 送信は数通で**完全に停止する**（TX packets が増えず、`tc -s qdisc` の backlog が減らない）
- **30 秒待っても復帰しない。** ABOM 相当の自動復帰は無い。bxCAN 版の candleLight は
  `can_init()` で `CAN_MCR_ABOM` を立てるので自動復帰するが、CANable2 は FDCAN で、
  bus-off では `CCCR.INIT` がセットされたままホストの明示的な復帰要求を待つ。ファーム上流
  （`candle-usb/candleLight_fw` の `src/can/m_can.c`）は `GS_CAN_FEATURE_BUS_OFF_RECOVERY`
  を申告するが、**カーネル 7.0 の `gs_usb` はこの feature を知らない**（実装は
  `GS_CAN_FEATURE_GET_STATE` = BIT(13) まで）
- **`down` → `up`（＝ `scripts/setup_can.sh` の再実行）で確実に復旧する。** 再現性あり
- **PC 側からは状態が一切見えない**:
  - `can state` は落ちている間も **`ERROR-ACTIVE` のまま**。`bus-off` / `error-warn` /
    `error-pass` / `bus-errors` のカウンタも全部 0 のまま
  - エラーフレームが 1 通も届かない
  - `berr-reporting` は `requested control mode BERR-REPORTING not supported` で有効化できず、
    `GET_STATE` も未対応（`ip -details -s link show` に `txerr`/`rxerr` が出ない）

**したがって `_handle_error_frame()` の bus-off 検出は、この実機構成では発火しない。**
`30863fd` は「bus-off を立てる経路が存在しなかった」ことを直したが、アダプタが
エラーフレームを送らない以上、経路は依然として無い。実機で bus-off が現れるのは
**送信の失敗**（`tx_error_count` と送信スコア）と **qdisc の滞留**だけである。
自動復旧を作るなら、検出条件はエラーフレームではなくこの 2 つに置くこと。

### bus-off 復旧ウォッチドッグ（`scripts/can_watchdog.sh`）

`cbc-can-watchdog.service` として常駐し、1 秒周期で全バスを見る。機体を動かさない
（`down`/`up` しかしない）ので `cbc-can.service` と同じく **enable する** ——
制御プログラムだけが「電源投入で機体が待機状態にならない」ために enable されない。

**判定は `ip link` の state ではなく送信の滞留で行う。** 上の実測のとおり、落ちている
間もカーネルから見える状態は正常なままなので、state を見る実装は永久に発火しない。
条件は 2 つの AND:

- `tc -s qdisc` の backlog が 0 でない（＝送るものがキューに残っている）
- `ip -s link` の TX packets が前周期から 1 つも進んでいない

**どちらか片方では足りない。** backlog だけだと正常な連続送信の一瞬を拾って
**動いているバスを落とす**。TX packets だけだと「送るものが無いだけ」の平常時を
異常と読む。3 周期（既定）連続で成立したときにだけ `down`/`up` を出す。

**復旧には最短間隔（既定 5 秒）を置く。** 相手が最初から居ないバス（試合前で機体の
電源が入っていない）では復旧しても滞留は解消しないので、制限しないと `down`/`up` を
回し続ける。害はログ量だけだが、その害が大きい（journal が埋まって本物の異常が
沈む）。同じ理由で、連続復旧のログは 1 回目と 12 回に 1 回だけ出す。

**`bitrate` と `txqueuelen` は入れ直さない。** `down`/`up` をまたいで保たれることを
実測で確認済みで、入れ直すとその途中で失敗したときに元より悪い状態で残る。

判定ロジックは `tests/test_can_watchdog.py` が `ip` / `tc` のスタブで固定している。

### 受信ループは断絶で降りない（`rx_down`）

**ウォッチドッグの `down`/`up` で制御プログラムが道連れになっていた。**
`_receive_loop` は `bus.recv` の失敗をそのまま伝播させて降りる実装で、`down` の
1 秒で受信タスクが永久に失われた。`_tasks` は誰も await しないため死はログ 1 行に
しか現れず、症状は **「UI は接続中のまま、そのバスの全モータが STALE」** ——
`docs/impl_plan.md` が「最も復旧しにくい壊れ方」と呼んでいるものそのものだった。

前提が実測で覆ったのが直した理由:

```
① up      : send OK / recv フレーム受信
② down 中 : send・recv とも CanOperationError "Network is down [Errno 100]"
③ 再び up : 同じ socket のまま send OK / recv フレーム受信
```

**SocketCAN の socket は `ip link` の down/up を跨いで生き残る。** `down`/`up` は
ifindex を変えないので、待って呼び直せば戻る。バスを作り直す必要は無い
（復旧経路を増やすほど「復旧に失敗した中途半端な状態」が増える。ウォッチドッグが
`bitrate` を入れ直さないのと同じ理由）。

そこで受信側を送信側と同じ形にした —— `PeriodicTask._run` は tick ごとに例外を
握って回り続け、復旧すれば自力で戻る。**受信側だけが片道だったのが不具合の本体。**
再試行は 20ms から 200ms までのバックオフで、平常時（`_RECV_TIMEOUT` の
タイムアウトは成功であって失敗ではない）には 1 度も使われない。

**降りないだけでは足りない。黙って回り続けるのが最も危ない。** 読めていない間は
`_rx_down` を立て、`BusHealth.DOWN` として必ず見えるようにする。**`bus_off` へ
相乗りさせない** —— bus-off はコントローラがバスから切り離された状態、`rx_down` は
インタフェースが down している状態で、原因も復旧の手当ても別になる。1 つにまとめると
どちらが起きているのか画面からもログからも区別できない。外せる経路は
**実際にフレームを 1 通読めたときだけ**で、送信の成否でも、`recv` がタイムアウトで
`None` を返しただけでも外さない。

- 送信で外さないのは、インタフェースが戻って送信だけが通り、受信は死んだままという
  形を見逃さないため
- **タイムアウトで外さないのは実測に基づく。** python-can の socketcan は select が
  タイムアウトした時点で socket に触れずに `None` を返すので、**down している間も
  `None` は返り続ける**。最初これを復帰扱いにしていたところ、vcan を down させたまま
  「30ms で受信が再開しました」と誤判定し、`rx_down` は立った直後に外れて
  ヘルスは OK のままだった（実バスへの `ip link down/up` を通した検証で発覚）

### 受信の中断は M3508 の累積角を飛ばす

**受信が戻っても、位置は戻らない。** M3508 の多回転累積は「半周を超える差分は
0 を跨いだ折り返し」という推定に立っており、これは 1kHz で届き続けている間しか
成り立たない。中断した窓でモータ軸が半周以上回ると方向を取り違え、累積角に
1 回転（360deg）が乗る。

`y_axis` の scale 55.0131deg/mm では **6.54mm**。同じ軸の `sync_tolerance` は
**2.0mm** なので 3 倍を超える —— **左右の片方だけに乗れば、実在しないずれで
その場で全体緊急停止が掛かる。** ウォッチドッグの `down`/`up` は約 1 秒なので、
1 回の復旧動作がそのまま試合を止めうる。

`M3508Driver` は窓を跨いだ推定を拒み、差分を積まずに再アンカーする。判定は 2 つの
OR で、どちらか一方でも引っ掛かれば推定をやめる:

- 窓の間に回りえた回転数が半周に届くか。窓の**前後**で観測した rpm の大きいほうから
  見積もる（昇降軸は窓の間フィードバック途絶で電流 0 に落ちるため重力で加速する ——
  入口の rpm だけでは上限にならない）
- 窓そのものが長すぎるか（既定 100ms）。両端の rpm がたまたま 0 に見える
  「回って戻った」場合の歯止め。`feedback_timeout_ms` を流用しないのは、あちらが
  「途絶とみなす境界」でこちらの「折り返しを推定できる境界」とは別概念のため

再アンカーすると窓の中の実移動量は累積角から失われる。それでも 1 回転を捏造するより
誤差は小さい（推定を続けると実移動量に**加えて**必ず 360deg が乗る）。

**失った量は測れないので、代わりに「原点はもう信用できない」ことを報告する。**
`health_detail()` が `MotorHealth` を WARNING へ倒し、`MotorHealthInfo.detail` に
理由を載せる。**状態を OK に置いたまま detail だけ載せてはならない** ——
`summarizeMotors` が「All operational」を出して `SubsystemStatus` は畳んだままになり、
報告はどの画面にも現れない（「報告した」つもりの黙殺が成立する）。信頼を戻す経路は
原点の確定（`reset_multi_turn_origin`）だけで、受信の復帰では戻らない。

### 「励磁されていない」はヘルスに現れない

DM3520 は指令フレームを無励磁のまま受理して黙って捨てる。ドライバの通信途絶保護や
電源の瞬断で励磁が外れると、PC は 20Hz で位置指令を送り続け、フィードバックも正常に
届き、`is_fault()`（エラー符号 0x5 以上）にも掛からないのでモータのヘルスは **OK の
まま**になる。操縦者に見えるのは「指令しても動かない」だけで、原因はどこにも出ない。

そこで `MotorDriver.is_energized()`（判定手段が無いドライバは `None`）を
`RobotServer._unenergized_motors()` が読み、`state.safety.unenergized_motors` として
配信する。緊急停止中は無励磁が正しい状態なので報告しない。**この緊急停止ガードが
唯一の抑止で、`activate_e_stop` 側にも同じ判定を置いてはならない**（片方を消しても
症状が出なくなり、後で本物のガードを消しても気付けなくなる）。

`None` を「無励磁」へ倒さないこと。自作モタドラも C620 も励磁の有無を報告しないので、
倒すと常時警告になる（「測ったように見える 0」と同じ罠）。

---

## ② 統合動作確認 — 動くか

**両ハンドを 1 本のシーケンスで順に駆動する。** 機体ごとに独立していた頃は 2 つを
同時に起動でき、可動域の重なる位置で干渉しえた。

```
Monitor の設定面 [動作確認]
        │  motor_check_start (WS) / POST /motor_check
        ▼
MotorCheckController.deny_reason()       ← 起動できるかの唯一の判定
        │  ①未登録 ⑥既に実行中          ← コントローラ自身の条件
        │  ②試合中 ③緊急停止中 ④どれかが手動 ⑤どれかがシーケンス実行中
        │                                 ← RobotServer._motor_check_environment_deny()
        ▼
MotorCheckController.start()
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
起動ゲート（`MotorCheckController.deny_reason()`）も、送信経路の停止（`pause`）も、手動切替の
拒否も、全ロボットが対象。

### 起動の窓

タスクを作ってから `run()` が駆動を始めるまでに停止が届きうる。`Sequence.run()` は
冒頭で停止イベントを `clear()` するので、**シーケンスの外側のフラグ
（`MotorCheckController._abort_requested`）で覚えておかないとその 1 通が消える**。
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

**現在の偏差は手動操縦パネルの軸行に出る**（`state.manual.axes[].deviation`。算出は
同じ `SyncGroup.deviation()`）。ここが無かった頃は「どれだけずれて止まったか」を読む
手段が診断カラムのモータ生単位 `POS` しかなく、逆回転ペアでは符号まで反転して見えるため
暗算では追えなかった。結果として `sync_tolerance` を勘で緩める以外に手が無くなる。
許容差の 6 割を超えると行が自分から主張する（超過した時点では 50Hz の `SyncMonitor` が
既に全体緊急停止を掛けているので、操縦者が手を打てるのはそれより前の区間しかない）。

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
| ヘルスの最悪値への集約（サーバー側） | `lib/health.py` の `worst_bus_health()` |
| 動作確認が完了したか（UI 側） | `web/src/lib/motorCheckStatus.ts` |
| ヘルスのしきい値 | `lib/config_schema.py` の `HealthThresholds` |
| 左右ずれの境界 | `lib/axis_sync.py` の `SyncGroup.violation()` |
| 動作確認を起動できるか | `MotorCheckController.deny_reason()`（`lib/server_motor_check.py`） |
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
