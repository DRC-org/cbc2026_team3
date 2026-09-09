# EDULITE 05 の原点が電源断を跨ぐか — 実機確認手順

物理緊急停止を押すとモータ電源が落ち、`SET_ZERO`（通信タイプ 0x06）で切った原点が失われる
（マニュアルに "lost after power failure"）。復帰すると `rotate_r` / `rotate_l` の原点差が
同期ずれとして現れ、`SyncMonitor` が緊急停止を再発動する。

「原点をモータ側の `SET_ZERO` ではなく PC 側のオフセットで持つ」設計へ移せるかは、次の 1 点に
かかっている。**まだ実測されていないのはここだけである。**

> **EDULITE 05 は電源を切って入れ直しても、同じ生角度（フラッシュの機械ゼロ基準の絶対角）を
> 返すのか。**

この文書は、それを実機で確かめる手順である。使う道具は
[`scripts/edulite_origin_probe.py`](../scripts/edulite_origin_probe.py)。

---

## 道具

```bash
uv run python scripts/edulite_origin_probe.py --label before-estop
```

- 読むのは `config/main_hand.yaml` の `rotate_r` / `rotate_l`（軸は `--axis`、ロボットは
  `--config` で変えられる）。バス名・CAN ID・`host_id`・`scale` はすべて config から解決する
- 出力: 生角度（rad / deg）、`main_hand_positions.yaml` の `scale` で割った論理角 deg、
  左右の偏差（`SyncGroup.deviation` と同じ計算）、`mode_state`、fault ビット、
  `run_mode` / `limit_spd` / `limit_cur` / `loc_kp`
- 既定で 5 回読む（`--samples`）。ばらつきが「電源断で動いた量」に埋もれていないかを見るため
- `--json out.json` を付けると、比較用に同じ値をファイルへ残せる

### 送るフレーム

| 既定 | 内容 |
|---|---|
| `disable`（0x04） | フィードバックを返させる唯一の無励磁フレーム。`feedback_probe_message()` |
| `READ_PARAM`（0x11） | パラメータの読み出し。書き込みではない。`--no-read-param` で止められる |

**励磁（`enable`）も位置指令も 1 通も送らない。** `SET_ZERO` は `--set-zero` を明示したときだけ
送り、送る前に何が書き換わるかを表示して確認を取る。

### 走らせる前に

- **サーバー（`main.py`）を止めること。** `rotate_r` / `rotate_l` は
  `set_zero_on_start: true` なので、サーバーが起動した瞬間に `SET_ZERO` が飛び、確かめたい
  零点がその場で書き換わる。ツールは起動時に 0.3 秒バスを聞き、自分が 1 通も送っていないのに
  フィードバックが流れていたら「他プロセスが使っている」として実行を拒否する
- **機構が自重で動く姿勢なら支えておくこと。** このツールは励磁しないので、無励磁のまま
  落ちる姿勢に置いてはならない（読んでいる間ずっと無励磁のままである）

---

## 手順

値の読み取り分解能は uint16 で ±12.57rad を刻んだ **1LSB ≈ 0.000384rad ≈ 0.022deg**。
「一致」とは **0.05deg 以内**、「動いた」とはそれを明確に超えることを指す。
機構は手順のあいだ**一切動かさない**（動かしたら比較の意味が無くなる）。

### 1. サーバーを止めた状態で読む

```bash
uv run python scripts/edulite_origin_probe.py --label 1-before --json /tmp/probe-1-before.json
```

記録する: `rotate_r` / `rotate_l` の生 deg、偏差、`mode_state`、fault、パラメータ 4 つ。

### 2. 物理緊急停止 → 数秒待つ → 解除 → もう一度読む

1. 物理緊急停止を押す（モータ電源が落ちる。CAN トランシーバもモータ電源から取るので、
   このあいだ EDULITE はバス上から消える）
2. **5 秒以上待つ**（フラッシュの保持ではなく電源が本当に落ちきったことを確かめるため）
3. 解除して電源を戻す。モータの起動を 2 秒ほど待つ
4. **機構には触れないまま**もう一度読む

```bash
uv run python scripts/edulite_origin_probe.py --label 2-after --json /tmp/probe-2-after.json
```

### 3. 手順 1 と 2 を突き合わせる

生 deg が一致するか（＝機械ゼロが電源断で不動か）を見る。

| 手順 2 の生角度 | 読めること | 次にやること |
|---|---|---|
| 手順 1 と 0.05deg 以内で一致 | 機械ゼロは電源断で不動。**生角度は電源を跨ぐ絶対角として使える** | PC 側オフセット設計へ進んでよい。オフセットは「零点確定時の生角度」を保存する形になる |
| 電源投入時の姿勢が 0 になる（両輪とも 0 付近） | 生角度は絶対ではなく起動時姿勢基準。**PC 側オフセットでは原点を持てない** | 復帰のたびに原点センサでホーミングし直す設計にする |
| 一致も 0 でもなく毎回違う | 電源断でフラッシュが書き換わっているか、読み方が間違っている | `mode_state` と fault（特に `UNCALIBRATED` / `MAG_ENCODER`）を先に見る |

### 4. `SET_ZERO` した零点が電源断を跨ぐか

手順 3 で「不動」と出た場合でも、**`SET_ZERO` した零点まで残るとは限らない**。ここを分けて
確かめる。

```bash
# 今の姿勢を零点として書き込む (確認プロンプトが出る)
uv run python scripts/edulite_origin_probe.py --label 4-setzero --set-zero
```

送信直後の読み値が 0 付近になっていることを確認したら、**機構に触れないまま**手順 2 と同じく
物理緊急停止 → 5 秒 → 解除を行い、もう一度読む。

```bash
uv run python scripts/edulite_origin_probe.py --label 4-after --json /tmp/probe-4-after.json
```

| 手順 4 の電源断後の値 | 読めること |
|---|---|
| 0 付近のまま | `SET_ZERO` の零点はフラッシュに残る。マニュアルの "lost after power failure" と食い違うので、**別の個体でもう一度確かめてから**信用すること |
| 手順 1 の値へ戻る（機械ゼロ基準） | マニュアルどおり `SET_ZERO` は電源断で失われる。**復帰時の左右差はこれが原因**で、`set_zero_on_start: true` に原点を預ける設計は成立しない |

### 5. パラメータが電源断で失われるか

手順 1 と手順 2 の `run_mode` / `limit_spd` / `limit_cur` / `loc_kp` を比べる。

| 比較 | 読めること | 次にやること |
|---|---|---|
| 同じ | 電源断で設定は残る | 今の `initialization_steps()`（起動時 1 回だけ書く）のままでよい |
| 出荷値・既定値へ戻る | 電源断で設定が失われる | DM3520 と同じく再励磁のたびに書き直す必要がある。**ただし `SET_ZERO` はそこへ移してはならない**（移すと再励磁のたびに原点がその場の姿勢へ書き換わる。`lib/drivers/edulite05.py` の `initialization_steps()` の docstring と `docs/invariants.md` §3） |
| `読めなかった (応答なし)` | この個体・このファームは `READ_PARAM` に答えない | パラメータの保持は別の方法（書き込み前後の挙動）でしか確かめられない。**0 とみなして先へ進まないこと** |

### 6. まとめて何が言えるか

| 手順 3 | 手順 4 | 結論 |
|---|---|---|
| 生角度が不動 | `SET_ZERO` は失われる | **狙いどおり。** 原点は PC 側オフセット（零点確定時の生角度）で持ち、`set_zero_on_start` をやめる。復帰後は生角度からオフセットを引いて論理角を作れるので、左右差は再発しない |
| 生角度が不動 | `SET_ZERO` も残る | どちらでも実装できる。PC 側オフセットの方が「モータのフラッシュ寿命を使わない」「値がログに残る」ぶん有利 |
| 生角度が不動でない | — | **PC 側オフセットは成立しない。** 電源復帰のたびに原点センサでホーミングし直す設計しか採れない（`rotate` には `rotate_origin_sensor` がある） |

---

## 記録テンプレート

結果は `docs/history/` へ日付付きで残す。最低限これだけ埋まっていれば判定できる。

```
日付 / 個体:
手順1  rotate_r 生deg =        rotate_l 生deg =        偏差 =
       mode_state =            fault =
       run_mode =   limit_spd =   limit_cur =   loc_kp =
手順2  rotate_r 生deg =        rotate_l 生deg =        偏差 =
       mode_state =            fault =
       run_mode =   limit_spd =   limit_cur =   loc_kp =
手順4  SET_ZERO 直後 rotate_r =        rotate_l =
       電源断後     rotate_r =        rotate_l =
判定   機械ゼロ: 不動 / 不動でない      SET_ZERO: 残る / 失われる
       パラメータ: 残る / 失われる / 読めなかった
```
