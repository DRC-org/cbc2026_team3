# TODO — まだ手を付けていないこと

**未着手と未決の一覧。** 実装まで済んだら該当する文書（`invariants.md` /
`architecture.md` / `mechanism_handoff.md`）へ移し、ここからは消す。

**詳細をここへ書かない。** ここは「何が残っているか」の索引で、値と手順の正は
各文書にある。

## 1. サブハンドの直動 2 軸の位置定数が仮値

試合シーケンスは `sequences/sub_hand.py` に 66 段（初期位置 1 + 16 × 4 + 復帰 1）で
入っており、崩してはいけない順序は `tests/test_robot_sequences.py` と
`tests/test_sub_hand_positions_config.py` が**値ではなく関係**で固定している。
残っているのは実測だけ。

埋める値と満たすべき条件は [`mechanism_handoff.md`](mechanism_handoff.md) §2 が正。
残るのは `sub_y_axis` / `sub_lift`（サーボ 3 軸は 2026-09-10 に実測済み。可動域クランプは
同 §0 に残っている）。

## 2. サーボ基板 #2（`0x50`〜`0x54`）が一度黙った原因が未特定

2026-09-10 に 5 スロットとも `FEEDBACK` が止まり、書き込み時のリセットで復帰した。
左右ペアを 90deg ずらして押し合わせた直後だったので停動による過電流が疑わしいが、
切り分けていない。

## 3. メインハンド `y_axis` の左右位置ずれ

左右の M3508 が 106.7 と 211.4 で、互いに逆向きのトルクが出ていた（軸の値は −14.0mm で
宣言範囲の外）。**原因未特定のまま、実機で再現するかを確かめていない。**
この状態で零点合わせを回すと失敗する。**原因が分かるまでメインハンドは動かさない。**

PID とプロファイルの値そのものは [`mechanism_handoff.md`](mechanism_handoff.md)
§3-1〜§3-3 で詰めてあり、そこに残る未検証（`timeout_s`、短距離の `sync_kp`、
ワーク保持力）は別の話。

## 4. 非常停止中のログが送信失敗で埋まる

モータ電源が落ちている間、3 バスとも送信が `Transmit buffer full` で失敗する。
`LogThrottle`（`lib/control/periodic.py`、1 秒に 1 件/キー）は効いているが、
バス数 × 定期タスク数ぶんが毎秒スタックトレース付きで出続けるので他のログが読めない。
**動作には影響しない**（イベントループが固まる不具合は修正済み）。1 行に畳む。

## 5. 励磁の誤診（2026-09-10）を調べる途中で見つかったもの

「無励磁」と「応答なし」を分ける修正（`docs/history/incidents.md` 2026-09-10）の
調査で見つかったが、その修正には含めていないもの。

- **`encode_disable(clear_fault=True)` がリポジトリ全体で 1 度も呼ばれていない**
  （`lib/drivers/edulite05.py`）。fault ラッチを解除する手段が PC 側に無いので、
  2026-09-05 の事象（UNDERVOLTAGE で励磁が落ち、fault が消えても戻らない）が
  再発したとき再励磁で直らない。`docs/architecture.md` の「既知の穴」にも同じ趣旨がある
- **`Edulite05Driver` が `health_detail()` を実装していない。** どの fault ビットが
  立ったかが画面にもログにも出ない（DM3520 は実装済み）
- **`lib/config_schema.py` の `edulite05` の CAN ID 許容域が `0x00..0xFF`。** 出荷値
  `0x7F` も `host_id` と同値の `0xFD` も弾かない（`generic` は `0xFF` を弾くのに非対称）
- **`tests/test_robot_sequences.py` の「モータ名・CAN ID はロボット横断に一意」テストが
  `config/bench/**` と `sensors:` を見ない**
- **`lib/drivers/edulite05.py` の `VEL_MIN/MAX = ±50.0` / `TORQUE_MIN/MAX = ±6.0` に
  リポジトリ内の典拠が無い。** トルク基準の歯止めを入れる前に実測で確認が要る
- **`docs/architecture.md` の「EDULITE 05 × 3」が config（2 台）と食い違う**
- **「無励磁」と「応答なし」の境目にヒステリシスが無い。** `feedback_timeout_ms` の既定
  500ms に対して `state` は 50ms ごとに配る。バスが劣化して到達が 500ms 付近を行き来すると
  同じモータが 2 つの欄を往復し、**再励磁ボタンが出たり消えたりする**。デバウンスを
  入れるかは運用判断（点滅を嫌って遅らせると、本当に応答が無い状態の表示も遅れる）
- **物理停止スイッチの検出が `pump_vac` 1 台頼み。** `RobotServer._detect_board_e_stop` は
  `GenericDriver` に絞っており、物理停止は DC 基板の `REF` にしか無いのでサブハンドで
  検出できるのは `pump_vac` だけ。しかも `received_at is None` で `continue` するため
  **DC 基板が黙っていると押されていても検出されない**。2026-09-10 の誤診と同型の
  「機体が動かないのに画面が理由を言わない」穴

## 6. 軸間干渉インターロックに残る穴

宣言（`axes.<軸>.guard.requires` / `.not_with`）と指令の入口での判定は入った
（`invariants.md` §4）。**今より弱くなる箇所は 1 つも無い**が、次の 2 つは残る。

- **塞げていない 2 手。** どちらも「1 指令の瞬間は条件を満たしていたが、移動中に相手が
  動く」形で、**周期監視を足さないと決めた**以上そのまま残る（理由 3 つは §4）:
  - `sub_rotate` の掃過中に `sub_y_axis` を前端へ動かす
  - `sub_y_axis` の移動中に `sub_lift` を下げる
- **軸ごとの零点確定済みフラグが無い。** `requires` は原点が確定した軸についてしか意味を
  持たないのに、確定したかどうかをサーバーが持っていない。未確定の `sub_lift` が偶然
  `top ± 1.0` を読めば素通りする。独立した機能なので今回は入れていない

## 7. `origin/main` との統合（2026-09-10）で持ち越したもの

`feat/sub-hand-control` を `origin/main` へ合流させたときに、片方へ寄せず持ち越した判断。

- **`config/main_hand_positions.yaml` の `rotate.home` を 0.0 から 5.0 へ動かした。** 0.0 は
  零点確定のスイッチの動作点そのもので、`tolerance` 2.0deg の到達帯が ON 区間（実測 約 2deg）へ
  食い込み、可動端インターロックが ON 区間の内側からの指令を拒む。**0.0 は相手が「実機の実測に
  合わせる」コミットで入れた値**なので、**メインハンド担当は実機で干渉しない角を実測して確定する
  こと**（`docs/mechanism_handoff.md` §1）
- **`rotate.pick_shared: 165.0` がどこからも参照されていない。** `_pick_at()` は `work_shared` も
  含め全列で `rotate: pick` を使う。足した側の作業が途中と思われるので消していない
- **軸間干渉の歯止めが 2 つある。** `guard.requires` / `.not_with`（移動そのものの禁止）と
  `interlocks:`（終状態の禁止）で、見ているものが違うので両方要る（`invariants.md` §4）。
  ただし**ピッチ × オフセットだけは 2 枚が重なっている**。1 つに寄せるなら、終状態を見る側が
  「連続して動かす手」を塞げている性質を失わないこと
- **出荷の動作確認（`sequences/motor_check.py`）の `sub_pitch` の段が `interlocks:` に拒否されていた**
  （`sub_offset` が `open` のまま `sub_pitch: close` を送っていた）。統合時に
  「閉じるとき オフセット → ピッチ」の順へ直した。**`origin/main` 単体では実機で止まる状態だった**
