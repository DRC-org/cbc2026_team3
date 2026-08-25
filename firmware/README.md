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
      src/MotorPid.{h,cpp}           position / velocity 用 PID
  dc_motor/            DC モータ用モタドラのファーム
    include/config.h   ピン配置と機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・制御ループ・CAN 送受信
    test/test_protocol/  native 環境の Unity ユニットテスト
```

`MotorCan` が `Arduino.h` を include しないのは意図的で、PC 上の native 環境で
そのままコンパイルしてテストできるようにするため。サーボ用モタドラのファームを
追加するときは `firmware/servo/` を切り、同じ `lib_extra_dirs = ../lib` で共有する。

## コマンド

いずれも `firmware/dc_motor` をプロジェクトディレクトリとして実行する
（`-d` を付ければリポジトリ直下からでも実行できる）。

```bash
# プロトコル層のユニットテスト（実機不要）
pio test -e native -d firmware/dc_motor

# ビルド
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e uno_r4_wifi   -d firmware/dc_motor

# 書き込み（基板を USB で接続してから）
pio run -e uno_r4_minima -d firmware/dc_motor -t upload
pio run -e uno_r4_minima -d firmware/dc_motor -t upload --upload-port /dev/ttyACM0

# シリアルモニタ（115200 baud）
pio device monitor -e uno_r4_minima -d firmware/dc_motor

# クリーン
pio run -e uno_r4_minima -d firmware/dc_motor -t clean
```

基板が Minima か WiFi かは未確定のため両方の env を用意してある。既定は
`uno_r4_minima`（`platformio.ini` の `default_envs`）。基板が確定したら
`default_envs` をそれに合わせること。

初回ビルドではツールチェーン（`toolchain-gccarmnoneeabi`）と Arduino コアが
ダウンロードされるためネットワークが必要。以降はオフラインでビルドできる。

## デバッグ用シリアル

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

## デバイス ID

基板上の DIP スイッチ 4bit（`INPUT_PULLUP` の負論理、LOW = 1）で設定する。
`SW0`=bit0, `SW1`=bit1, `SW2`=bit2, `SW3`=bit3。

`0x00`（全 OFF）は「設定忘れ」とみなし、**駆動を拒否して LED を速く点滅させる**
（仕様書 §2.2）。`FEEDBACK` の bit5 も立つ。設定ミスで意図しないアクチュエータが
動くより、動かない方が安全なため。

デバイス ID の割り当ては仕様書 §2.2 の表と `config/*.yaml` を参照。

## config.h の要確認項目（通電前に必ず）

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

## 安全に関する既定値

| 項目 | 既定 | 根拠 |
|---|---|---|
| `max_duty` | `0.30` | 仕様書 §5.3。サンプルコードは未初期化で常に 0 だったので明示的に持つ |
| `command_timeout_ms` | `500` | 仕様書 §5.1。満了で出力停止し `FEEDBACK` bit4 を立てる。ラッチしない |
| `feedback_interval_ms` | `10` | 100Hz。緊急停止中・ウォッチドッグ作動中も送り続ける |
| 起動時 | duty モード / 目標 0 / 出力停止 / 緊急停止ラッチ解除済み | 仕様書 §5.4 |

**PC 側の目標値再送が未実装のうちは、コマンドウォッチドッグでコンベアが 500ms で
止まる**（仕様書 §5.1 の注記と §7）。それまでは `SET_PARAM` の
`command_timeout_ms`（ID `0x04`）を大きくして運用すること。

## テストの方針

`test/test_protocol/` は `MotorCan`（プロトコル層・安全機構・PID）のみを対象とし、
Arduino に依存しない。実機が無くても以下を検出できる。

- CAN ID の組み立て／解析（予約値 `0b100`/`0b101`/`0b110` を無効として弾くこと）
- float32 リトルエンディアンの往復と既知バイト列の一致
- `E_STOP` の解除がマジックバイト `0x5A` `0xA5` 揃いのときだけ通ること
- `FEEDBACK` の位置・速度・電流が int16 で折り返さず飽和すること
- ウォッチドッグの満了・復帰・`millis()` 折り返し、ラッチ中も養えること
- duty クランプ（`max_duty` 超過・負値・0・NaN）
- PID のリセットとワインドアップ制限

`main.cpp` はペリフェラル依存のため native テストの対象外
（`platformio.ini` の `test_ignore = test_protocol` は実機 env 側の設定）。
