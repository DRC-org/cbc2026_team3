# TODO — まだ手を付けていないこと

**未着手と未決の一覧。** 実装まで済んだら該当する文書（`invariants.md` /
`architecture.md` / `mechanism_handoff.md`）へ移し、ここからは消す。

## 1. サブハンドの試合シーケンス（未実装）

`sequences/sub_hand.py` は電磁弁とポンプだけの 4 段で、直動 2 軸とサーボ 3 軸を
一度も動かしていない（Issue #159）。段構成は決着済み。**未決は下の 1 点だけ**なので、
段構成はこのまま書ける。

- **位置定数の実測値。** 機構が付いておらず、サーボ基板 #2 も無応答なので測れない。
  決まっているのは名前と満たすべき条件だけ

### 全体像

ワークは **4 個**。取るのは**棚から**（前後軸で水平に寄せ、吸着高さまで下ろす）。置くのは
**箱へ上から入れる**（箱の上まで運んでから下ろす）。**取る位置は 4 個とも共通、置く位置は
4 個とも違う。**

**ポンプは吸気の `pump_vac` 1 つだけで、試合中は回しっぱなし。** 吸着パッドの弁は
**三方電磁弁**で、閉じた側が大気開放になる。だから**解放は弁を閉じるだけで済む**。

| 軸 | 役割 | 取る値 |
|---|---|---|
| `sub_rotate` | 姿勢。受け取り姿勢と搬送姿勢を切り替える | `receive` / `carry` |
| `sub_pitch` / `sub_offset` | 箱へ入れるための持ち方。**順序があるので別の段で動かす** | `open` / `close` |

### 段構成

**個数ぶん書き下す**（`sequences/main_hand.py` と同じ流儀。ジャンプで回さない）。
**初期位置 1 + 16 × 4 + 復帰 1 = 66 段。**

初期位置は「弁 全閉 / `pump_vac` 運転 / `sub_rotate: receive` / 持ち方 `open` /
`sub_lift: top` / `sub_y_axis: retracted`」。復帰は `sub_y_axis: retracted`
（他の軸は 16 段目の時点で初期位置と同じ）。

ワーク 1 個ぶんの 16 段（`N` は 1〜4）:

| # | ステップ | 動かすもの | 確認 |
|---|---|---|---|
| 1 | 棚へ寄せる | `sub_y_axis: receive` | 要 |
| 2 | 吸着高さへ下降 | `sub_lift: pick` | — |
| 3 | 吸着 | 選ばれたパッドの弁だけ開 | 要 |
| 4 | 持ち上げ | `sub_lift: top` | — |
| 5 | 回転可能位置へ後退 | `sub_y_axis: clear` | — |
| 6 | 搬送姿勢へ | `sub_rotate: carry` | — |
| 7 | 箱の上へ | `sub_y_axis: place_N` | — |
| 8 | オフセットを閉じる | `sub_offset: close` | — |
| 9 | ピッチを閉じる | `sub_pitch: close` | — |
| 10 | 箱へ下降 | `sub_lift: place` | 要 |
| 11 | 解放 | 弁を閉（三方弁で大気開放） | 要 |
| 12 | 上昇 | `sub_lift: top` | — |
| 13 | ピッチを開く | `sub_pitch: open` | — |
| 14 | オフセットを開く | `sub_offset: open` | — |
| 15 | 回転可能位置へ後退 | `sub_y_axis: clear` | — |
| 16 | 受け取り姿勢へ | `sub_rotate: receive` | — |

### 崩してはいけない順序

- **前後に動かすのは `sub_lift` が `top` のときだけ**
- **`sub_y_axis` が前端スイッチから 150mm 以内に居るあいだ `sub_rotate` を回さない**
  （機構が干渉する）。`receive` も `place_1`〜`place_4` も 150mm 未満なので、
  **`clear` を経由しない限り回さない**構成にする
- **`sub_pitch` と `sub_offset` は一緒に動かさない。** 閉じるときは
  **オフセット → ピッチ**、開くときは**ピッチ → オフセット**
- **棚へ寄せる段は操縦者の確認で止める**（メインハンドからワークが送られてくるとき、
  サブハンドが `receive` に居ると引っかかる）

### 位置定数に足すもの

| 軸 | 名前 | 満たすべき条件 |
|---|---|---|
| `sub_y_axis` | `retracted` | 収納位置。−440 〜 2mm の内側 |
| | `clear` | 回転可能位置。**前端スイッチから 150mm 以上離す** |
| | `receive` | 棚から取る位置。**1 つだけ**（4 個とも共通）。前端スイッチの手前（作動点そのものは可動端インターロックが拒否する） |
| | `place_1`〜`place_4` | 箱 4 箇所。**4 個とも違う** |
| `sub_lift` | `top` | 移動高さ。前後に動かすのはこの高さでだけ。−152 〜 2mm の内側 |
| | `pick` | `top` の 10mm 下。棚のワークに吸着する高さ |
| | `place` | 箱へ下ろす高さ。**1 つだけ**（箱 4 箇所とも同じ） |
| `sub_rotate` | `receive` / `carry` | 左右ペア。逆回転側の `offset`（折り返し点）を実測してから |
| `sub_pitch` / `sub_offset` | `open` / `close` | — |

## 2. リミットスイッチを NO から NC へ替える

極性は `firmware/servo/include/config.h` の `sensorActiveLow`（基板 #1 の SV1〜SV4 が
`true`）にあり、**yaml では切り替えられない**。替えるならファームの反転・書き込み・
`kFirmwareVersion` と `expected_firmware` の同時更新がセットになる。

## 3. メインハンド `y_axis` の左右位置ずれ

左右の M3508 が 106.7 と 211.4 で、互いに逆向きのトルクが出ている。軸の値は −14.0mm で
宣言範囲の外。この状態で零点合わせを回すと失敗する。**原因が分かるまでメインハンドは
動かさない。**

## 4. サーボ基板 #2（`0x50`〜`0x54`）が無応答

サブハンドのサーボ 5 スロットが全部この基板。基板 #1（`0x48`〜`0x4C`、可動端スイッチ
4 本を含む）は生きている。ハードウェア側の切り分け待ち。

## 5. 非常停止中のログが送信失敗で埋まる

モータ電源が落ちている間、3 バスとも送信が `Transmit buffer full` で失敗し、例外の
スタックトレースが数秒おきに出る（40 秒で 400 件超）。**動作には影響しない**
（イベントループが固まる不具合は別途修正済み）が、他のログが読めなくなる。1 行に畳む。

## 6. 可動端の保護に残る穴

**どちらへ寄せるかは決着した。** PR #161（`lib/control/limit_guard.py`）は閉じ、main の
`lib/control/limit_monitor.py` と `lib/motion_guard.py` を正として **main に無いものだけを
足す**形で PR #167（`feat/limit-guard-on-motion-guard`）へ作り直してある。**まだ draft で
未マージ**なので、それが入るまで main には次が残る。

- **50Hz でセンサの現在値しか見ていない。** ON 区間がそれより狭い接触は落とす
- **止めた移動が「到達」と読まれる。** `LimitMonitor` は目標を実測位置へ書き直すので
  `is_reached` が必ず成立し、軸は途中に居るのにシーケンスだけが次へ進む
- **メインハンドの 3 本が未保護。** `guard:` があるのは `config/sub_hand_positions.yaml` の
  2 軸だけ

## 7. 励磁の誤診（2026-09-10）を調べる途中で見つかったもの

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
- **物理停止スイッチの検出が `pump_vac` 1 台頼み。** `RobotServer._detect_board_e_stop` は
  `GenericDriver` に絞っており、物理停止は DC 基板の `REF` にしか無いのでサブハンドで
  検出できるのは `pump_vac` だけ。しかも `received_at is None` で `continue` するため
  **DC 基板が黙っていると押されていても検出されない**。2026-09-10 の誤診と同型の
  「機体が動かないのに画面が理由を言わない」穴
