# 構造変更と、安全機構の穴の棚卸し

日付が特定できないもの、および「事象」ではなく構造そのものを変えた作業の記録。
現況は `docs/architecture.md` / `docs/invariants.md` / `docs/checks_and_health.md` が正。

## 実装フェーズ Phase 1〜12（全 Phase 完了済み）

着手前のチェックリストとして積んだ作業分解表。**何をいつ作ったかの要約だけを残す**
（表の中身は git 履歴にある）。

| Phase | 作ったもの |
|---|---|
| 1 | CAN 通信レイヤ（TDD）。`lib/drivers/{base,m3508,edulite05,generic}.py` と `lib/can_manager.py` |
| 2 | シーケンスエンジン（TDD）。`@step` デコレータと `config/<robot>.yaml` |
| 3 | aiohttp サーバー + WebSocket（`lib/server.py` / `main.py`） |
| 4 | Web UI。**3 度作り直している** —— HeroUI v3（2026-04）→ 古典 TUI 風 / TuiCss（2026-06）→ Tailwind v4 + daisyUI 5 + React Router（2026-08）。以後の改修（レイアウト再設計・ライトテーマ化・`components/` の分割・二度押し・健全性判定の一本化・外周の整理・EMG STOP の配置）は `incidents.md` に日付ごとにある |
| 5 | ロボット固有シーケンスと位置定数（`lib/sequence/positions.py`）。`y_axis` の `scale` は `360*(3591/187)/(pi*m*z) = 55.0131`（ピニオン モジュール 1 / 歯数 40。ラックは基準円で転がるので分母は基準円周） |
| 6 | CAN ヘルスチェック（TDD）。`BusHealth` / `MotorHealth` と `GET /health`、`health_change` push。既定値は `feedback_timeout_ms: 500` / `temp_warning_c: 65` / `temp_critical_c: 80` / `tx_error_threshold: 96` |
| 7 | 試合運用フロー（コート / フェーズ / 指差喚呼） |
| 8 | モータアクセス層（`MotorHandle` / `AxisHandle`）と M3508 位置制御ループ |
| 9 | 励磁の有効化（EDULITE 05 の enable。現在角を書いてから enable する順序） |
| 10 | 手動操縦（`OperationMode` / `ManualController`） |
| 11 | アクチュエータ動作確認の両ハンド統合（`sequences/motor_check.py`）と零点確定（`lib/sequence/homing.py`）。モータ単位で駆動していた `lib/motor_check.py` の 471 行は丸ごと不要になった |
| 12 | PID 調整支援 —— **この Phase は機能ごと削除した。** `/pid-tuning` タブ・`set_param` コマンド・`lib/tuning/{advice,recorder,report}.py` は存在しない。残るのは `lib/tuning/metrics.py`（純関数）だけで、唯一の呼び出し元は `scripts/tune_y_axis.py` |

Phase 12 が解こうとしていた問題は記録に値する ——「調整できない」のは操縦者の技量ではなく
**判断材料が届いていなかった**ためだった: テレメトリ 20Hz に対し制御ループ 200Hz
（ナイキスト 10Hz。整定 100ms なら 2 点しか取れない）/ 目標値が配信されておらず**偏差そのものが
画面に存在しなかった** / 時系列が無く行き過ぎ（＝ピークの高さ）が原理的に読めない /
飽和が見えず、飽和中はゲインを変えても応答が変わらないので「効かない」という誤った結論に至る。

## 全域リファクタリング（2026-08-31）

**振る舞いを変えないことを条件にした整理。** ①同じ判断が 2 箇所以上にある状態を潰す
②本番から 0 参照の死にコードを撤去する ③テストが実装の写しになっている所を公開経路へ張り替える。
配信 JSON は 1 バイトも変えていない。検証は `pytest` 1461 passed（作業前 1448）/
`pnpm test:run` 616 passed・49 files（作業前 538・46）/ `pio test -e native` 153 cases（作業前 131）。

切り出したもの: `lib/ws_hub.py`（WS の唯一の配信経路）/ `lib/server_motor_check.py` /
`lib/server_dryrun.py` / `lib/health.worst_bus_health()` / `main._wire_one_robot` ほか 4 段 /
`firmware/lib/MotorCan` の `composeFeedbackFlags()` ほか / `scripts/_common.sh` /
`web/src/lib/motorCheckStatus.ts` / `tests/{server_fixtures,fake_can,feedback_frames}.py`。

**整理の途中で見つかった不具合**（どれも症状が画面か journal にしか出ない形だった）:

- **接続直後の 3 通**（`server_info` / `match_state` / `motor_check_state`）**が送信タイムアウトを
  通っていなかった** —— スリープに入りかけたノート PC が 1 台繋いだだけで接続ハンドラが返らない
- **`_load_sequence` が `dir()` の並び（アルファベット順）で最初のサブクラスを返していた** ——
  `sequences/sub_hand.py` が `MotorCheckSequence` を import しただけで
  `"MotorCheckSequence" < "SubHandSequence"` が成立し、サブハンドとして動作確認が登録された
- **動作確認の完了判定が 2 箇所にあり食い違っていた** —— パネル側は「実行中でなく、エラーも無く、
  ステップ表が届いている」を完了と読むので、**一度も実行していない状態が「完了」**になり
  全ステップに緑の ✓ が付いた（`running:false, step_index:0, total_steps:2`）。指差喚呼
  「アクチュエータ動作確認 完了」はその誤表示のままチェックが付く経路だった
- **配信 1 欄の欠落で全画面が白くなる経路があった** —— `describeSafetyIssues` が無検査で
  `.length` を呼び、呼び出し元がレンダー本体なので React ツリーごとアンマウントした
- **ジョグ量セレクトが未知の値で黙って 1 に落ちていた**（`steps` に浮動小数が入ると
  `indexOf` が −1）
- **`install.sh` / `deploy.sh` が未知の引数を黙って無視していた**（`--uninstal` がフル
  インストールを走らせる）
- **ファームの `MotorSafety::tryClear()` が偽の失敗経路を見せていた**（常に true を返し、
  呼び出し側 3 箇所とも戻り値を捨てていた）
- **電磁弁の CAN 受信フィルタが `CommandType` を参照していなかった**（リテラル直書き）。
  この enum は実際に一度動いている（`E_STOP` は `0b111` だった）

**変異テストで確認した層**のうち最も重いもの: `composeFeedbackFlags` を引き上げる前は、
**DC 基板と電磁弁基板に `flags |= status_flag::kReached;` を 1 行足しても 131 件すべて緑**
だった。また `_body_references`（しきい値 fallback が参照で書かれていることの検査）は
理由付きで用意されていたのに**呼び出し元が無く**、既定と同じ値のリテラルへ書き戻すと
**1447 件すべて緑のまま**だった（＝既定値の分散が復活した状態そのもの）。

## 零点確定を `rotate` へ移し、`y_axis` を一時無効化した

EDULITE の `SET_ZERO` 経路が入って `rotate` の原点確定が通るようになった一方、`y_axis` の
原点スイッチ（サーボ基板 #0 の SV4 / `0x44`）は**装着されていない**。実機で配線が済んでいるのは
SV3（`0x43` = `rotate_origin_sensor`）だけで、CAN 上で接触が読める（`00` ↔ `10`）ことは確認済み。

- **3 箇所（ファームの `kServoBoards[]`・`sensors:`・`axes.y_axis.homing`）は必ず同時に動かす。**
  `sensors:` から外してファームを `TouchSensor` のまま残すと、`homing:` が生きている限り
  「センサが config の `sensors:` に居ません」で**動作確認の最初のステップが毎回 `HomingError`**
  になり、症状が配線不良と区別が付かない
- **`set_zero_on_start: true` は零点確定が入っても残す。** `false` にすると 2 台の機械ゼロの差
  （**実機で 175.879deg**）が起動直後にそのまま偏差として現れ `SyncMonitor` が全体緊急停止を
  掛ける。機体が 1 ステップも動かせないので**動作確認そのものを開始できず、零点確定に
  たどり着く手前で詰まる**。2 つは順に効く（起動時の暫定原点 → ホーミングが上書き）が、
  **「搬送中に手で回されたぶん」は動作確認を回すまで残る**
- 指差喚呼に `rotate_holds` を足した —— `SET_ZERO` の順序が `disable → set_zero` なので、その
  数百 ms のあいだ `rotate` は保持トルクを持たない。自重で回るならスイッチで原点を決める方式
  そのものが成立せず、ソフトでは解決できない
- `kFirmwareVersion` は 4 → 5（デバイス ID `0x44` が `FEEDBACK` を送るかどうかがバイナリで
  変わるため。上げないと 2 種類のファームがどちらも v4 を名乗る）

**［2026-09-09 追記］上の 175.879deg は「`SET_ZERO` を 1 度も送っていない個体の機械ゼロが
左右で違う」という起動時の話であり、物理緊急停止から復帰できない症状の原因ではない。**
零点は電源断を跨いで残ることが実測で分かった（1〜2LSB）。復帰時のずれは**電源投入時に位置の
報告値が [0, 360) へ畳まれる**ことによる別物で、実測の偏差は 360.2853deg。値は
[`incidents.md`](incidents.md) の 2026-09-09、規則は `docs/invariants.md` §2。

## リファクタリングで見つけた安全機構の穴

### 零点確定の探索を実測位置起点にした【済】

かつて `home()` は `value = start_value + direction*step` の絶対値指令を出す一方、
`start_value` は既定 `0.0` のままで唯一の呼び出し元が渡していなかった。**1 歩目が原点近傍への
1 回のジャンプになり、`travelled` が指令の積算だったためその移動が `search_distance` を 1mm も
消費しなかった。** 踏むのは「一度原点確定した後」「手動で軸を動かした後」で、どちらも
セッティングタイムに普通に起きる操作である。

決着: 引数を増やさず**ランナーが毎ステップ実測値を読む**形（`AxisHandle.observed_value()`）へ。
`travelled` も実測位置の差分になり、事前確認は 4 つ（位置指令の軸か / 原点を確定する手段が
あるか / センサ鮮度 / **対象軸モータの鮮度**。最後が無いと未受信の `0.0` を現在位置と信じて
全ストロークぶんを 1 回で出す）。再アンカー方式では進まない機構が探索距離を消費しないので、
停滞判定（`step/2` 未満が 3 歩連続）が対になる。

### 触れた状態から始めたときに離脱してから寄せ直す【済】

かつては探索前にセンサが ON ならその場を原点として確定していた（`return 0.0`）。
**リミットスイッチの ON 区間には幅がある**ので、区間の奥で始めれば奥が、入口近くで始めれば
入口が原点になり、**ばらつきの幅は `step`（0.5mm）ではなく区間幅（数 mm）**になる。
**実運用ではセッティングタイムに機体を原点近くへ寄せてから起動するので、むしろ常態である。**
症状は「原点合わせをしたのに位置がずれる」だけで、始めた位置が毎回違うため再現もしない。

決着: 探索と逆向きへ離脱してから通常の探索へ渡す。離脱の歩数上限 `_RELEASE_STEP_LIMIT`（20 歩）は
`search_distance` を流用しない（あちらは実ストローク相当なので反対側の機構端まで走り抜ける）。

### `run_forever` が `CancelledError` を飲む【未着手】

`lib/sequence/engine.py` の `run_forever` が `except asyncio.CancelledError: break` で受けるため、
タスクが `cancelled()` ではなく**正常終了**として終わる。現状の後始末はどちらでも同じ経路を
通るので害は出ていないが、`asyncio.gather(..., return_exceptions=True)` で状態を判定する
書き方を将来入れると**静かに誤判定する**。修正は `break` → `raise` の 1 行だが、シャットダウンと
動作確認の起動の両方に触るので**低リスクな単独の変更として切る**のが適切。

### 実機・運用面で残っている主な穴【未着手】

- **`_e_stop_active` がプロセスメモリ上のみ。** サーバーを再起動すると緊急停止状態が消え、
  物理ボタンの状態と同期する仕組みも無い
- **緊急停止で fault がラッチされた場合の復帰手順が無い。** `encode_disable(clear_fault=True)` は
  送らない（原因を隠すため意図的）。実機で「解除しても動かない」なら fault の内容を確認して
  電源再投入
- **零点確定が有効なのは `rotate`（EDULITE）だけ。** `sub_y_axis` / `sub_lift`（DM3520）は
  スイッチを付けても `HomingError`（`SET_ZERO` の安全な順序が `disable` を要求し、`sub_lift` は
  disable すると自重落下する）。`y_axis`（M3508）は手段はあるがスイッチ未装着
- **`rotate` の `search_distance` 180.0deg は未実測。** `direction` −1 と `step` の下限
  （0.5deg では動かない）だけが実機で確定している
- **down したバスでも起動できてしまう。** `operstate` を見て起動ログへ ERROR は残すように
  なったが、起動は拒否しない（`--strict` を通していない構成を一律に潰さない判断）
- **`config/bench/main_hand/checklist.yaml` が実態とずれている。** この構成が開く `can_generic`
  の確認項目（`conveyor_run` / `origin_sensor_react` / `main_gripper_open` / `wall_initial` に
  相当するもの）が無く、`bench_return_home` の文言は走らないホーミングを前提にしている
- **`config/bench/y_axis_tuning/system.yaml` のコメントが本番と食い違う。** コメントは
  「保護値は本番と同じに保つ」だが、このセットの `sync_tolerance` は 2.0mm、本番は 10.0mm
- **200Hz が実機で維持できるかの実測データそのものが無い。** `PeriodicTask` が実周期の超過回数と
  最悪値を測り `match_finish` で journal へ 1 行出すようにはなったが、しきい値は理屈で導いた値で
  未検証なので**画面と WS 配信からは外してある**（順番が逆になっていた —— 先に実機で測って線を
  決めるべきところを、理屈だけの線を先に画面へ出していた）
- **低速域のスティックスリップは予測であって観測ではない。** この軸の静止摩擦は重い側で
  500counts 相当。出るなら「動かない → 誤差が溜まって急に動く」形で `sync_tolerance` の発報として
  現れうる
- **CAN 復旧の `down`/`up`（1 秒弱）が `command_timeout_ms` 500ms を満了させ、吸着中のワークが
  落ちる。** 試合中かどうかのゲートは意図的に置いていない（「バスが戻らない」ほうが重い）。
  運用で受ける —— journal に `[ WD ]` が出たらワーク落下を疑う（2026-09-05 に UI 表示を追加）
