# firmware/ — 自作モータドライバのファームウェア

自作モタドラ（Arduino UNO R4 / Renesas RA4M1）の PlatformIO プロジェクト群。
PC 側 `lib/drivers/generic.py` と CAN で対向する。

**プロトコルの単一情報源は `docs/motor_driver_can_protocol.md`。**
フレーム定義・安全機構の仕様はすべてそこにあり、ファームと PC 側の片方だけを
変更してはならない。

## 構成

```
firmware/
  lib/
    MotorCan/          全ファーム共通。Arduino 非依存の純 C++
      src/MotorCanProtocol.{h,cpp}   CAN ID / フレームの符号化・復号
      src/MotorSafety.{h,cpp}        緊急停止ラッチ + コマンドウォッチドッグ
      src/MotorPid.{h,cpp}           position / velocity 用 PID（DC 用のみ使用）
      src/ServoMotion.{h,cpp}        角度補間・可動範囲クランプ・到達推定（サーボ用のみ使用）
  dc_motor/            DC モータ用モタドラのファーム
    include/config.h   ピン配置と機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・制御ループ・CAN 送受信
    test/test_protocol/  native 環境の Unity ユニットテスト
  servo/               サーボ用モタドラのファーム（1 枚で複数チャンネル）
    include/config.h   ピン配置・チャンネル表・機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・補間ループ・CAN 送受信
    test/test_servo/     native 環境の Unity ユニットテスト
```

`MotorCan` が `Arduino.h` を include しないのは意図的で、PC 上の native 環境で
そのままコンパイルしてテストできるようにするため。`dc_motor/` と `servo/` は
`lib_extra_dirs = ../lib` で同じ `MotorCan` を共有するので、**`MotorCan` を触ったら
両方の native テストを回すこと。**

## コマンド

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
# ユニットテスト（実機不要）
pio test -e native -d firmware/dc_motor   # プロトコル層・安全機構・PID
pio test -e native -d firmware/servo      # 角度補間・可動範囲クランプ・到達推定

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

- `0 5.0` のように「`<チャンネル番号> <角度[deg]>`」を送るとそのチャンネルへ角度指令
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
基板・データシート・実測と突き合わせること。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kDisActiveHigh` | `true` | ゲートドライバ `DIS` の論理。HIGH でアサート（出力禁止）と仮定 | **反転していると緊急停止でモータが全力で回る。最優先で確認** |
| `HAS_ENCODER` | `1` | エンコーダの有無。無ければ 0（position/velocity 制御が使えなくなる） | 無いのに 1 だと位置・速度が 0 のまま PID が振り切れる |
| `kEncoderPulsesPerMotorRev` | `500` | エンコーダ 1 相あたりのパルス数（モータ軸） | 位置・速度の換算が丸ごとずれる |
| `kGearRatio` | `30.0` | 減速比。`FEEDBACK` は出力軸換算で送る（PC 側は換算を知らない） | 同上 |
| `kEncoderDirectionSign` | `1.0` | 正の duty で速度が正になるか。A/B の配線で反転する | 位置・速度制御が正帰還になって暴走する |
| `kCurrentSenseZeroCount` | `512` | 無電流時の ADC カウント（10bit, 0–1023） | 過電流フラグが誤検出／未検出になる |
| `kCurrentSenseMaPerCount` | `10.0` | ADC カウント → mA の換算係数 | 同上 |
| `kDefaultOvercurrentThresholdMa` | `5000` | モータとドライバ IC の連続定格 | 同上 |
| `kDefaultKp` / `kDefaultKi` / `kDefaultKd` | `0.01` / `0` / `0` | PID ゲイン。position と velocity で 1 組を共有する（仕様書 §3.4） | 発振・目標に届かない |
| `HAS_RGB_LED` | `0` | シリアル RGB LED の搭載。有効化するには `lib_deps` にライブラリ追加が必要 | — |

温度センサはこの基板に無く、`FEEDBACK` の温度は常に 0 を送る
（仕様書 §3.2 / §7 の既知の制限）。PC 側の温度警告は発火しない。

### servo

`firmware/servo/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。

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
| 緊急停止・ウォッチドッグ時 | 出力停止（コースト） | **現在角を保持** | 仕様書 §7.5。サーボは脱力すると壁が倒れワークを落とす |
| 受け付けるモード | position / velocity / duty | **position のみ**（他は無視） | 仕様書 §7.2 |
| 起動時 | duty モード / 目標 0 / 出力停止 | 各チャンネル `initialAngleDeg` / 緊急停止ラッチ解除済み | 仕様書 §5.4 |

`FEEDBACK` はチャンネルごとに送信タイミングを周期内でずらしてある。
全チャンネルが同時に送るとフレームのバーストになり、他バスの周期送信と重なったときに
調停待ちが伸びて送信間隔が波打つため。

**PC 側の目標値再送が未実装のうちは、コマンドウォッチドッグでコンベアが 500ms で
止まる**（仕様書 §5.1 の注記と §8）。それまでは `SET_PARAM` の
`command_timeout_ms`（ID `0x04`）を大きくして運用すること。サーボ側は満了しても
現在角を保持するので機構が落ちることはないが、そこから先は動かせなくなる。

## テストの方針

`dc_motor/test/test_protocol/` は `MotorCan`（プロトコル層・安全機構・PID）のみを対象とし、
Arduino に依存しない。実機が無くても以下を検出できる。

- CAN ID の組み立て／解析（予約値 `0b100`/`0b101`/`0b110` を無効として弾くこと）
- float32 リトルエンディアンの往復と既知バイト列の一致
- `E_STOP` の解除がマジックバイト `0x5A` `0xA5` 揃いのときだけ通ること
- `FEEDBACK` の位置・速度・電流が int16 で折り返さず飽和すること
- ウォッチドッグの満了・復帰・`millis()` 折り返し、ラッチ中も養えること
- duty クランプ（`max_duty` 超過・負値・0・NaN）
- PID のリセットとワインドアップ制限

`servo/test/test_servo/` は `ServoMotion`（角度補間・可動範囲クランプ・到達推定）を対象とする。

- 角度 → パルス幅の線形変換（0deg → `minUs`、`angleRangeDeg` → `maxUs`、範囲外のクランプ）
  と、180 度サーボ向けのスケール変換をしていないこと
- `angle_min` / `angle_max` でのクランプ、NaN 目標の拒否（仕様書 §7.2）
- スルーレート制限と、到達までの所要時間が `距離 / slew_rate` と一致すること（§7.3）
- 静止中は `currentSlewDegPerSec()` が 0 になること（§7.4 の速度欄の元）
- `holdHere()` が目標を現在角で凍結し、それ以上動かないこと（§7.5）
- `setLimits()` が可動範囲を狭めたときに現在の目標をクランプし直し、角度を飛ばさないこと（§7.6）
- `SET_PARAM` がサーボ向け ID（`0x04`/`0x05`/`0x07`/`0x10`/`0x11`/`0x12`）だけを通すこと
- `millis()` の 49.7 日折り返しで補間が巻き戻らないこと

`main.cpp` はペリフェラル依存のため native テストの対象外
（`platformio.ini` の `test_ignore` は実機 env 側の設定）。
**`MotorCan` は両ファームで共有しているので、触ったら両方の native テストを回すこと。**
