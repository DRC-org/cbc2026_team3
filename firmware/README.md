# firmware/ — 自作モータドライバのファームウェア

自作モタドラ（Arduino UNO R4 / Renesas RA4M1）の PlatformIO プロジェクト群。
PC 側 `lib/drivers/generic.py` と CAN で対向する。

**プロトコルの単一情報源は `docs/motor_driver_can_protocol.md`。**
フレーム定義・安全機構の仕様はすべてそこにあり、ファームと PC 側の片方だけを
変更してはならない。

## 構成

```
firmware/
  common.ini           両プロジェクト共通のビルド設定（各 platformio.ini が extra_configs で読む）
  lib/
    MotorCan/          全ファーム共通。Arduino 非依存の純 C++
      src/MotorCanProtocol.{h,cpp}   CAN ID / フレームの符号化・復号 + PC 側との契約既定値
      src/MotorCanRouter.{h,cpp}     受信フレームの宛先判定・DIP オフセット・DIP 読み出し
      src/MotorControlTarget.h       ControlTarget（制御モードと目標値を 1 つの状態にする）
      src/MotorLoopTimer.h           PeriodicTimer（millis() 折り返しに耐える周期判定）
      src/MotorSafety.{h,cpp}        緊急停止ラッチ + コマンドウォッチドッグ
      src/MotorPid.{h,cpp}           position / velocity 用 PID（DC 用のみ使用）
      src/SerialLineBuffer.{h,cpp}   デバッグシリアルの行組み立て（行の解釈は各 main.cpp）
      src/ServoMotion.{h,cpp}        角度補間・可動範囲クランプ・到達推定（サーボ用のみ使用）
      src/ServoChannel.{h,cpp}       上 2 つの結線（出力禁止中は指令を受け付けず先に凍結する）
  test/                native 環境の Unity テスト。両プロジェクトが test_dir = ../test で共有
    test_protocol/     プロトコル層・安全機構・PID・制御目標
    test_board/        宛先判定・デバイス ID 解決・周期タイマ・シリアル行
    test_servo/        角度補間・可動範囲クランプ・到達推定・安全機構との結線
  dc_motor/            DC モータ用モタドラのファーム
    platformio.ini     固有行のみ（default_envs / extra_configs / test_dir）
    include/config.h   ピン配置と機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・制御ループ・CAN 送受信
  servo/               サーボ用モタドラのファーム（1 枚で複数チャンネル）
    platformio.ini     同上
    include/config.h   ピン配置・チャンネル表・機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・補間ループ・CAN 送受信
```

`MotorCan` が `Arduino.h` を include しないのは意図的で、PC 上の native 環境で
そのままコンパイルしてテストできるようにするため。`dc_motor/` と `servo/` は
`lib_extra_dirs = ../lib` で同じ `MotorCan` を共有し、テストも `firmware/test/` を共有するので、
**`pio test -e native` はどちらのプロジェクトから回しても同じ全ケースが走る。**
一方**実機ビルド（`pio run`）は両方で確認すること。** 共有しているのは `MotorCan` までで、
`main.cpp` と `config.h` は別物のため。

ビルド設定の実体は `common.ini` にある。2 つの `platformio.ini` がコメント以外まったく
同じ内容を持っていると、片方だけを直したことに誰も気付けない。

## コマンド

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
# ユニットテスト（実機不要）。どちらも firmware/test/ の全ケースが走る
pio test -e native -d firmware/dc_motor
pio test -e native -d firmware/servo

# ビルド
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e uno_r4_minima -d firmware/servo

# 書き込み（サーボ基板）
pio run -e uno_r4_minima -d firmware/servo -t upload
pio device monitor -e uno_r4_minima -d firmware/servo

# 書き込み（基板を USB で接続してから）
pio run -e uno_r4_minima -d firmware/dc_motor -t upload
pio run -e uno_r4_minima -d firmware/dc_motor -t upload --upload-port /dev/ttyACM0

# シリアルモニタ（115200 baud）
pio device monitor -e uno_r4_minima -d firmware/dc_motor

# クリーン
pio run -e uno_r4_minima -d firmware/dc_motor -t clean
```

**基板は DC 用・サーボ用とも UNO R4 Minima。** CAN ペリフェラルは `D4`(TX)/`D5`(RX) に
固定されているので、このピンを他用途へ割り当ててはならない。割り当てると CAN が上がらず
**PC から止められない基板**ができあがる。各 `main.cpp` の `static_assert` が
`config.h` のピンと `PIN_CAN0_TX` / `PIN_CAN0_RX` の衝突をビルド時に検出する。

初回ビルドではツールチェーン（`toolchain-gccarmnoneeabi`）と Arduino コアが
ダウンロードされるためネットワークが必要。以降はオフラインでビルドできる。

## デバッグ用シリアル（dc_motor）

USB CDC の `Serial`（115200 baud）から duty を直接入力できる。

- `0.3` のような数値を送ると duty モードに切り替わって その duty で回る
- `s` を送ると停止し、シリアル操作モードを抜ける
- CAN から `SET_TARGET` が来たらシリアル操作モードは自動的に解除される
- **緊急停止ラッチ中はシリアルからも駆動できない**

シリアル操作中はコマンドウォッチドッグを養い続ける（1 回だけ養う実装だと
`command_timeout_ms` 後に必ず止まってデバッグにならないため）。
不要なら `config.h` の `ENABLE_SERIAL_DEBUG` を 0 にする。

`SW2`/`SW3`（DIP スイッチ）は D1/D0 = ハードウェア UART と同じピンなので、
**`Serial1` を開いてはならない**。開くと DIP が読めずデバイス ID が化ける。

## デバッグ用シリアル（servo）

USB CDC の `Serial`（115200 baud）から角度を直接入力できる。

- `0 5.0` のように「`<チャンネル番号> <角度[deg]>`」を送るとそのチャンネルへ角度指令。
  **チャンネル番号と角度は空白で区切る。** 区切りが無い行は捨てる（番号を読み違えると
  別のサーボが動くので、曖昧な入力は指令にしない）
- `s` を送ると全チャンネルを現在角で凍結し、シリアル操作モードを抜ける
- CAN から `SET_TARGET` が来たらシリアル操作モードは自動的に解除される
- **緊急停止ラッチ中はシリアルからも駆動できない**（角度も `angle_min`/`angle_max` でクランプされる）

サーボ基板の DIP は A0〜A3 で UART とは重ならないため、`Serial` の使用に制約はない。

## デバイス ID

### dc_motor — DIP がそのままデバイス ID

基板上の DIP スイッチ 4bit（`INPUT_PULLUP` の負論理、LOW = 1）で設定する。
`SW0`=bit0, `SW1`=bit1, `SW2`=bit2, `SW3`=bit3。

`0x00`（全 OFF）は「設定忘れ」とみなし、**駆動を拒否して LED を速く点滅させる**
（仕様書 §2.2）。`FEEDBACK` の bit5 も立つ。設定ミスで意図しないアクチュエータが
動くより、動かない方が安全なため。

### servo — チャンネル表 + DIP オフセット

サーボ基板は 1 枚で複数のサーボを駆動し、**チャンネルごとに独立したデバイス ID を持つ**
（仕様書 §7.1）。PC からは別々のモータとして見え、`FEEDBACK` もチャンネルごとに
別の CAN ID で送る。チャンネル表は `firmware/servo/include/config.h` の
`kServoChannels[]` にあり、既定は `config/main_hand.yaml` の実構成に合わせてある。

| ch | 基準デバイス ID | モータ | 既定ピン |
|---|---|---|---|
| 0 | `0x01` | `gripper` | D9 |
| 1 | `0x03` | `wall_f` | D10 |
| 2 | `0x04` | `wall_r` | D11 |

**DIP はデバイス ID そのものではなく、チャンネル表全体に加えるオフセットとして働く。**
DC 用の「DIP の値 = デバイス ID」とは意味が違う。同一ファームの基板を複数枚使うとき、
2 枚目の DIP を 1 段上げるだけで全チャンネルの ID がまとめてずれるようにするため。

- DIP = `0` → ID は `0x01` / `0x03` / `0x04`（＝ `config/main_hand.yaml` そのまま）
- DIP = `4` → ID は `0x05` / `0x07` / `0x08`

オフセット適用後の ID が `0x00`（未設定）または `0xFF`（`E_STOP` ブロードキャスト用に予約）
になったチャンネルは**駆動しない**。そのチャンネルは `PwmOut::begin()` すら通さないので
パルスが 1 発も出ず、`FEEDBACK` の bit5 が立ち、LED が速く点滅する。

デバイス ID の割り当ては仕様書 §2.2 の表と `config/*.yaml` を参照。
**デバイス ID はバス単位でロボット横断に一意**でなければならない。

## サーボの到達フラグは推定値（重要）

**サーボは位置フィードバックを返さないため、`FEEDBACK` bit0（目標到達）は実測ではなく
ファームの推定である**（仕様書 §7.3）。指令角と現在角の差をスルーレート（`slew_rate`）で
割った所要時間が経過し、補間が完了した時点で bit0 を立てているだけで、
サーボが実際にそこへ行ったかは一切見ていない。

したがって **脱調・過負荷・メカ干渉で実際には動いていなくても「到達」と報告する。**
PC 側 `move_to` はこのフラグで次のステップへ進むため、**機構が引っかかっていても
シーケンスは進んでしまう。** 危険な動作には必ず `require_trigger` を付けて
人間の目視確認を挟むこと（`robots/main_hand.py` のハンド閉じ等）。

同じ理由で `FEEDBACK` の位置は「補間中の指令角」、速度は「そのときのスルーレートの
rpm 換算」であり、電流・温度・過電流・過熱は検出手段が無いので常に 0 を送る（仕様書 §7.4）。

## config.h の要確認項目（通電前に必ず）

### dc_motor

`firmware/dc_motor/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。
基板・データシート・実測と突き合わせること。**下の表と config.h のマーカーは 1 対 1 で対応する。**
表にあってマーカーが無い（またはその逆の）項目を作ると、通電前チェックリストとして
どちらを信じればよいか分からなくなるため、項目を増やすときは必ず両方に入れる。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kDisActiveHigh` | `true` | ゲートドライバ `DIS` の論理。HIGH でアサート（出力禁止）と仮定 | **反転していると緊急停止でモータが全力で回る。最優先で確認** |
| `HAS_ENCODER` | `1` | エンコーダの有無。無ければ 0（position/velocity 制御が使えなくなる） | 無いのに 1 だと位置・速度が 0 のまま PID が振り切れる |
| `HAS_CURRENT_SENSE` | `1` | 電流センス回路の有無。無ければ 0（電流は常に 0・過電流を検出しない） | **無いのに 1 だと SENS ピン（A0）が浮き、ADC の振れがしきい値を跨いで必ず誤発火する**（仕様書 §3.2） |
| `kEncoderPulsesPerMotorRev` | `500` | エンコーダ 1 相あたりのパルス数（モータ軸） | 位置・速度の換算が丸ごとずれる |
| `kGearRatio` | `30.0` | 減速比。`FEEDBACK` は出力軸換算で送る（PC 側は換算を知らない） | 同上 |
| `kEncoderDirectionSign` | `1.0` | 正の duty で速度が正になるか。A/B の配線で反転する | 位置・速度制御が正帰還になって暴走する |
| `kCurrentSenseZeroCount` | `512` | 無電流時の ADC カウント（10bit, 0–1023） | 過電流フラグが誤検出／未検出になる |
| `kCurrentSenseMaPerCount` | `20.0` | ADC カウント → mA の換算係数。しきい値がフルスケール偏差の 80% 以下に収まること | 同上 |
| `kDefaultOvercurrentThresholdMa` | `5000` | モータとドライバ IC の連続定格 | 同上 |
| `kDefaultKp` / `kDefaultKi` / `kDefaultKd` | `0.01` / `0` / `0` | PID ゲイン。position と velocity で 1 組を共有する（仕様書 §3.4） | 発振・目標に届かない |

**電流センスの 3 つの仮値（`kCurrentSenseZeroCount` / `kCurrentSenseMaPerCount` /
`kDefaultOvercurrentThresholdMa`）は互いに噛み合っていなければならない。**
しきい値が ADC のフルスケール偏差（0 点から近い側のレールまで × 換算係数）に近いと、
正常な回路でもレール付近でしか発報できない「効いているつもりの保護」になる。
しきい値は連続定格という物理量なので、合わせるのは換算係数の側。
`config.h` の `static_assert` がしきい値をフルスケールの 80% 以下に強制し、
成立しない組み合わせをビルド時に弾く（実機では「過電流を一度も検出しない」という
無症状でしか現れないため）。

温度センサはこの基板に無く、`FEEDBACK` の温度は常に 0 を送る
（仕様書 §3.2 / §8 の既知の制限）。PC 側の温度警告は発火しない。

`HAS_RGB_LED` は dc_motor / servo とも既定 `0` で、**点灯処理はまだ無い**
（`updateLed()` の `#if HAS_RGB_LED` は TODO コメントだけ）。1 にしても状態表示は
オンボード LED の点滅のままなので通電前に確認することは無く、上の表には載せていない。
発光させるには `platformio.ini` の `lib_deps` にライブラリを追加したうえで中身を書くこと。

### servo

`firmware/servo/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。
表にはそれに加えて、仮値ではないが機構ごとに判断が要る既定値
（`initialAngleDeg` / `kEStopDetach`）も入れてある。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kServoChannels[].limits.angleMinDeg` / `angleMaxDeg` | `0.0` / `30.0` | 機構を付けた状態で当たらない可動範囲を実測する | **広すぎるとサーボがメカストッパに当たったまま停動し、短時間で焼損する。最優先で確認**（狭すぎる分はクランプで止まるだけ） |
| `kServoPulse270` | `{500, 2500, 270.0}` | サーボのデータシートのパルス幅と可動角 | 指令角と実角がずれる。上端で当たり続ける |
| `kServoPwmPeriodUs` | `20000`（50Hz） | サーボが許容するフレーム周期。デジタルサーボなら上げられる | 対応しない個体に速い周期を与えると発熱・ジッタ |
| `kPinServoCh0/1/2` | `9` / `10` / `11` | 基板のサーボ信号線。**CAN のピンと重ならないこと** | `main.cpp` の `static_assert` がビルド時に弾く |
| `kPinDip[4]` | `{14,15,16,17}`（A0–A3） | 基板の DIP がどのピンに落ちているか | オフセットが化けて別のアクチュエータが動く |
| `kServoChannels[].initialAngleDeg` | `0.0` | 電源投入時に持っていく角度 | 起動した瞬間に機構が動く |
| `kEStopDetach` | `false` | 緊急停止時に脱力させたい機構があるか | `true` にすると壁が自重で倒れ、把持中のワークを落とす |

**UNO R4 Minima の CAN ペリフェラルは `D4`(TX)/`D5`(RX) に固定されている。**
チーム提供のサンプルは `SV0..SV3` を `D4`〜`D7` に置いているが、この配線は CAN と
正面衝突するためそのままでは使えない。`servo/src/main.cpp` の `static_assert` が
サーボ出力ピン・ステータス LED と `PIN_CAN0_TX` / `PIN_CAN0_RX` の衝突、および
チャンネル間のピン重複をビルド時に検出する。

## 安全に関する既定値

| 項目 | dc_motor | servo | 根拠 |
|---|---|---|---|
| 出力上限 | `max_duty` = `0.30` | `angle_min` / `angle_max` でのクランプ | 仕様書 §5.3 / §7.2。サーボは可動範囲外で停動すると焼損する |
| `command_timeout_ms` | `500` | `500`（**チャンネルごとに独立**） | 仕様書 §5.1 / §7.1。ラッチしない |
| `feedback_interval_ms` | `10` | `10`（チャンネルごとに位相をずらして送信） | 100Hz。緊急停止中・ウォッチドッグ作動中も送り続ける |
| ウォッチドッグ有効/無効 | `WATCHDOG_ENABLED` = `1` | 同左 | 仕様書 §5.1 / §8。`SET_PARAM` からは変更できない |
| 緊急停止・ウォッチドッグ時 | 出力停止（コースト）。position モードの解除は現在位置で凍結 | **現在角を保持。出力禁止中の `SET_TARGET` は採用しない** | 仕様書 §7.5 / §3.5。サーボは脱力すると壁が倒れワークを落とす。受け付けると再送のたびに補間が再アンカーされ、ラッチ中に動く |
| 受け付けるモード | position / velocity / duty | **position のみ**（他は無視） | 仕様書 §7.2 |
| 起動時 | duty モード / 目標 0 / 出力停止 | 各チャンネル `initialAngleDeg` / 緊急停止ラッチ解除済み | 仕様書 §5.4 |

`FEEDBACK` はチャンネルごとに送信タイミングを周期内でずらしてある。
全チャンネルが同時に送るとフレームのバーストになり、他バスの周期送信と重なったときに
調停待ちが伸びて送信間隔が波打つため。

**PC 側は最後に指令した目標値を `command_timeout_ms` 以内に再送し続けること**
（仕様書 §5.1 の契約。既定 500ms なら 50ms 周期 = 20Hz が目安）。再送が途切れると
コンベアは 500ms で止まり、サーボは現在角を保持したまま新しい角度指令を受け付けなくなる。
これは運用上の異常なので、`command_timeout_ms`（`SET_PARAM` ID `0x04`）を伸ばしたり
`WATCHDOG_ENABLED` を 0 にして覆い隠してはならない（仕様書 §8）。

**`command_timeout_ms`（`SET_PARAM` `0x04`）は 50〜2000ms に丸められる。**
上限が無いと、`WATCHDOG_ENABLED` を CAN から触れないようにしていても、猶予を
49.7 日へ伸ばすだけで同じ結果（ウォッチドッグの実質無効化）になるため（仕様書 §3.4）。

**`command_timeout_ms` と `feedback_interval_ms` の既定値は `config.h` に無い。**
PC 側の再送周期と STALE 判定が前提にしている値、つまり PC 側との契約なので、
`lib/MotorCan/src/MotorCanProtocol.h` の `kDefaultCommandTimeoutMs` /
`kDefaultFeedbackIntervalMs` が唯一の定義を持つ。両基板の `config.h` に同じ数字を書くと、
仕様が動いたとき片方だけが古くなる。`max_duty` や PID ゲインのようにアクチュエータ単位で
変える値は従来どおり各 `config.h` にある。

**`WATCHDOG_ENABLED` は `config.h` のビルド時フラグだが、判定は実行時に行う。**
`setup()` が値を `MotorSafety::setWatchdogEnabled()` へ写し、駆動ゲートは
`MotorSafety::isOutputAllowed()` だけを通す。`#if` を各 `main.cpp` に置くと同じ分岐を
両ファームが各自で持つことになり、片方に入れ忘れても誰も気付けない。写し忘れた場合は
有効側（＝安全側）に倒れる。**CAN の `SET_PARAM` にこのフラグの ID は無く、
1 フレームで最後の砦が外れる経路は存在しない。**

**無効にしても「最初の `SET_TARGET` まで出力禁止」（仕様書 §5.4）は外れない。**
外れる基板は CAN 通信を 1 通も受けないまま `setup()` でゲートドライバを開く。
ベンチ確認の逃げ道は残っていて、最初の `cansend` でゲートが開く。

## テストの方針

`firmware/test/` は `MotorCan` のみを対象とし、Arduino に依存しない。両プロジェクトが
`test_dir = ../test` でここを指すので、どちらから `pio test -e native` を回しても同じ
全ケースが走る。片方のプロジェクトの下にだけ置くと、もう一方だけを回した人が共有
ライブラリの回帰を検出できない。

`test_protocol/` はプロトコル層・安全機構・PID・制御目標を対象とする。
実機が無くても以下を検出できる。

- CAN ID の組み立て／解析（予約値 `0b100`/`0b101`/`0b110` を無効として弾くこと）
- float32 リトルエンディアンの往復と既知バイト列の一致
- `E_STOP` の解除がマジックバイト `0x5A` `0xA5` 揃いのときだけ通ること
- `FEEDBACK` の位置・速度・電流が int16 で折り返さず飽和すること
- ウォッチドッグの満了・復帰・`millis()` 折り返し、ラッチ中も養えること
- ウォッチドッグを無効にしても緊急停止ラッチと「最初の指令まで出力禁止」は効くこと
- `command_timeout_ms` / `feedback_interval_ms` が範囲へ丸められ、NaN では現在値を保つこと
- `SET_TARGET` / `SET_PARAM` の NaN を捨てること、PID が NaN から自己回復すること
- duty クランプ（`max_duty` 超過・負値・0・NaN）
- PID のリセットとワインドアップ制限
- モード切替で目標値が 0 に落ち、同じモードの再指令では落ちないこと

`test_board/` は基板共通部（`MotorCanRouter` / `PeriodicTimer` / `SerialLineBuffer`）を
対象とする。`main.cpp` の「配線」だった部分で、宛先判定を間違えると
「他のアクチュエータ宛のフレームで自分が動く」「ブロードキャスト緊急停止が届かない」の
どちらも起こりうる。

- Standard Frame 以外と予約コマンド種別を捨てること
- `0xFF` は `E_STOP` のときだけ受理し、全チャンネルへ配ること
- チャンネル数が `channelMask` のビット数を超えたら、切り詰めずにフレームごと捨てること
  （切り詰めると「ブロードキャスト E_STOP が届かないチャンネル」を持ったまま動く）
- デバイス ID `0x00`（未設定）に「自分宛」が存在しないこと
- DIP オフセットが `0x00` / `0xFF` に回り込んだチャンネルを未設定に倒すこと
- DIP のビット順と負論理
- `millis()` 折り返しで周期判定が止まらないこと
- 空行を指令として通さないこと（数値に化けて duty 0 / 角度 0 の指令になる）

`test_servo/` は `ServoMotion`（角度補間・可動範囲クランプ・到達推定）と
`ServoChannel`（安全機構と補間の結線）を対象とする。

- 角度 → パルス幅の線形変換（0deg → `minUs`、`angleRangeDeg` → `maxUs`、範囲外のクランプ）
  と、180 度サーボ向けのスケール変換をしていないこと
- `angle_min` / `angle_max` でのクランプ、NaN 目標の拒否（仕様書 §7.2）
- スルーレート制限と、到達までの所要時間が `距離 / slew_rate` と一致すること（§7.3）
- 静止中は `currentSlewDegPerSec()` が 0 になること（§7.4 の速度欄の元）
- `holdHere()` が目標を現在角で凍結し、それ以上動かないこと（§7.5）
- `setLimits()` が可動範囲を狭めたときに現在の目標をクランプし直し、角度を飛ばさないこと（§7.6）
- `SET_PARAM` がサーボ向け ID（`0x04`/`0x05`/`0x07`/`0x10`/`0x11`/`0x12`）だけを通すこと
- `millis()` の 49.7 日折り返しで補間が巻き戻らないこと
- 緊急停止ラッチ中に 20Hz の再送を受けても 1 度も動かないこと（§7.5）
- ラッチ中に指令された角度が解除後に実行されないこと（同じ受信バッチでの解除を含む）
- ウォッチドッグ満了の瞬間に 1 ティック分も進まないこと（凍結が補間より先であること）
- 電源投入後の最初の `SET_TARGET` は受理されること（養う → 受理判定の順序）

`main.cpp` はペリフェラル依存のため native テストの対象外
（`common.ini` の `test_ignore = *` は実機 env 側の設定で、共有テストを実機 env で
1 つも走らせないためのもの。個別のテスト名ではなくワイルドカードにしてあるのは、
テストを足すたびにここへ名前を書き足す必要を無くすため）。
