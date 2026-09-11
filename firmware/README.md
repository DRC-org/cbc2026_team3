# firmware/ — 自作モータドライバのファームウェア

自作モタドラのファームウェア（PlatformIO 2 プロジェクト + CMake 1 プロジェクト）。
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
      src/MotorLoopTimer.h           PeriodicTimer（millis() 折り返しに耐える周期判定）
      src/MotorSafety.{h,cpp}        緊急停止ラッチ + コマンドウォッチドッグ + 物理停止入力
      src/SerialLineBuffer.{h,cpp}   デバッグシリアルの行組み立て（行の解釈は各 main.cpp）
      src/DcChannel.{h,cpp}          DC 1ch 分の結線（安全機構 + duty 目標。DC 用のみ使用）
      src/ServoMotion.{h,cpp}        角度補間・可動範囲クランプ・到達推定（サーボ用のみ使用）
      src/ServoChannel.{h,cpp}       上 2 つの結線（出力禁止中は指令を受け付けず先に凍結する）
      src/SolenoidChannel.{h,cpp}    電磁弁 1ch 分の結線（安全機構 + ON/OFF 目標。電磁弁用のみ使用）
  test/                native 環境の Unity テスト。PlatformIO の 2 つが test_dir = ../test で共有
    test_protocol/     プロトコル層・安全機構・物理停止・duty 分解・DcChannel・状態フラグの組み立て
    test_board/        宛先判定・デバイス ID 解決・周期タイマ・シリアル行
    test_servo/        角度補間・可動範囲クランプ・到達推定・安全機構との結線
    test_solenoid/     電磁弁のデバイス ID・on_off の復号・SolenoidChannel の安全機構
  dc_motor/            DC モータ用モタドラのファーム（1 枚で 3 チャンネル）
    platformio.ini     固有行のみ（default_envs / extra_configs / test_dir / lib_deps）
    include/config.h   ピン配置・チャンネル表・機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・出力反映・CAN 送受信
  servo/               サーボ用モタドラのファーム（1 枚で 5 スロット。**基板 3 枚 / MCU 2 種**）
    platformio.ini     固有行のみ。**実機 env が 2 つ**（nano = #0/#1 / uno_r4_minima = #2）
    include/config.h   ピン配置・スロット表・機体依存定数（要確認項目はここ）。**1 プロジェクト 1 ファイル**
    src/main.cpp       ペリフェラル初期化・補間ループ・CAN 送受信（**MCU に依らず 1 コピー**）
    src/can_backend.h  CAN の薄いインタフェース（`begin` / `send` / `poll`）。MCU 差はここで吸う
    src/can_mcp2515.cpp  Nano（#0/#1）用の実装。SPI 外付け + 受信フィルタ
    src/can_r4.cpp     UNO R4（#2）用の実装。内蔵ペリフェラル（`Arduino_CAN`）
  solenoid/            電磁弁用モタドラのファーム（**STM32F303K8** / 1 枚で 6ch）
    CMakeLists.txt     **PlatformIO ではなく CMake**。MotorCan を target_sources で共有
    CMakePresets.json  Debug / Release（generator は Ninja）
    solenoid.ioc       CubeMX のプロジェクト定義。ピン割当・CAN ビットタイミングの単一情報源
    cmake/             ツールチェーン定義と HAL のソース一覧（CubeMX 生成だがコミットする）
    scripts/fetch_hal.sh  HAL と CMSIS を ST の公式リポジトリから取得（CubeMX の代わり）
    Core/              CubeMX 生成（main.c は setup() / loop() を呼ぶだけ）
    include/config.h   ピン配置・チャンネル表・機体依存定数（要確認項目はここ）
    src/app.cpp        ペリフェラル初期化・出力反映・bxCAN 送受信
```

**`firmware/solenoid/Drivers/` は `.gitignore` されている**（HAL と CMSIS で数十 MB あり、
ST の公式リポジトリから取り直せるため）。clone 直後は `scripts/fetch_hal.sh` を 1 回
実行すればビルドできる。**`cmake/` はコミットしてある** —— 詳細は下の「コマンド」を参照。

**MCU は 3 種類ある。しかも「サーボ基板 = Nano」ではない** —— サーボ基板は 3 枚あり、
**#2 だけ UNO R4 Minima（内蔵 CAN）**である。

| | DC 用 | サーボ用 #0/#1 | サーボ用 **#2** | 電磁弁用 |
|---|---|---|---|---|
| MCU | Arduino UNO R4 Minima（RA4M1 / 32bit / 3.3V） | **Arduino Nano（ATmega328P / 8bit / 5V）** | Arduino UNO R4 Minima（RA4M1 / 32bit / 3.3V） | **STM32F303K8T6（Cortex-M4F / 32bit / 3.3V）** |
| CAN | R4 内蔵ペリフェラル（`Arduino_CAN`、D4/D5 固定） | **MCP2515 を SPI で外付け**（`mcp_can`） | **R4 内蔵ペリフェラル**（`Arduino_CAN`、D4/D5 固定。MCP2515 は載っていない） | **STM32 内蔵 bxCAN**（PA11/PA12 固定） |
| 出力 | `PwmOut`（R4 専用）+ 方向ピン | **`Servo` ライブラリ**（`writeMicroseconds`） | `Servo` ライブラリ | **GPIO の ON/OFF だけ**（PWM も方向ピンも無い） |
| サーボのピン | ― | SV0–SV4 = **D4–D8** | SV0–SV4 = **D9 / D11 / D10 / D6 / D3** | ― |
| RGB LED | D6 | **D9** | **D8**（D9 はサーボ SV0） |（無し） |
| Flash / RAM | 256KB / 32KB | **32KB / 2KB** | 256KB / 32KB | 64KB / 12KB |
| ビルド | PlatformIO（env `uno_r4_minima`） | PlatformIO（env `nano`） | PlatformIO（env `uno_r4_minima`） | **STM32CubeMX + CMake** |

**サーボ用の 2 つの env は同じ `firmware/servo/` の中にある**（1 プロジェクト 2 env）。
別プロジェクト（`firmware/servo_r4/`）にしなかったのは、**`config.h` が 2 つに分かれると
デバイス ID `0x40` 台が 2 つの `kFirmwareVersion` に対応してしまう**ため。
`tests/test_firmware_version_sync.py` は「can_id の上位 2bit → プロジェクト 1 つ」で
同梱の全 yaml と突き合わせるので、分けた瞬間に**焼き忘れ検出の検査そのものが
片方しか見ない嘘になる**。

**「同じファームを全基板へ焼く」は「MCU ごとに 1 種類のバイナリ」へ緩めてある。**
Nano 用バイナリを R4 へ物理的に焼けない（platform も board も違う）以上、
焼き間違いの事故が起きる余地は無い。CAN 上の振る舞いは 2 つのバイナリで同一なので、
**`kFirmwareVersion` も同じ値を名乗る**（R4 バイナリの新設では版番号を上げていない。
理由は仕様書 §3.4）。

`MotorCan` が `Arduino.h` も `stm32f3xx_hal.h` も include しないのは意図的で、PC 上の
native 環境でそのままコンパイルしてテストできるようにするため。**MCU もビルド系も
違ってよく、3 枚が同じソースを共有する。** `dc_motor/` と `servo/` は
`lib_extra_dirs = ../lib`、`solenoid/` は CMake の `target_sources` で同じ `MotorCan` を指す。

テストも `firmware/test/` を共有するので、**`pio test -e native` はどちらの
PlatformIO プロジェクトから回しても同じ全ケース（`test_solenoid` を含む）が走る。**
電磁弁基板のロジック層も、実機ビルドが CMake であることとは無関係にここでテストされる。
**native テストは MCU に依らないので、サーボの env が 2 つに増えてもどちらか一方で足りる**
（テスト対象の `MotorCan` は Arduino 非依存）。
一方**実機ビルド（`pio run` / `cmake --build`）は 4 つとも確認すること** ——
dc_motor / servo(`nano`) / servo(`uno_r4_minima`) / solenoid。サーボの 2 env は
`main.cpp` を共有しているので、**片方だけ通しても壊れているのは常にもう片方**である
（ピンの `static_assert` も CAN バックエンドも env ごとに別物）。

`common.ini` が持つのは本当に共通の部分（`lib_extra_dirs` / `test_framework` / native env と、
実機ビルドの共通フラグ）だけ。**`platform` / `board` / `lib_deps` は MCU が違うので
各プロジェクトの `platformio.ini` が持つ。**

## コマンド

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
# ユニットテスト（実機不要）。どちらも firmware/test/ の全ケースが走る
pio test -e native -d firmware/dc_motor
pio test -e native -d firmware/servo

# ビルド（env が基板ごとに違う。**サーボは 2 env とも必要**）
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e nano          -d firmware/servo      # サーボ基板 #0 / #1（Arduino Nano）
pio run -e uno_r4_minima -d firmware/servo      # サーボ基板 #2（UNO R4 Minima）
pio run                  -d firmware/servo      # 上の 2 つを両方（default_envs が両方）

# 書き込み（サーボ基板 #0 / #1 = Arduino Nano）
pio run -e nano -d firmware/servo -t upload
pio device monitor -e nano -d firmware/servo

# 書き込み（サーボ基板 #2 = UNO R4 Minima）
pio run -e uno_r4_minima -d firmware/servo -t upload
pio run -e uno_r4_minima -d firmware/servo -t upload --upload-port /dev/ttyACM0
pio device monitor -e uno_r4_minima -d firmware/servo

# 書き込み（基板を USB で接続してから）
pio run -e uno_r4_minima -d firmware/dc_motor -t upload
pio run -e uno_r4_minima -d firmware/dc_motor -t upload --upload-port /dev/ttyACM0

# シリアルモニタ（115200 baud）
pio device monitor -e uno_r4_minima -d firmware/dc_motor

# クリーン
pio run -e uno_r4_minima -d firmware/dc_motor -t clean
```

**書き込みの作法も MCU で違う。** Nano（サーボ #0/#1）だけがブートローダの
ボーレート問題を持つ —— `platformio.ini` の `board` は `nanoatmega328`（**古い
ブートローダ / 57600 baud**）で、新しいブートローダの個体には `nanoatmega328new`
（115200）が要る。違うと avrdude が同期できず、しかも**症状が紛らわしい**（同期待ちの
あいだにスケッチが起動してシリアルへ喋り出し、それを応答として読むので
`not in sync: resp=0x45` のように意味のある ASCII が並ぶ。配線やポートの誤りに見えるが
ボーレート違いでしかない）。切り分けは `-n`（書き込みなし）で署名だけ読むのが速い:
`avrdude -c arduino -p atmega328p -P /dev/ttyUSB0 -b 57600 -n`。
**ビルド成果物は同一**で、違うのは書き込み時の baud だけ。
一方 **UNO R4（DC 基板とサーボ #2）は USB/DFU で書き込むのでこの制約が無い** ——
ポートは `/dev/ttyACM*`（Nano は `/dev/ttyUSB*`）で、`board` を選び直す判断も要らない。
**同じ「サーボ基板」でも書き込み手順が違うので、繋いだ基板がどちらかを先に確認すること。**

**電磁弁基板（STM32F303K8）は PlatformIO ではなく CMake でビルドする。**

必要なツール:

| ツール | 版 | 備考 |
|---|---|---|
| `arm-none-eabi-gcc` | **11 以降** | CubeMX のリンカスクリプトが使う `READONLY` キーワードが GCC11 以降にしか無い。古い版では**コンパイルは全部通ってリンクだけが落ちる**（`STM32F303XX_FLASH.ld:106: non constant or forward reference address expression for section .ARM.extab`）。PlatformIO が renesas-ra 用に持っている gcc 7.2.1 では通らない |
| `cmake` | 3.22 以降 | `uv tool install cmake` で入る |
| `ninja` | — | `CMakePresets.json` の generator。`uv tool install ninja` で入る |

ツールチェーンの入れ方は 2 通り。

**Ubuntu 24.04 以降なら apt が最短**（26.04 で 14.2 が入る。sudo が要る代わりに
`PATH` を通す必要が無く、`/usr/bin/arm-none-eabi-gcc` として見える）:

```bash
sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi \
                 libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib
```

**`libstdc++-arm-none-eabi-newlib` を省かないこと。** `src/app.cpp` は C++ なので、
これが無いとコンパイルは全部通ってリンクだけが落ちる（GCC の版が古いときと
同じ壊れ方をするので、版を疑って時間を溶かす）。

古い Ubuntu では apt の版が 10 以下のことがある（`apt-cache policy gcc-arm-none-eabi`
で確認）。その場合は xPack 版を使う（sudo 不要、tar を展開するだけ）:
<https://github.com/xpack-dev-tools/arm-none-eabi-gcc-xpack/releases>

**`Drivers/`（HAL と CMSIS）は生成物なのでコミットしていない。** CubeMX が無くても
`scripts/fetch_hal.sh` で取得できる —— ST の公式リポジトリから、STM32CubeF3 が
サブモジュールとして指している検証済みのコミットに固定して取る。

```bash
# 1. HAL / CMSIS を取得（初回のみ）
firmware/solenoid/scripts/fetch_hal.sh

# 2. ビルド（apt で入れたなら PATH の指定は要らない）
cmake --preset Debug -S firmware/solenoid
cmake --build firmware/solenoid/build/Debug

#    xPack 版を展開して使う場合はその bin を PATH の先頭に置く:
# PATH="$HOME/.local/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin:$PATH" \
#   cmake --preset Debug -S firmware/solenoid
#   → Debug: FLASH 35.9% (23,496 B / 64KB) / RAM 19.6% (2,408 B / 12KB)
#   → Release: FLASH 21.3% (13,972 B)
#   （数値は GCC 14.2 / apt 版での実測。コンパイラの版で数 % 前後する）

# 3. 書き込み（ST-Link。STM32CubeProgrammer / OpenOCD いずれでも）
STM32_Programmer_CLI -c port=SWD -w firmware/solenoid/build/Debug/solenoid.elf -rst

# 4. デバッグシリアル（USART1 / PA9-PA10 / 115200 baud）
#    USB-UART 変換を繋いで任意のシリアルモニタで開く
```

**CubeMX が要るのは `.ioc` を変更したときだけ。** ピン割当・クロック・CAN ビット
タイミングを変えたら CubeMX で GENERATE CODE し、`Core/` と `cmake/` の差分を確認して
コミットする。`Drivers/` の中身は `.ioc` では変わらないので `fetch_hal.sh` の再実行は要らない。

**`cmake/` は CubeMX の生成物だがコミットしてある**（`.gitignore` から意図的に外した）。
中身はツールチェーン定義と HAL のソース一覧だけで数 KB しかなく、無視すると
**CubeMX を持っている人以外ビルドできない**リポジトリになる。試合前に基板へ焼ける人が
1 人に絞られるのは避けたい。

**`Core/Src/main.c` の USER CODE 領域には `setup()` / `loop()` の呼び出ししか置かないこと。**
ロジックを書くと、次に CubeMX で再生成した人が黙って壊す（USER CODE 以外は上書きされる）。
`src/app.cpp` に書けば再生成の影響を受けない。

**`solenoid.ioc` はピン割当と CAN ビットタイミングの単一情報源。** DIP の内部プルアップも
ここが持つ（既定の `GPIO_NOPULL` のままだと、外部プルアップの無い基板で DIP の読みが
不定になり、電源投入のたびに違うデバイス ID を名乗る）。`Core/` を手で直すのではなく
`.ioc` を直して再生成すること。

**UNO R4 Minima の CAN ペリフェラルは `D4`(TX)/`D5`(RX) に固定されている**（DC 基板と
**サーボ基板 #2** が該当）。このピンを他用途へ割り当ててはならない。割り当てると CAN が
上がらず**PC から止められない基板**ができあがる。**サーボ基板 #0/#1（Nano）は MCP2515 を
SPI で外付けしているので D4/D5 はサーボ出力に使えるが、代わりに `D11`/`D12`/`D13` を
SPI が占有する**（`D13` は SCK なのでステータス LED に使えず、RGB LED がその役目を担う）。

**この 2 つは互いに逆なので、同じ「サーボ基板」どうしでもピンを類推してはならない。**
#0/#1 で空いている D4/D5 は #2 では CAN が取り、#0/#1 で SPI が取っている D11 は
#2 ではサーボ（SV1）である。**だからスロット表は基板番号を明示した並び
（`kServoBoards[]`）に持ち、各バイナリは自分が担当する基板の要素だけを持つ** ——
1 つの表に 3 枚ぶんを並べると、R4 ビルドの `static_assert` が Nano 側の D4/D5 を
CAN 衝突と誤検出し、Nano ビルドは R4 側の D9 を RGB LED との衝突と誤検出する
（どちらも実在しない衝突なので、検査を緩めるしか通す方法が無くなる）。

各 `main.cpp` の `static_assert` が、`config.h` の全ピンについて **①CAN/SPI との衝突
②役割どうしの重複 ③デバイス ID の重複と連続ブロック性 ④センサの報告ビット**を
ビルド時に検出する。

初回ビルドではツールチェーン（`toolchain-gccarmnoneeabi`）と Arduino コアが
ダウンロードされるためネットワークが必要。以降はオフラインでビルドできる。

## デバッグ用シリアル（dc_motor）

USB CDC の `Serial`（115200 baud）から duty を直接入力できる。

- `0 0.3` のように「`<チャンネル番号> <duty>`」を送るとそのチャンネルが回る。
  **チャンネル番号と duty は空白で区切る。** 区切りが無い行は捨てる（番号を読み違えると
  別のモータが回るので、曖昧な入力は指令にしない）
- `s` を送ると全チャンネル停止し、シリアル操作モードを抜ける
- CAN から `SET_TARGET` が来たらシリアル操作モードは自動的に解除される
- **緊急停止ラッチ中はシリアルからも駆動できない**（`max_duty` のクランプも同じく効く）

シリアル操作中はコマンドウォッチドッグを養い続ける（1 回だけ養う実装だと
`command_timeout_ms` 後に必ず止まってデバッグにならないため）。
**養うのは打ったチャンネルだけ**で、**最後の入力から `kSerialOverrideHoldMs`
（2000ms = `kMaxCommandTimeoutMs`）で自動解除**される（規則は
`lib/MotorCan/src/SerialOverride.h`。3 枚とも同じものを使う）。
不要なら `config.h` の `ENABLE_SERIAL_DEBUG` を 0 にする。

DIP スイッチ（`SW0`/`SW1`）は D1/D0 = ハードウェア UART と同じピンなので、
**`Serial1` を開いてはならない**。開くと DIP が読めずデバイス ID が化ける。

## デバッグ用シリアル（servo）

USB CDC の `Serial`（115200 baud）から角度を直接入力できる。

- `0 5.0` のように「`<スロット番号> <角度[deg]>`」を送るとそのスロットへ角度指令。
  **番号と角度は空白で区切る。** 区切りが無い行、サーボ以外のスロットを指した行は捨てる
  （番号を読み違えると別のサーボが動くので、曖昧な入力は指令にしない）
- `s` を送ると全スロットを現在角で凍結し、シリアル操作モードを抜ける
- CAN から `SET_TARGET` が来たらシリアル操作モードは自動的に解除される
- **緊急停止ラッチ中はシリアルからも駆動できない**（角度も `angle_min`/`angle_max` でクランプされる）

シリアル操作中の養い方は dc_motor と同じ（打ったスロットだけ / 最後の入力から
2000ms で自動解除。`lib/MotorCan/src/SerialOverride.h`）。

サーボ基板の DIP は 3 枚とも A0〜A3 で UART とは重ならないため、`Serial` の使用に
制約はない。ただし **#0/#1（Nano）は Flash 32KB / SRAM 2KB しかない**ので、
容量が足りなくなったら
`config.h` の `ENABLE_SERIAL_DEBUG` を 0 にして落とす。

### 試合用ビルドでは全基板とも `ENABLE_SERIAL_DEBUG` を 0 にする

シリアルの上書きは**コマンドウォッチドッグ（仕様書 §5.1 の「最後の砦」）を
意図的に外す経路**である。範囲はそのチャンネルだけ・期限は 2000ms に絞ってあるが、
0 にすればその経路自体が存在しなくなる。**通電前チェックの項目**として扱うこと。

| 基板 | `config.h` | 試合時 | ベンチ／デバッグ時 |
|---|---|---|---|
| dc_motor | `firmware/dc_motor/include/config.h` | `#define ENABLE_SERIAL_DEBUG 0` | `1` |
| servo | `firmware/servo/include/config.h` | `#define ENABLE_SERIAL_DEBUG 0` | `1` |
| solenoid | `firmware/solenoid/include/config.h` | `#define ENABLE_SERIAL_DEBUG 0` | `1` |

**`config.h` は 3 つだが、焼く相手は 5 枚**（DC 1 + サーボ 3 + 電磁弁 1）。サーボの
`config.h` は 1 つでも **`nano` / `uno_r4_minima` の 2 回焼く**必要がある。

**焼き直したら `kFirmwareVersion` は変えない**（プロトコルもピン配置も変わっていない）。
どのビルドが載っているかは `INFO` からは分からないので、**試合前に全基板を
0 で焼き直したことを口頭で確認する**のが唯一の担保になる。

## デバイス ID

### デバイス ID は固定ビット分割

```
Bit7..6 : 基板種別 (1=サーボ / 2=DC / 3=電磁弁。0 は予約)
Bit5..3 : 基板番号 (DIP そのもの。0-7)
Bit2..0 : スロット番号 (0-7)
```

**帯も刻み幅も連続ブロック性も要らない。** DIP は基板番号そのもので、スロットの
添字がそのまま ID の下位 3bit になる。ID を見ればどの基板のどのスロットかが
直接読めるので、`candump` を眺めているときに対応表を引かなくてよい。

| 基板 | 基板番号 0 の ID | モータ |
|---|---|---|
| **DC** ch0 / ch1 / ch2 | `0x80` / `0x81` / `0x82` | `conveyor` / `pump_vac` / **未使用（ID は予約）** |
| **サーボ** SV0 – SV4 | `0x40` – `0x44` | `gripper` / `wall_f` / `wall_r` / `rotate_origin_sensor` / `y_axis_r_origin_sensor` |
| **電磁弁** ch0 – ch5 | `0xC0` – `0xC5` | `valve_1` 〜 `valve_6` |

2 枚目は DIP=1 で DC が `0x88`〜、サーボが `0x48`〜、電磁弁が `0xC8`〜。
**サーボ基板だけは実際に 3 枚使い、15 スロットとも埋まっている** —— 基板 #1
（`0x48`〜`0x4C`）は**5 スロットともタッチセンサ**（SV0 だけメインハンドの `y_axis` 左
スイッチで、残りはサブハンドの可動端スイッチ）、基板 #2（`0x50`〜`0x54`、UNO R4）は
**5 スロットともサーボ**（サブハンドの回転 2 + ピッチ 2 + オフセット 1）。
**空きが無いので、足すには基板を 1 枚増やす**（DIP=3 → `0x58`〜`0x5C`）。

**範囲外は未設定（駆動拒否）へ倒す。** DIP は 4bit だが基板番号は 3bit なので、8 以上へ
回した基板は全スロットが未設定になる。**`0xFF`（`E_STOP` ブロードキャストの予約）に着地する
1 個も未設定へ倒す** —— 電磁弁基板の「基板番号 7 × スロット 7」だけがそこへ落ちる（潰れるのは
512 個中 1 個）。

**未設定のチャンネルは `FEEDBACK` も `INFO` も 1 通も送らない。切り分けは LED だけで行う。**
PC から見た症状は「その基板の全チャンネルが STALE」で配線不良と区別が付かないので、
**まず基板の LED を見ること**（赤の速い点滅 = CAN 不通 / CAN 送信の連続失敗 /
デバイス ID 未設定）。理由は
[`../docs/invariants.md`](../docs/invariants.md) の「未設定のチャンネルは `FEEDBACK` も
`INFO` も 1 通も送らない」。

### servo — スロット表

サーボ基板は **5 本の信号線（SV0〜SV4）を持ち、どれもサーボ出力にもデジタル入力にもなる**
（仕様書 §7.1）。何を繋ぐかは配線で決まるので、ファームは `config.h` に**役割**
（`Servo` / `TouchSensor` / `Unused`）を持つ。
**組み合わせは自由で、それぞれ何個でもよい** —— 「サーボ 2 + センサ 2 + 空き 1」も
「センサだけ」も成立する。`Unused` 以外のスロットはすべて CAN デバイスとして
`FEEDBACK` を送る。

**スロット設定は基板番号を明示して持つ並び `kServoBoards[]`（`ServoBoardConfig`）が持つ。**
1 つのバイナリを複数の基板へ焼くので、表が 1 つしか無いと基板 #0 の SV3 をスイッチへ
変えた瞬間に基板 #1 の SV3 も道連れになる。要素を選ぶのは `setup()` の DIP 読み取り。

**「行の添字 = 基板番号」ではなく基板番号を値として持つのは、バイナリが MCU ごとに
分かれたため。** Nano 用バイナリは #0/#1 の要素だけ、R4 用バイナリは #2 の要素だけを
持つ。担当外の基板番号は表に**載っていない**ので、後述の「表に無い基板番号は全スロット
`Unused`」へそのまま乗る（安全側の倒れ方は変えていない）。

**サーボの型（270° / 180°）もスロットごと・基板ごとに選べる。** 型は `pulse`
（`kServoPulse270` / `kServoPulse180`）が表し、`SlotRole` は「駆動するか、読むか、
使わないか」だけを表す軸に保つ。混在させるときは `minUs` / `maxUs` / `angleRangeDeg` を
**3 値セット**でデータシートに合わせること（1 つだけ直してもずれ方が変わるだけで、
CAN 越しには指令どおり動いたようにしか見えない）。

| 基板 | スロット | ピン | 役割 | 型 | デバイス ID | モータ / 用途 |
|---|---|---|---|---|---|---|
| #0 | SV0 | D4 | `Servo` | 270° | `0x40` | `gripper`（メインハンド） |
| #0 | SV1 | D5 | `Servo` | 270° | `0x41` | `wall_f`（メインハンド） |
| #0 | SV2 | D6 | `Servo` | 270° | `0x42` | `wall_r`（メインハンド） |
| #0 | SV3 | D7 | **`TouchSensor`** | ― | `0x43` | `rotate_origin_sensor`（`rotate` の原点） |
| #0 | SV4 | D8 | **`TouchSensor`** | ― | `0x44` | `y_axis_r_origin_sensor`（`y_axis` 右のスイッチ） |
| #1 | SV0 | D4 | **`TouchSensor`** | ― | `0x48` | `y_axis_l_origin_sensor`（**メインハンド**。`y_axis` 左のスイッチ） |
| #1 | SV1 | D5 | **`TouchSensor`** | ― | `0x49` | `sub_y_axis_f_limit_sensor`（サブハンド） |
| #1 | SV2 | D6 | **`TouchSensor`** | ― | `0x4A` | `sub_y_axis_r_limit_sensor`（サブハンド） |
| #1 | SV3 | D7 | **`TouchSensor`** | ― | `0x4B` | `sub_lift_t_limit_sensor`（サブハンド） |
| #1 | SV4 | D8 | **`TouchSensor`** | ― | `0x4C` | `sub_lift_b_limit_sensor`（サブハンド） |
| **#2**（UNO R4） | SV0 | **D9** | `Servo` | 270° | `0x50` | `sub_rotate_r`（サブハンド） |
| **#2** | SV1 | **D11** | `Servo` | 270° | `0x51` | `sub_rotate_l`（サブハンド） |
| **#2** | SV2 | **D10** | `Servo` | 270° | `0x52` | `sub_pitch_r`（サブハンド） |
| **#2** | SV3 | **D6** | `Servo` | 270° | `0x53` | `sub_pitch_l`（サブハンド） |
| **#2** | SV4 | **D3** | `Servo` | 270° | `0x54` | `sub_offset`（サブハンド） |

**#2 のピンが連番ですらないのは MCU が違うから** —— 内蔵 CAN が D4/D5 を、RGB LED が
D8 を取っている。**ピン番号からスロットを推測してはならない**（#0 の D6 は SV2、
#2 の D6 は SV3）。

**基板 #1 は 2 つのロボットにまたがる。** SV0 だけがメインハンドで、SV1〜SV4 が
サブハンドである。CAN 上は両ロボットが `can_generic` を共有するので成立するが、
**配線はメインハンドから基板 #1 まで引く必要がある** —— 基板は「どのロボットのものか」
ではなく「どのバスに繋がっているか」でしか区切られていない。

**基板 #2 は通電した瞬間に 5 本とも `initialAngleDeg` へ駆動する**（`setup()` が attach
して初期角を出す）。全スロットが `Unused` だった間は attach すらしなかったので、
「電源を入れても何も起きない基板」ではなくなっている。**初期角は 2026-09-10 に実測して
焼き込み済み、可動範囲は全域のまま未実測**。

行が実行時にしか決まらないので、`ServoChannel g_channel[]` は静的初期化子を持てない。
**初期角と可動範囲は `setup()` が `begin()` で入れる**（`ServoChannel.h`）。`begin()` を
呼ばないチャンネルは出力を許可しないので、`Unused` のスロット宛に指令が届いても
パルスは 1 発も出ない。

**デバイス ID は表に持たない。** スロットの添字がそのまま ID の下位 3bit、DIP の
基板番号がその上の 3bit になるので、配線で役割を変えても ID は動かない。役割を変える
ときは**その基板の要素**の `SlotRole` を書き換えるだけでよい。**基板 #2 はピン配置が
#0/#1 と全く違うのに `0x50`〜`0x54` がスロット順に並ぶ**のは、この性質があるため
（PC 側 yaml の `can_id` は配線に引きずられない）。

**表に無い基板番号は全スロット `Unused` のまま据え置く**（Nano 用バイナリでは 2 以上、
R4 用バイナリでは 2 以外）。黙って
基板 #0 の行を使うと、DIP を回しすぎた基板が別の基板の役割とデバイス ID を名乗り、
同じ ID の 2 ノードが違うデータを送ってバスがエラーフレームで埋まる。据え置けば ID が
付かないので、「デバイス ID 未設定 → LED 赤の速い点滅・駆動拒否」へそのまま乗る
（DIP を 8 以上へ回したときと同じ扱い）。

**`SlotRole` は `Unused` が 0 である。** 表は 1 基板あたり `kServoSlotCount` 個を並べるので
書き忘れた要素はゼロ埋めされる。先頭が `Servo` だと、書き忘れたスロットが黙って
「サーボとして駆動するピン」になり、そこにスイッチが繋がっていれば通電したまま叩く。
ゼロ埋めは `pin = 0` としても現れるので、**UART（D0/D1）を `kFixedPins[]` に予約してある**
—— 予約が無いと 1 スロットぶんの書き忘れだけが衝突検査を素通りする（2 つ以上なら
スロット間のピン重複で落ちる）。**`kFixedPins[]` の中身は MCU ごとに違う**（Nano は
MCP2515 の INT/CS と SPI 3 本 + RGB(D9)、R4 は RGB(D8)）ので、検査の網もそのバイナリが
担当する基板ぶんしか張られない。**R4 の CAN（D4/D5）だけは `kFixedPins[]` に写さず、
別の `static_assert`（`pinsAvoidCan()`）が variant のマクロ `PIN_CAN0_TX` / `PIN_CAN0_RX` を
直接見る** —— 正はコアの variant なので、`config.h` に写しを置くと**写しだけが古くなる**
経路ができる。

**基板の要素ごと書き忘れると、その基板は全スロット `Unused` になる**（上の「表に無い
基板番号」と同じ扱い）。安全側には倒れるがビルドは通るので、症状は「焼いたのに
`FEEDBACK` が 1 通も来ない」だけになる。**PC からは配線不良と区別が付かないので、
まず基板の LED を見ること**（赤の速い点滅 = デバイス ID 未設定）。

**センサは PC 側 `config/<robot>.yaml` の `sensors:` セクションに登録する。**
`motors:` に置くと動作確認・目標値再送・UI のモータ一覧に「常に 0 のモータ」として
並ぶ。登録しないと受信ループがそのフレームを誰にも配らず、接触が PC まで届かない。

> **`constexpr` のループで `continue` を使わないこと。** avr-gcc 7.3 は `constexpr` 評価中の
> `continue` で増分式を飛ばし、無限ループになってビルドが落ちる。実際 `Unused` スロットを
> 1 つ置いただけでこれを踏んだ。条件は `if` の入れ子で書く。

デバイス ID の割り当ては仕様書 §2.2 の表と `config/*.yaml` を参照。
**デバイス ID はバス単位でロボット横断に一意**でなければならない。

### solenoid — チャンネル表

電磁弁基板は **6ch すべてが同じ役割**（電磁弁またはそれに準じる ON/OFF 負荷の駆動口）で、
サーボ基板のような役割の切り替えは無い。`config.h` の `kSolenoidChannels[]` が持つのは
ピンと表示名だけ。

| チャンネル | ピン | 回路図 | デバイス ID | モータ |
|---|---|---|---|---|
| ch0 | PB7 | `PUMP1_SW` | `0xC0` | `valve_1`（サブハンド） |
| ch1 | PB6 | `PUMP2_SW` | `0xC1` | `valve_2` |
| ch2 | PB5 | `PUMP3_SW` | `0xC2` | `valve_3` |
| ch3 | PB4 | `PUMP4_SW` | `0xC3` | `valve_4` |
| ch4 | PB3 | `PUMP5_SW` | `0xC4` | `valve_5` |
| ch5 | PA15 | `PUMP6_SW` | `0xC5` | `valve_6` |

**基板のシルクは `PUMP*` だが、ファームの表示名は PC 側 yaml のモータ名に合わせてある。**
`candump` とシリアルログと `config/sub_hand.yaml` を突き合わせるとき、同じものが
2 つの名前で呼ばれていると対応表を頭の中で引くことになる。

**吸気ポンプはこの基板ではなく DC 基板が動かす**（仕様書 §9.6）。
6ch はすべて弁で埋まっており、またポンプは起動電流が大きく `max_duty` で立ち上がりを
抑えられる DC 基板の側が適している。**排気の駆動源は無い** —— 弁が三方弁で、閉じた側が
吸着パッドを大気開放する。

`config.h` は STM32 HAL を include せず、ポートを自前の `Port` enum で持つ。
HAL の `GPIOA` / `GPIOB` はポインタへのキャストを含むマクロで `constexpr` 文脈に
持ち込めないため、取り込むと**ピンの重複検査がビルド時にできなくなる**。
`src/app.cpp` の `static_assert` が、ポートとピンの組で全ピン（CAN / UART / LED / DIP を
含む）の重複を検査し、あわせて CubeMX 生成の `main.h` と表が一致していることも見る。

## 到達フラグの意味は基板で違う（重要）

| 基板 | 到達フラグ | 何を意味するか |
|---|---|---|
| **DC** | 常に 0 | フィードバックを一切持たない |
| **電磁弁** | 常に 0 | 弁が開いたかを観測する手段が無い（圧力センサもリミットスイッチも無い） |
| **サーボ** | ファームの**推定** | 補間が完了した時点で立てているだけで、サーボが実際にそこへ行ったかは見ていない（仕様書 §7.3） |

**サーボは脱調・過負荷・メカ干渉で実際には動いていなくても「到達」と報告する。** PC 側
`move_to` はこのフラグで次のステップへ進むため、**機構が引っかかっていてもシーケンスは
進んでしまう。** 危険な動作には必ず `require_trigger` を付けて人間の目視確認を挟むこと
（`sequences/main_hand.py` のハンド閉じ等）。同じ理由で `FEEDBACK` の位置は「補間中の
指令角」であって実測ではない（仕様書 §7.4）。

DC 基板と電磁弁基板は自動判定ができないので、**動くところまでを動作確認シーケンスが担い、
実際に動いたかは人が目視・打音で確認する**。理由は
[`../docs/invariants.md`](../docs/invariants.md) の §7。

## config.h の要確認項目（通電前に必ず）

### dc_motor

`firmware/dc_motor/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。
基板・データシート・実測と突き合わせること。**下の表と config.h のマーカーは 1 対 1 で対応する。**
表にあってマーカーが無い（またはその逆の）項目を作ると、通電前チェックリストとして
どちらを信じればよいか分からなくなるため、項目を増やすときは必ず両方に入れる。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kRefActiveLow` | `true` | 物理非常停止 `REF`（D2）の極性。LOW = 押されている | **反転していると、押しても止まらず離すと止まる基板になる。最優先で確認** |
| `kDirForwardIsLow` | `true` | 方向ピンの論理。サンプルの `duty >= 0 ? LOW : HIGH` に準拠 | 全チャンネルが指令と反対に回る |
| `kPinPwm[3]` / `kPinDir[3]` | `{11,10,9}` / `{12,3,7}` | 基板の PWM 線と方向線。**CAN の D4/D5 と重ならないこと** | `main.cpp` の `static_assert` がピンの重複と CAN 衝突をビルド時に弾く |
| `kPinDip[2]` | `{1,0}`（D1, D0） | 基板の DIP がどのピンに落ちているか | オフセットが化けて別のアクチュエータが動く |
| `kDefaultMaxDuty` | `0.30` | モータとギヤ比が決まってから詰める（サンプルは 50%） | 大きすぎると機構に無理がかかる。小さすぎると回り出さない |
| duty 0 の挙動 | — | ハーフブリッジがコーストになるかブレーキになるか | 機構の噛み込みからの復帰性が変わる。この基板には出力禁止（`DIS`）が無く、停止＝PWM 0% だけ |

**この基板はフィードバックを一切持たない。** エンコーダ・電流センス・温度センサとも非搭載で、
`FEEDBACK` の位置・速度は常に 0、到達フラグも立たない（仕様書 §3.2 / §8）。
位置・速度制御と PID は実装ごと存在せず、`duty` だけを受理する。

`HAS_RGB_LED` は dc_motor / servo とも既定 `1`。どちらも基板にシリアル RGB LED が
1 個載っており（DC は D6、servo は **#0/#1 が D9・#2 が D8**。#2 では D9 をサーボ SV0 が
使うため）、`platformio.ini` の `lib_deps` は
**`adafruit/Adafruit NeoPixel`** を指す。**FastLED は使わない** —— WS2812 を 1 個
光らせるだけに引くと `fl/json.cpp` まで丸ごと付いてきて Nano の Flash 32KB に収まらず、
さらにヘッダ走査で framework 同梱の I2S を巻き込む（`lib_ignore` が要るのはそちらの話）。

表示規則は 2 枚で同じ。**赤の速い点滅 = CAN 不通 / CAN 送信が連続失敗 /
デバイス ID 未設定**、**橙 = 緊急停止ラッチ中**、**青のハートビート = 平常**。
基板が違うたびに色の意味が変わると、現場で 2 種類の対応表を覚えることになる。

**平常に緑を使ってはならない（緑のランプの使用が禁止されている）。** 橙
`(255, 96, 0)` は緑ダイを 96 で点けるが発色はオレンジなのでそのままにしてある。
色は 2 枚の `main.cpp` に別々に書かれているので、**片方だけ直すと表示規則が
静かに食い違う**（色は native テストの届かない翻訳単位にある）。

**servo 側の `show()` は色が変わったときだけ呼ぶ**（**理由が効くのは AVR = #0/#1 だけ
だが、`main.cpp` は 2 env で共有しているので規則はそのまま守ること**）。AVR 版
`Adafruit_NeoPixel::show()` は 1 LED あたり約 30us 割り込みを禁止し、その窓に
Servo ライブラリの Timer1 割り込み（パルス終端）が当たると**そのパルスだけが最大 30us
伸びる**。`kServoPulse270` は 7.04us/deg なので約 4.3deg のヒゲになり、
grip 5deg / 壁 6deg の微小ストロークではほぼ全域に相当する（次のフレーム 20ms 後に
正しい幅へ戻るので機構への影響は一瞬）。**`show()` を呼ぶ回数を増やしてはならない** ——
`updateMotion()` の直後や毎ループの位置へ動かすと、当たる確率がそのまま比例して上がる。

### servo

`firmware/servo/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。
表にはそれに加えて、仮値ではないが機構ごとに判断が要る既定値
（`initialAngleDeg` / `kEStopDetach`）も入れてある。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kServoBoards[][].limits.angleMinDeg` / `angleMaxDeg` | 駆動する 8 スロットとも `0.0` / `270.0` = **サーボの全可動域**（クランプは実質効いていない） | 機構を付けた状態で当たらない可動範囲を、**スロットごとに**実測する（定数は 1 本ずつ独立している。共有すると 1 スロットの都合で他スロットのクランプまで一緒に緩む） | **広すぎるとサーボがメカストッパに当たったまま停動し、短時間で焼損する。最優先で確認**（狭すぎる分はクランプで止まるだけ） |
| `kServoBoards[][].role` | #0 = サーボ 3 + センサ 2 / #1 = **センサ 5** / #2 = **サーボ 5**（空きは 1 つも無い） | 実際に**どの基板に**何を何本繋ぐか（個数に制約は無い）。**足すには基板を 1 枚増やす**（DIP=3）—— 既存のスロットを流用すると、そこを使っていた機構が黙って動かなくなる | 役割がずれると、指令した先と違うものが動く。**基板番号を間違えると隣の基板の役割になる** |
| `kServoBoards[][].sensorActiveLow` | 7 本とも **`false`**（NC 接点。非接触 = LOW・接触 = HIGH）。確定は #0 SV3 と #1 SV1〜SV4、残る 2 本（#0 SV4 / #1 SV0 = `y_axis` 左右）は仮値 | センサの極性。**1 スロットずつ** `candump` で非接触・接触の両方の `FEEDBACK` を見て確定する（**確定した 1 つを他のスロットへ広げてはならない** —— 極性はスロットごとの配線でしか決まらない） | 逆だと「触れていないのに触れている」と報告し続け、原点合わせが即座に終わる |
| `kServoBoards[][].pulse` | 全スロット `kServoPulse270` = `{500, 2400, 270.0}` | 各スロットに挿す**サーボの型**とデータシートのパルス幅・可動角（`kServoPulse180` は仮置き） | 指令角と実角がずれる。上端で当たり続ける |
| `kServoBoards[][].pin` | #0/#1 = `4`/`5`/`6`/`7`/`8`、**#2 = `9`/`11`/`10`/`6`/`3`** | 基板の信号線。**避けるピンが MCU で違う** —— #0/#1 は SPI(D11-13) / MCP2515(D3,D10) / RGB(D9) / DIP(A0-A3)、**#2 は内蔵 CAN(D4,D5) / RGB(D8) / DIP(A0-A3)** | `main.cpp` の `static_assert` がビルド時に弾く（**検査するのはそのバイナリが担当する基板のぶんだけ**） |
| `kPinDip[4]` | `{14,15,16,17}`（A0–A3） | 基板の DIP がどのピンに落ちているか。**3 枚とも A0–A3 で、番号（14–17）も一致する**（Nano と UNO R4 でたまたま同じ） | オフセットが化けて別のアクチュエータが動く |
| `kServoBoards[][].initialAngleDeg` | #0 = `0.0`/`270.0`/`90.0`、#2 = `0.0`/`270.0`/`260.0`/`10.0`/`0.0`（PC 側位置定数の初期姿勢と対） | 電源投入時に持っていく角度 | 起動した瞬間に機構が動く |
| `kEStopDetach` | `false` | 緊急停止時に脱力させたい機構があるか | `true` にすると壁が自重で倒れ、把持中のワークを落とす |

#### CAN の持ち方は `can_backend.h` で分ける（#0/#1 と #2）

**MCU 差は `src/can_backend.h` の薄いインタフェース（`begin` / `send` / `poll`）だけに
閉じ込め、`main.cpp` は MCU に依らず 1 コピーのまま**にしてある。実装は
`can_mcp2515.cpp`（Nano / SPI 外付け）と `can_r4.cpp`（UNO R4 / 内蔵ペリフェラル）。
`main.cpp` を分岐で埋めると、安全機構とスロット処理が 2 通りに分かれて片方だけ古くなる。

**送信バッファの本数も #0/#1 と #2 で違う。** MCP2515 は TX 3 本 + ライブラリが空きを
待つが、**R4 の `Arduino_CAN` は標準 ID の mailbox を 1 本しか使わない**（`R7FA4M1_CAN.cpp`
の `write()` が `CAN_MAILBOX_ID_0` 固定）。5 スロットぶんの `INFO` を同じ反復でまとめて
送ると **#2 では 2 通目以降が必ず落ち**、`kInfoIntervalMs`(1000) が
`feedback_interval_ms`(10) の整数倍で位相が固定されるため**毎回同じスロットだけが出て
残り 4 本は永久に出ない**（＝そのスロットの焼き忘れ検出が黙って無効になる）。
**対処済み** —— DC 基板・電磁弁基板と同じ「**1 反復 1 通・落ちたスロットは添字を進めず
次の反復で送り直す**」（`loop()` の `g_infoPendingSlot`）へ揃えてあり、**`#if` による
MCU 分岐は入れていない**。1 反復 1 通は 3 枚に共通の規則で Nano でも成立する
（PC から見える振る舞い —— 1Hz・内容・CAN ID —— は変わらず、5 通が数反復に散るだけ。
むしろ `sendMsgBuf` の空き待ちで `loop()` が伸びる最悪値は縮む）ので、
**`kFirmwareVersion` も上げていない。**

**R4 の `Arduino_CAN` には受信フィルタの API が無い。** したがって #2 にはバス上の
全フレームが上がってくるが、宛先判定は `routeFrame` が行うので**自分宛でないフレームは
そこで捨てられる** —— DC 基板（同じ R4 内蔵 CAN）で実績のある形である。
下の MCP2515 のフィルタ話（#0/#1 限定）は、この差を埋めるためのものではなく
**MCP2515 の受信バッファが 2 段しかないことへの対処**なので、#2 には当たらない。

#### 以下は **#0/#1（Arduino Nano）に限る話**

**Arduino Nano は Flash 32KB / SRAM 2KB しかない。** ライブラリを足したらビルド時の
使用率を必ず見ること（現状は **Flash 57.9% / 17,786B、RAM 48.9% / 1,001B**。
avr-gcc 7.3 / `pio run -e nano -d firmware/servo` の実測）。RGB LED に FastLED を使うと
収まらないので Adafruit NeoPixel にしてある。足りなくなったら `ENABLE_SERIAL_DEBUG` を
0 にして落とす。**#2（UNO R4）は 256KB / 32KB なのでこの制約に当たらないが、
`main.cpp` と `lib_deps` は共有しているので、足したライブラリは Nano 側にも乗る** ——
**使用率は必ず `-e nano` で確認すること**（R4 側だけ見て「入った」と判断すると、
気付くのは Nano ビルドが落ちたときになる）。

**MCP2515 を 16MHz 水晶で 1Mbps** はサンプルポイントの余裕が乏しい設定として
知られている。実機で通信エラーが出るなら、バス全体を 500kbps へ下げる判断が要る
（M3508・EDULITE 側も揃える必要がある）。

**受信フィルタは必ず張る（`begin(MCP_STDEXT, ...)` + `configureCanFilters()`）。**
かつて `begin(MCP_ANY, ...)` でマスク／フィルタを丸ごと無効化しており、
`can_generic` に流れる自作モタドラ 14 台 × 100Hz の `FEEDBACK` と 1Hz の `INFO`
（約 1,400 fps）を**全部 SPI で読み出して捨てて**いた。MCP2515 の受信バッファは
**RXB0 / RXB1 の 2 段しかなく**、`sendMsgBuf` は空き TX 待ちと TXREQ クリア待ちの
二段で 1 通あたり最大 5ms（`TIMEOUTVALUE` 2500us × 2）まで伸びる。loop が 1.4ms
止まれば 2 段は溢れ、**落ちるのがブロードキャスト `E_STOP` だと「たまに緊急停止が
効かないサーボ基板」**になる。

- 通す ID は `MotorCanProtocol.h` の `kEStopAndSetTargetFilter` / `kSetParamFilter`
  から導く。`main.cpp` に置くのはビット配置の変換だけ（電磁弁の `toFilterReg()` と同じ形）
- **`mcp2515_write_mf` は `ext=0` のとき `ulData` の bit16 以降を SIDH/SIDL へ詰める。**
  標準 ID はそこへ載せ、低位 16bit は 0 にする（非 0 にすると標準フレームでも
  データ 1-2 バイト目と比較され始める）
- **`MCP_STD` を渡してはならない。** coryjfowler/mcp_can では「シリコンのバグ」として
  分岐ごとコメントアウトされており、渡すと `begin()` が `MCP2515_FAIL` を返して
  **基板がまるごと緊急停止ラッチに落ちる**。`MCP_STDEXT` はフィルタを有効にしたまま
  各フィルタの EXIDE で標準／拡張を判別する設定で、全 6 本を標準 ID として書き直せば
  拡張フレームは 1 通も通らない
- RXB0（マスク 0 / フィルタ 0-1）に `E_STOP` + `SET_TARGET`、RXB1（マスク 1 /
  フィルタ 2-5）に `SET_PARAM` を割り当てる。時間に厳しい方を優先度の高い RXB0 に置き、
  RXB0 は BUKT（ロールオーバー）付きなので、埋まっている間に来た `E_STOP` は
  RXB1 のフィルタに関係なく RXB1 へ落ちる

**`sendMsgBuf` の戻り値は捨てない。** 連続失敗が `kCanTxFailStreakAlarm`（50 通 ≒ 100ms
分の全滅）に達したら LED を「今すぐ直さないと使えない」表示（赤の速い点滅）へ倒す。
捨てると、`FEEDBACK` も `INFO` も出ていない基板が平常と同じ青のハートビートを出し続け、
PC 側からは STALE にしか見えないので配線不良と区別が付かない。

### solenoid

| 定数 | 現在値 | 確認すること |
|---|---|---|
| `kSolenoidChannels[].pin` | PB7 / PB6 / PB5 / PB4 / PB3 / PA15 | 回路図の `PUMP1_SW`〜`PUMP6_SW` と一致すること（`static_assert` が `main.h` と突き合わせる） |
| `kDipActiveLevel` | `0`（LOW = ON） | **DIP のコモンが GND へ落ちていること。** VCC 側へ引く配線なら極性と `solenoid.ioc` の `GPIO_PuPd` を揃えて反転する |
| DIP の `GPIO_PuPd` | `GPIO_PULLUP`（`solenoid.ioc`） | 外部プルアップが無い前提。**外すと読みが不定になり、電源投入のたびに違うデバイス ID を名乗る** |
| `kFirmwareVersion` | `2` | プロトコルかピン配置を変えたら上げる。**`config/*.yaml` の `expected_firmware` も同時に揃える**（揃え忘れると「正しく焼いたのに全部 FAULT」になる）。3 枚とも `tests/test_firmware_version_sync.py` が `uv run pytest` で突き合わせるので、片方だけ変えたコミットは必ず落ちる |

**この基板に物理非常停止入力（DC 用の `REF`）は無い。** サンプル基板にそのピンが
存在しないためで、物理停止は DC 基板が受けて PC 経由で伝わる（`FEEDBACK` の緊急停止ビット
→ サーバー全体の緊急停止 → ブロードキャスト `E_STOP`）。**DC 基板が繋がっていない
構成では物理停止が効かない。**

**`AutoBusOff` は有効にしてある**（`solenoid.ioc` の `CAN.ABOM=ENABLE`）。**CubeMX の
既定は `DISABLE` で、そのままだと一度 Bus-Off に落ちた基板は電源を入れ直すまで復帰しない。**
MCP2515（サーボ基板）も R4 内蔵 CAN（DC 基板）も自動復帰するので、既定のままだと
この 1 枚だけが「試合中にノイズで落ちたらそれきり」という特性を持つことになる。

実機で踏んだ形はこうだった —— CAN の配線が繋がっていない状態で通電したところ、
送信エラーカウンタが 255 に達して Bus-Off（`CAN_ESR = 0x00F80057`: `BOFF=1` /
`TEC=248` / `LEC=5`）。この状態は**配線を直しても直らない**ので、原因が配線なのか
基板なのか切り分けられなくなる。`ABOM=ENABLE` にすると再送を試み続けるので、
配線を直した瞬間に自力で復帰する。

**CAN のビットタイミングはサンプルの `.ioc` から直してある。** サンプルは
`BS1=2TQ` / `BS2=7TQ` / `Prescaler=3` で、**ビットレートこそ 1Mbps だが
サンプルポイントが (1+2)/10 = 30% しかない**。`can_generic`（CANable）は 74.7% なので、
この組み合わせでは通信できない。

現在は `Prescaler=3` / `BS1=6TQ` / `BS2=3TQ` / **`SJW=3TQ`** = 10tq / **70%**
（APB1 30MHz、tq = 100ns）。

**`SJW` を 3TQ 取れることがこの構成の理由。** ただし **Bosch の許容クロック誤差は
2 つの式の小さい方**であり、片方だけ見て裕度を語ってはならない。

```
条件1:  df <= SJW / (2 * 10 * NBT)
条件2:  df <= min(Phase_Seg1, Phase_Seg2) / (2 * (13 * NBT - Phase_Seg2))
```

| 構成 | サンプルポイント | 条件1 | 条件2 | **実効裕度** |
|---|---|---|---|---|
| 15tq / `SJW=1TQ` | 73.3%（CANable に近い） | 1/300 = 0.33% | — | **0.33%** |
| **10tq / `BS1=6` / `BS2=3` / `SJW=3`（現行）** | 70% | 3/200 = 1.5% | 3/(2×127) = 1.18% | **1.18%** |
| 10tq / `BS1=5` / `BS2=4` / `SJW=4`（改善案） | 60% | 4/200 = 2.0% | 4/(2×126) = 1.59% | **1.59%** |

**この基板は水晶を持たず HSI（内蔵 RC）で動く**（`.ioc` の `Mcu.Pin` に
`PF0`/`PF1` が無く、`SystemClock_Config` も `RCC_OSCILLATORTYPE_HSI`）。
`SJW=1TQ` の 0.33% では HSI の誤差に対して規格を満たさないので、サンプルポイントを
CANable へ近づけるより **`SJW` を大きく取れる構成を優先**している。STM32 の `SJW` は
最大 4TQ なので、tq 数を減らすほど許容誤差が上がる。

**現行構成の実効裕度は 1.18% で、HSI の公称 ±1% に対して余裕がほとんど無い。**
データシートの ±1% は**25℃で工場トリム済みのとき**の値であり、全温度範囲では
これより数倍悪くなる。**基板が温まると規格を割る**ことになり、症状は `LEC=5`
（Bit dominant error）—— 下の「ビットレートが合っていてもサンプルポイントが
ずれていると 1 通も通らない」とまったく同じ見え方をするので、配線を疑って
時間を使うことになる。

**改善案（`BS1=5TQ` / `BS2=4TQ` / `SJW=4TQ`）を採るかは実機判断。**
実効裕度は 1.18% → 1.59% に上がるが、サンプルポイントが 70% → 60% へ下がって
`can_generic`（CANable / 74.7%）から遠ざかる。長さの短いバスなら 60% でも通るが、
どちらが効くかは実機でのエラーカウンタでしか分からない。**`.ioc` の変更には CubeMX が
要る**ので、通信エラーが出てから判断すればよい。

**`AutoRetransmission = ENABLE`（`main.c:187`）。これは選んだ設定であって既定ではない。**
CubeMX の既定は `DISABLE` だが、それだと `sendFrame()` の検出口が 2 つとも成立しない ——
1 回の送信試行が成功・エラー・調停負けのどれで終わってもメールボックスが解放されるので、
`HAL_CAN_GetTxMailboxesFreeLevel() == 0` にも `HAL_CAN_AddTxMessage() != HAL_OK` にも
ならず、**トランシーバが死んでいても 1 バイトもバスへ出ていなくても `g_txFail` は 0 のまま**
LED は平常のハートビートを出し続ける。DC 基板（R4 内蔵 CAN は既定で再送する）と
サーボ基板（MCP2515 も既定で再送する）は同じ状況で規則どおり赤へ倒れるので、
**この基板だけが検出できないという非対称は意図されたものではなかった。**

`ABOM=ENABLE` と組み合わせると、Bus-Off で保留中の送信は中止されてメールボックスが
解放される。**したがって「配線を抜けば必ず LED が赤になる」とは言い切れない** ——
ABOM の復帰（128×11 ビット ≒ 1.4ms）と送信レートの噛み合い次第では連続失敗が
`kCanTxFailStreakAlarm`(50) に届かないことがある。実機で赤にならなかった場合は、
`HAL_CAN_GetError()` / `HAL_CAN_GetState()`（あるいは `CAN_ESR` の `BOFF`）を
`BoardIndication` の urgent 条件に足すこと。そちらは「送信できていない」を直接見るので
再送設定に依存しない。

**トレードオフ**: ビットエラーで落ちたフレームをハードウェアが繰り返し再送するので、
上の裕度の話（実効裕度 1.18% / 温度で規格割れ / 症状は `LEC=5`）が現実になった場面では
エラーフレームと再送の応酬が長引き、共有バス `can_generic` の他ノード（DC / サーボ /
センサ）にも波及しうる。裕度の改善案を採るかどうかは、こちらとセットで判断すること。

なお仕様書 §5 の規則④「`FEEDBACK` は落ちたら諦める」は**アプリが再送キューを持たない**
という意味であって、ハードウェアの再送を禁じるものではない。電磁弁の `FEEDBACK`
（`0x3C0`〜`0x3C5`）はバス上で最も優先度が低いので混雑時に最も取りこぼしやすいが、
100Hz で送り続けるので PC 側の STALE 判定（既定 500ms）には遠く届かない。

**ビットレートが合っていてもサンプルポイントがずれていると 1 通も通らない。**
症状は `CAN_ESR` の `LEC=5`（Bit dominant error）—— ビットの立ち上がり途中、まだ前の
ビットの dominant が残っている領域をサンプルするために起きる。`candump` には何も
出ないので、そのままだと配線を疑って時間を使うことになる。

**`.ioc` を CubeMX で再生成したら、次の 6 点を必ず確認すること。**

1. `Prescaler=3` / `BS1=6TQ` / `BS2=3TQ` / `SJW=3TQ`（＝ NBT 10tq / SP 70%）が戻っていないか
   —— CubeMX の既定に戻ると `BS1=2` / `BS2=7` で SP 30% になり 1 通も通らない
2. `ABOM=ENABLE`（`AutoBusOff`）—— 既定は `DISABLE` で、一度 Bus-Off に落ちたら
   電源を入れ直すまで復帰しない
3. **`AutoRetransmission = ENABLE`（`.ioc` では `CAN.NART=ENABLE`）が戻っていないか**
   —— CubeMX の既定は `DISABLE` で、戻ると `sendFrame()` の送信失敗検出が丸ごと
   無効になる（上の段落を参照）。**`.ioc` のキー名はレジスタのビット名（`NART`）だが、
   値は生成される HAL のフィールド値そのままである** —— 同ファイルの
   `CAN.ABOM=ENABLE` が `AutoBusOff = ENABLE` を生んでいるのが根拠。
   キー名につられて `DISABLE` と書くと意味が反転する
4. 上の表の**両方の条件**で裕度を計算し直す（条件2 を忘れると 1.5% と読み違える）
5. `RCC_OSCILLATORTYPE_HSI` のまま（水晶は載っていない）であること
6. `Core/Src/main.c` の USER CODE 領域に `setup()` / `loop()` の呼び出し以外が
   入っていないこと

#### 実機で確認済みの動作（2026-08-29 / CAN_TX・CAN_RX のジャンパ修正後）

実基板 1 枚を `can_generic` へ繋いで、仕様書の主要な項目を一通り確認した。
確認は `candump` / `cansend` と、SWD 経由の GPIO 出力レジスタ読みで行っている。

| 確認したこと | 結果 |
|---|---|
| `FEEDBACK` 送信（§3.2） | `0x3C0`〜`0x3C5` の 6ch すべてが 100Hz で流れる |
| 起動直後の状態フラグ（§5.4） | `0x20` = bit5（起動後まだ `SET_TARGET` を受けていない）のみ |
| `INFO` 送信（§3.4） | `0x4C0`〜 に `01 03 00` = 版 1 / 基板種別 3（電磁弁）/ 役割 0（アクチュエータ） |
| `SET_TARGET` の受理（§9.2） | `1C0#030100` で `on_=1`、`everFed_=1`、フラグが `0x00` へ |
| 出力（§9.1） | 6ch 同時 ON で `GPIOB_ODR=0xF8`（PB3-PB7）+ `GPIOA_ODR` bit15（PA15）。**`config.h` のチャンネル表と実配線が一致** |
| 到達フラグを立てない（§9.3） | 指令を受けてもフラグは `0x00` のまま（bit0 が立たない） |
| コマンドウォッチドッグ（§5.1） | 送信を止めて 500ms で全 ch 消磁。フラグに bit2 が立つ |
| 緊急停止のラッチ（§3.5 / §9.4） | `0FF#000000` で即消磁・`latched_=1`・**`on_=0`**（目標ごと落ちる）。フラグ `0x06` |
| マジックバイトの検証（§3.5） | `0FF#010000`（`0x5A`/`0xA5` なし）では**解除されない**（`0x06` のまま） |
| 正規の解除（§3.5） | `0FF#015AA5` で緊急停止ビットが消える（`0x04` = ウォッチドッグのみ残る） |

#### !!! 基板の既知問題: CAN_TX / CAN_RX が逆配線 !!!

**2026-08 時点の実機は、MCU の 21 番ピン（PA11 = CAN_RX）と 22 番ピン
（PA12 = CAN_TX）がトランシーバの TXD / RXD と逆に繋がっている。**
STM32F303K8 の CAN は PA11=RX / PA12=TX に固定で、LQFP32 には代替ピンが無いため
**ファームウェアでは直せない。** パターンカットとジャンパで入れ替えること。

**修正するまで長時間通電しないこと。** MCU の PA12（AF push-pull 出力）と
トランシーバの RXD（push-pull 出力）が出力同士でぶつかる。応急処置として
PA12 を入力へ落とせば衝突は止まる（リセットで元に戻る）:

```bash
# GPIOA_MODER の PA12 (bit24-25) を 00 = 入力にする
$OCD/bin/openocd -s $OCD/openocd/scripts -f interface/stlink.cfg \
  -c "set WORKAREASIZE 0x2000" -f target/stm32f3x.cfg \
  -c "init" -c "halt" -c "mww 0x48000000 0x68A80400" -c "resume" -c "shutdown"
```

**この配線ミスを最短で見抜く方法は 2 つある。**

1. **バスから物理的に切り離して `CAN_ESR` を読む。** 誰もいないバスなら送信は
   本来 `LEC=3`（ACK error）になる。それが `LEC=5`（Bit dominant error）のままなら、
   **自分の RXD が dominant を返している** —— バスではなく基板内部の問題だと分かる
2. **内部ループバックを試す。** ループバックは RXD ピンを一切見ないモードなので、
   ここで成功すれば送信ロジックは正常で、原因は RXD 経路に絞られる

CANH / CANL に recessive の 2.45V が出ていても、それはトランシーバが自分で
バイアスしているだけで、**送受信できることの証拠にはならない**。

#### 実機で CAN が繋がらないときの切り分け（SWD だけでここまで分かる）

ST-LINK が繋がっていれば、シリアルも CAN も無しで内部状態を読める。

```bash
OCD=~/.platformio/packages/tool-openocd
$OCD/bin/openocd -s $OCD/openocd/scripts -f interface/stlink.cfg \
  -c "set WORKAREASIZE 0x2000" -f target/stm32f3x.cfg \
  -c "init" -c "halt" -c "mdw 0x40006418 1" -c "mdw 0x48000010 1" -c "resume" -c "shutdown"
```

| 読む場所 | 意味 |
|---|---|
| `0x40006418`（`CAN_ESR`） | `bit2`=Bus-Off / `bit4-6`=最後のエラー種別 / `bit16-23`=送信エラーカウンタ |
| `0x48000010`（`GPIOA_IDR`）の `bit11` | `CAN_RX`（PA11）の実レベル。**アイドルで 0 ならトランシーバが dominant に張り付いている**（未給電・CANH/CANL 短絡）。1 ならバスはアイドルとして読めている |
| `_ZN12_GLOBAL__N_1L10g_deviceIdE`（`arm-none-eabi-nm` で引く） | DIP から解決した実効デバイス ID。0x00 が並んでいたら DIP の読みが失敗している |

**`WORKAREASIZE` の指定は省略できない。** OpenOCD の `stm32f3x.cfg` は既定で 16KB の
ワークエリアを取るが、**STM32F303K8 の RAM は 12KB しかない**ので、そのままだと
`Failed to write memory at 0x20003008` で書き込みが失敗する（RAM 末尾は `0x20003000`）。

**受信できているかは `g_channel[0]` を読めば分かる。** `arm-none-eabi-nm` で
`g_channel` のアドレスを引き、先頭 16 バイトを読む。

| オフセット | 中身 | 見かた |
|---|---|---|
| +0 | `timeoutMs_` | 既定 500（`0x1F4`） |
| +4 | `lastFedMs_` | **0 のままなら `SET_TARGET` を 1 通も受けていない** |
| +8 | `everFed_` | 同上。0 なら受信経路が死んでいる |
| +9 | `latched_` | 緊急停止ラッチ |
| +10 | `watchdogEnabled_` | `config.h` の `WATCHDOG_ENABLED` が写っているか |
| +12 | `on_` | 目標の ON/OFF |

`cansend can_generic 1C0#030100`（デバイス 0xC0 へ `on_off` = ON）を数通投げてから
読むと、**送信と受信のどちらが死んでいるか**が分かる。

**MCU 側だけを切り分けるには内部ループバックが早い。** 再ビルドも再書き込みも要らず、
レジスタを 3 本書くだけでよい（`INRQ` で初期化モード → `BTR` の `LBKM` を立てる → `INRQ` を戻す）。

```bash
$OCD/bin/openocd -s $OCD/openocd/scripts -f interface/stlink.cfg \
  -c "set WORKAREASIZE 0x2000" -f target/stm32f3x.cfg \
  -c "init" -c "halt" \
  -c "mww 0x40006400 0x00010011" -c "mww 0x4000641c 0x40390001" \
  -c "mww 0x40006400 0x00010010" -c "resume" -c "shutdown"
```

そのあと `CAN_ESR`（`0x40006418`）が **0**、`CAN_TSR`（`0x40006408`）の各メールボックスに
**`TXOK`（bit1）が立つ**なら、MCU・ビットタイミング・フィルタ・ファームはすべて正常で、
原因はトランシーバ以降（`TJA1441` の電源、`CANH`/`CANL` の配線、終端）に確定できる。
戻すときは普通に再書き込みすればよい（リセットで `LBKM` は消える）。

**トランシーバの電源は MCU とは別系統。** `TJA1441` の `VCC` は 5V で、基板上の
`SI-8050Y`（DC-DC）が主電源から作る。**ST-LINK が供給するのは MCU の 3.3V だけ**なので、
主電源を入れずに SWD だけ繋ぐと「MCU は動いていて LED も点滅しているのに CAN だけ
まったく通らない」状態になる。実機で最初に踏んだのがこれ。

## 安全に関する既定値

| 項目 | dc_motor | servo | 根拠 |
|---|---|---|---|
| 出力上限 | `max_duty` = `0.30`（**チャンネルごと**） | `angle_min` / `angle_max` でのクランプ（**スロットごと**） | 仕様書 §5.3 / §7.2。サーボは可動範囲外で停動すると焼損する |
| `command_timeout_ms` | `500`（**チャンネルごとに独立**） | `500`（**チャンネルごとに独立**） | 仕様書 §5.1 / §7.1。ラッチしない |
| `feedback_interval_ms` | `10`（チャンネルごとに位相をずらして送信） | `10`（同左） | 100Hz。緊急停止中・ウォッチドッグ作動中も送り続ける |
| ウォッチドッグ有効/無効 | `WATCHDOG_ENABLED` = `1` | 同左 | 仕様書 §5.1 / §8。`SET_PARAM` からは変更できない |
| 緊急停止・ウォッチドッグ時 | 出力停止（PWM 0%）。**出力禁止中の `SET_TARGET` は採用しない**。目標も 0 に落とす | **現在角を保持。出力禁止中の `SET_TARGET` は採用しない** | 仕様書 §7.5 / §3.5。受け付けると再送のたびに目標が更新され、解除した瞬間にその値で動き出す |
| 物理非常停止 | **`REF`（D2, LOW = 押下）でラッチ。離しても自動復帰しない** | 入力なし | 仕様書 §5.2。レベル追従だと PC の再送でスイッチを離した瞬間に動き出す |
| センサ入力 | 無し | **スロットに割り当て（現構成は基板 #0 の SV3 / D7 の 1 個。個数自由）。1 個ずつ独立した CAN デバイスとして FEEDBACK のセンサ入力ビットで報告するだけ** | 仕様書 §5.2。判断は PC 側。接触は異常ではないのでヘルスにも動作確認にも影響させない |
| 受け付けるモード | **duty のみ**（他は無視） | **position のみ**（他は無視） | 仕様書 §4 / §7.2。判定は `DcChannel` / `ServoChannel` / `SolenoidChannel` の `applySetTarget()` が持つ（`main.cpp` へ戻すと native テストの圏外になる） |
| 出力禁止中の `SET_PARAM` | — | **`0x03`〜`0x06` は効かせず保留し、出力の許可と同時に取り込む** | 仕様書 §7.6。`angle_min`/`angle_max` は目標角をクランプするので、素通しだと §7.5 の入口の拒否を `SET_PARAM` 経由で迂回できる |
| デバイス ID 未設定のチャンネル | **`FEEDBACK` も `INFO` も送らない**（LED 赤の速い点滅のみ） | 同左 | 仕様書 §2.2。CAN ID `0x300` は PC 側の `can_id` 範囲外なので誰も claim できず、2 枚以上が同時に未設定だとバスがエラーフレームで埋まる |
| シリアル上書き | **打ったチャンネルだけ / 2000ms で自動解除** | 同左 | 仕様書 §5.1。全チャンネルを無期限に養うと基板単位で最後の砦が外れる |
| 起動時 | duty 0 / 出力停止 / 緊急停止ラッチ解除済み（REF 押下時は即ラッチ） | 各チャンネル `initialAngleDeg` / 緊急停止ラッチ解除済み | 仕様書 §5.4 |

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
49.7 日へ伸ばすだけで同じ結果（ウォッチドッグの実質無効化）になるため（仕様書 §3.3）。

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

`test_protocol/` はプロトコル層・安全機構・`DcChannel`・`composeFeedbackFlags()` /
`applyCommonParam()` を対象とする。
**PID と速度・電流・温度は実装ごと存在しない**ので、対象にも入らない（仕様書 §8）。
実機が無くても以下を検出できる。

- CAN ID の組み立て／解析（予約値 `0b101`/`0b110`/`0b111` を無効として弾くこと）
- `E_STOP` が他のどのフレームよりも小さい ID になること（調停順。仕様書 §2.1）
- int16 固定小数点の往復と、`toRaw` が NaN と範囲外を飽和させること（仕様書 §4）
- `E_STOP` の解除がマジックバイト `0x5A` `0xA5` 揃いのときだけ通ること
- 状態フラグのビットが重ならず、頭から詰まっていること（仕様書 §3.2）
- `composeFeedbackFlags()` の 3 規則 —— **到達を立てられるのはサーボスロットだけ**（DC 基板と
  電磁弁基板では `reached` に何を渡しても立たない。仕様書 §8 / §9.3）、**センサスロットは
  緊急停止・ウォッチドッグ・到達を立てない**（§5.2）、アクチュエータスロットは
  `MotorSafety::statusFlags()` をそのまま中継すること。**この判断が 3 枚の `main.cpp` /
  `app.cpp` に書き写されていた頃は、DC 基板と電磁弁基板に `flags |= status_flag::kReached;` を
  1 行足しても全ケース緑だった**（写しが Arduino / HAL の翻訳単位にあり native テストが届かない）
- `FEEDBACK` / `INFO` の DLC 可変（位置・可動レンジを持つ基板だけが足すこと）
- `FEEDBACK` の位置が int16 で折り返さず飽和すること
- ウォッチドッグの満了・復帰・`millis()` 折り返し、ラッチ中も養えること
- ウォッチドッグを無効にしても緊急停止ラッチと「最初の指令まで出力禁止」は効くこと
- `command_timeout_ms` / `feedback_interval_ms` が範囲へ丸められること
- duty クランプ（`max_duty` 超過・負値・0）と、`splitDuty` が duty 0 で方向を反転しないこと
- 物理非常停止（`REF`）がラッチし、離しても自動復帰しないこと（仕様書 §5.2）
- 出力禁止中の `SET_TARGET` を `DcChannel` が入口で拒否すること

`test_board/` は基板共通部（`MotorCanRouter` / `PeriodicTimer`（`stagger()` を含む）/
`SerialLineBuffer` / `resolveDeviceIds()` / `parseSerialCommand()` / `blinkIntervalFor()`）を
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
- `angle_min` / `angle_max` でのクランプ（仕様書 §7.2）と、固定小数点経路が NaN を通さないこと
- スルーレート制限と、到達までの所要時間が `距離 / slew_rate` と一致すること（§7.3）
- 到達後は時間が経っても角度が動かないこと、目標が小さいときは減る方向へ補間すること
- `holdHere()` が目標を現在角で凍結し、それ以上動かないこと（§7.5）
- `setLimits()` が可動範囲を狭めたときに現在の目標をクランプし直し、角度を飛ばさないこと（§7.6）
- `SET_PARAM` が 1 つの ID 表（`0x00`〜`0x06`）で復号され、各基板が使わない ID を無視すること
- `millis()` の 49.7 日折り返しで補間が巻き戻らないこと
- 緊急停止ラッチ中に 20Hz の再送を受けても 1 度も動かないこと（§7.5）
- ラッチ中に指令された角度が解除後に実行されないこと（同じ受信バッチでの解除を含む）
- ウォッチドッグ満了の瞬間に 1 ティック分も進まないこと（凍結が補間より先であること）
- 電源投入後の最初の `SET_TARGET` は受理されること（養う → 受理判定の順序）

`main.cpp` はペリフェラル依存のため native テストの対象外
（`common.ini` の `test_ignore = *` は実機 env 側の設定で、共有テストを実機 env で
1 つも走らせないためのもの。個別のテスト名ではなくワイルドカードにしてあるのは、
テストを足すたびにここへ名前を書き足す必要を無くすため）。
