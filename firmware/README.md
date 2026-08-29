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
      src/MotorLoopTimer.h           PeriodicTimer（millis() 折り返しに耐える周期判定）
      src/MotorSafety.{h,cpp}        緊急停止ラッチ + コマンドウォッチドッグ + 物理停止入力
      src/SerialLineBuffer.{h,cpp}   デバッグシリアルの行組み立て（行の解釈は各 main.cpp）
      src/DcChannel.{h,cpp}          DC 1ch 分の結線（安全機構 + duty 目標。DC 用のみ使用）
      src/ServoMotion.{h,cpp}        角度補間・可動範囲クランプ・到達推定（サーボ用のみ使用）
      src/ServoChannel.{h,cpp}       上 2 つの結線（出力禁止中は指令を受け付けず先に凍結する）
      src/SolenoidChannel.{h,cpp}    電磁弁 1ch 分の結線（安全機構 + ON/OFF 目標。電磁弁用のみ使用）
  test/                native 環境の Unity テスト。PlatformIO の 2 つが test_dir = ../test で共有
    test_protocol/     プロトコル層・安全機構・物理停止・duty 分解・DcChannel
    test_board/        宛先判定・デバイス ID 解決・周期タイマ・シリアル行
    test_servo/        角度補間・可動範囲クランプ・到達推定・安全機構との結線
    test_solenoid/     電磁弁のデバイス ID・on_off の復号・SolenoidChannel の安全機構
  dc_motor/            DC モータ用モタドラのファーム（1 枚で 3 チャンネル）
    platformio.ini     固有行のみ（default_envs / extra_configs / test_dir / lib_deps）
    include/config.h   ピン配置・チャンネル表・機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・出力反映・CAN 送受信
  servo/               サーボ用モタドラのファーム（**Arduino Nano** / 1 枚で 5 スロット）
    platformio.ini     固有行のみ（default_envs / extra_configs / test_dir / lib_deps）
    include/config.h   ピン配置・スロット表・機体依存定数（要確認項目はここ）
    src/main.cpp       ペリフェラル初期化・補間ループ・MCP2515 送受信
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

**3 枚とも別の MCU に載っている。**

| | DC 用 | サーボ用 | 電磁弁用 |
|---|---|---|---|
| MCU | Arduino UNO R4 Minima（RA4M1 / 32bit / 3.3V） | **Arduino Nano（ATmega328P / 8bit / 5V）** | **STM32F303K8T6（Cortex-M4F / 32bit / 3.3V）** |
| CAN | R4 内蔵ペリフェラル（`Arduino_CAN`、D4/D5 固定） | **MCP2515 を SPI で外付け**（`mcp_can`） | **STM32 内蔵 bxCAN**（PA11/PA12 固定） |
| 出力 | `PwmOut`（R4 専用）+ 方向ピン | **`Servo` ライブラリ**（`writeMicroseconds`） | **GPIO の ON/OFF だけ**（PWM も方向ピンも無い） |
| Flash / RAM | 256KB / 32KB | **32KB / 2KB** | 64KB / 12KB |
| ビルド | PlatformIO（env `uno_r4_minima`） | PlatformIO（env `nano`） | **STM32CubeMX + CMake** |

`MotorCan` が `Arduino.h` も `stm32f3xx_hal.h` も include しないのは意図的で、PC 上の
native 環境でそのままコンパイルしてテストできるようにするため。**MCU もビルド系も
違ってよく、3 枚が同じソースを共有する。** `dc_motor/` と `servo/` は
`lib_extra_dirs = ../lib`、`solenoid/` は CMake の `target_sources` で同じ `MotorCan` を指す。

テストも `firmware/test/` を共有するので、**`pio test -e native` はどちらの
PlatformIO プロジェクトから回しても同じ全ケース（`test_solenoid` を含む）が走る。**
電磁弁基板のロジック層も、実機ビルドが CMake であることとは無関係にここでテストされる。
一方**実機ビルド（`pio run` / `cmake --build`）は 3 つとも確認すること。**

`common.ini` が持つのは本当に共通の部分（`lib_extra_dirs` / `test_framework` / native env と、
実機ビルドの共通フラグ）だけ。**`platform` / `board` / `lib_deps` は MCU が違うので
各プロジェクトの `platformio.ini` が持つ。**

## コマンド

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
# ユニットテスト（実機不要）。どちらも firmware/test/ の全ケースが走る
pio test -e native -d firmware/dc_motor
pio test -e native -d firmware/servo

# ビルド（env が基板ごとに違う）
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e nano -d firmware/servo

# 書き込み（サーボ基板 = Arduino Nano）
pio run -e nano -d firmware/servo -t upload
pio device monitor -e nano -d firmware/servo

# 書き込み（基板を USB で接続してから）
pio run -e uno_r4_minima -d firmware/dc_motor -t upload
pio run -e uno_r4_minima -d firmware/dc_motor -t upload --upload-port /dev/ttyACM0

# シリアルモニタ（115200 baud）
pio device monitor -e uno_r4_minima -d firmware/dc_motor

# クリーン
pio run -e uno_r4_minima -d firmware/dc_motor -t clean
```

**電磁弁基板（STM32F303K8）は PlatformIO ではなく CMake でビルドする。**

必要なツール:

| ツール | 版 | 備考 |
|---|---|---|
| `arm-none-eabi-gcc` | **11 以降** | CubeMX のリンカスクリプトが使う `READONLY` キーワードが GCC11 以降にしか無い。古い版では**コンパイルは全部通ってリンクだけが落ちる**（`STM32F303XX_FLASH.ld:106: non constant or forward reference address expression for section .ARM.extab`）。PlatformIO が renesas-ra 用に持っている gcc 7.2.1 では通らない |
| `cmake` | 3.22 以降 | `uv tool install cmake` で入る |
| `ninja` | — | `CMakePresets.json` の generator。`uv tool install ninja` で入る |

ツールチェーンは xPack 版が手軽（sudo 不要、tar を展開するだけ）:
<https://github.com/xpack-dev-tools/arm-none-eabi-gcc-xpack/releases>

**`Drivers/`（HAL と CMSIS）は生成物なのでコミットしていない。** CubeMX が無くても
`scripts/fetch_hal.sh` で取得できる —— ST の公式リポジトリから、STM32CubeF3 が
サブモジュールとして指している検証済みのコミットに固定して取る。

```bash
# 1. HAL / CMSIS を取得（初回のみ）
firmware/solenoid/scripts/fetch_hal.sh

# 2. ビルド
PATH="$HOME/.local/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin:$PATH" \
  cmake --preset Debug -S firmware/solenoid
PATH="$HOME/.local/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin:$PATH" \
  cmake --build firmware/solenoid/build/Debug
#   → Debug: FLASH 35.0% (22,964 B / 64KB) / RAM 19.6% (2,408 B / 12KB)
#   → Release: FLASH 21.1% (13,848 B)

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

**DC 用基板（UNO R4 Minima）の CAN ペリフェラルは `D4`(TX)/`D5`(RX) に固定されている。**
このピンを他用途へ割り当ててはならない。割り当てると CAN が上がらず**PC から止められない
基板**ができあがる。**サーボ用基板（Nano）は MCP2515 を SPI で外付けしているので D4/D5 は
サーボ出力に使えるが、代わりに `D11`/`D12`/`D13` を SPI が占有する**（`D13` は SCK なので
ステータス LED に使えず、RGB LED がその役目を担う）。

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

サーボ基板の DIP は A0〜A3 で UART とは重ならないため、`Serial` の使用に制約はない。
ただし **Flash 32KB / SRAM 2KB しかない**ので、容量が足りなくなったら
`config.h` の `ENABLE_SERIAL_DEBUG` を 0 にして落とす。

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
| **DC** ch0 / ch1 / ch2 | `0x80` / `0x81` / `0x82` | `conveyor` / `pump_vac` / `pump_blow` |
| **サーボ** SV0 – SV4 | `0x40` – `0x44` | `gripper` / `wall_f` / `wall_r` / `sub_gripper` / `origin_sensor` |
| **電磁弁** ch0 – ch5 | `0xC0` – `0xC5` | `valve_1` 〜 `valve_6` |

2 枚目は DIP=1 で DC が `0x88`〜、サーボが `0x48`〜、電磁弁が `0xC8`〜。

**範囲外は未設定（駆動拒否）へ倒す。** 黙って丸めると、DIP を回しすぎた基板が
別の基板の ID を名乗る。未設定にしておけば LED が速く点滅し、設定ミスがその場で
目に見える（DIP は 4bit だが基板番号は 3bit なので、8 以上は全スロット未設定）。

**`0xFF`（E_STOP ブロードキャストの予約）に着地する 1 個も未設定へ倒す。**
電磁弁基板の「基板番号 7 × スロット 7」だけがそこへ落ちる。ブロードキャストと同じ
デバイス ID を名乗る基板が居ると、そのスロット宛の `SET_TARGET` と全基板向けの
`E_STOP` がデバイス ID の上で区別できなくなる。潰れるのは 512 個中 1 個。

かつては「スロット表の基準 ID」「ブロックオフセット（刻み幅 = スロット数）」
「基板種別ごとの帯」「帯からのはみ出し判定」の 4 つの規則が重なっており、
`static_assert` も 3 つ必要だった。ビット分割にしたことで規則ごと消えた。

### servo — スロット表

サーボ基板は **5 本の信号線（SV0〜SV4）を持ち、どれもサーボ出力にもデジタル入力にもなる**
（仕様書 §7.1）。何を繋ぐかは配線で決まるので、ファームは `config.h` の
`kServoSlots[]` に**役割**（`Servo` / `TouchSensor` / `Unused`）を持つ。
**組み合わせは自由で、それぞれ何個でもよい** —— 「サーボ 2 + センサ 2 + 空き 1」も
「センサだけ」も成立する。`Unused` 以外のスロットはすべて CAN デバイスとして
`FEEDBACK` を送る。

| スロット | ピン | 役割 | デバイス ID | モータ |
|---|---|---|---|---|
| SV0 | D4 | `Servo` | `0x40` | `gripper`（メインハンド） |
| SV1 | D5 | `Servo` | `0x41` | `wall_f`（メインハンド） |
| SV2 | D6 | `Servo` | `0x42` | `wall_r`（メインハンド） |
| SV3 | D7 | `Servo` | `0x43` | `sub_gripper`（サブハンド） |
| SV4 | D8 | **`TouchSensor`** | `0x44` | `origin_sensor`（原点合わせ用） |

**デバイス ID は表に持たない。** スロットの添字がそのまま ID の下位 3bit になるので、
配線で役割を変えても ID は動かない。役割を変えるときはその行の `SlotRole` を
書き換えるだけでよい。

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

**吸気ポンプ・排気ポンプはこの基板ではなく DC 基板が動かす**（仕様書 §9.6）。
6ch はすべて弁で埋まっており、またポンプは起動電流が大きく `max_duty` で立ち上がりを
抑えられる DC 基板の側が適している。

`config.h` は STM32 HAL を include せず、ポートを自前の `Port` enum で持つ。
HAL の `GPIOA` / `GPIOB` はポインタへのキャストを含むマクロで `constexpr` 文脈に
持ち込めないため、取り込むと**ピンの重複検査がビルド時にできなくなる**。
`src/app.cpp` の `static_assert` が、ポートとピンの組で全ピン（CAN / UART / LED / DIP を
含む）の重複を検査し、あわせて CubeMX 生成の `main.h` と表が一致していることも見る。

## 電磁弁の動作は自動判定できない（重要）

**電磁弁基板は弁が実際に開いたかを観測する手段を持たない。** 圧力センサも
リミットスイッチも無く、分かるのは「指令どおり GPIO を駆動した」ことだけである。

そこで **`FEEDBACK` の到達フラグは常に 0 にする**（DC 基板と同じ扱い）。指令を出した
瞬間に到達を立てると、断線したソレノイドも抜けたコネクタも「到達」と報告され、
UI にもヘルス判定にも**測ったように見える到達**が流れ込む。

PC 側も到達を待たない。`on_off` 軸は `command_mode` が `position` ではないので
固定待ち（`settle_s`）へ落ちる。**動作確認（`motor_check`）からも除外**し
（`config/sub_hand.yaml` の `magnitude: 0`）、`config/checklist.yaml` の
`valves_actuate` で打音・目視確認する。

## サーボの到達フラグは推定値（重要）

**サーボは位置フィードバックを返さないため、`FEEDBACK` の到達フラグは実測ではなく
ファームの推定である**（仕様書 §7.3）。指令角と現在角の差をスルーレート（`slew_rate`）で
割った所要時間が経過し、補間が完了した時点で立てているだけで、
サーボが実際にそこへ行ったかは一切見ていない。

したがって **脱調・過負荷・メカ干渉で実際には動いていなくても「到達」と報告する。**
PC 側 `move_to` はこのフラグで次のステップへ進むため、**機構が引っかかっていても
シーケンスは進んでしまう。** 危険な動作には必ず `require_trigger` を付けて
人間の目視確認を挟むこと（`robots/main_hand.py` のハンド閉じ等）。

同じ理由で `FEEDBACK` の位置は「補間中の指令角」、速度は「そのときのスルーレートの
rpm 換算」である（仕様書 §7.4）。電流・温度と過電流・過熱はどちらの基板も測る手段を
持たないため、**プロトコルから外してある**（`FEEDBACK` の Byte4-6 と bit5-7 は予約）。

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

`HAS_RGB_LED` は dc_motor では既定 `1`。基板の D6 にシリアル RGB LED が 1 個載っており、
`platformio.ini` の `lib_deps` に `dmadison/FastLED NeoPixel` を入れてある
（FastLED がヘッダ走査で framework 同梱の I2S を巻き込みビルドを壊すので `lib_ignore = I2S` も要る）。
表示は **赤の速い点滅 = CAN 不通 / デバイス ID 未設定**、**橙 = 緊急停止ラッチ中**、
**緑のハートビート = 平常**。servo では既定 `0` のまま（実装は未着手）。

### servo

`firmware/servo/include/config.h` の `TODO(実機で確認)` はすべて仮置きの値。
表にはそれに加えて、仮値ではないが機構ごとに判断が要る既定値
（`initialAngleDeg` / `kEStopDetach`）も入れてある。

| 定数 | 仮の値 | 何を確認するか | 誤ったときのリスク |
|---|---|---|---|
| `kServoSlots[].limits.angleMinDeg` / `angleMaxDeg` | `0.0` / `30.0` | 機構を付けた状態で当たらない可動範囲を実測する | **広すぎるとサーボがメカストッパに当たったまま停動し、短時間で焼損する。最優先で確認**（狭すぎる分はクランプで止まるだけ） |
| `kServoSlots[].role` | サーボ 4 + センサ 1 | 実際に何を何本繋ぐか（個数に制約は無い） | 役割がずれると、指令した先と違うものが動く |
| `kServoSlots[].sensorActiveLow` | `true` | センサの極性。接触で導通して LOW になる想定 | 逆だと「触れていないのに触れている」と報告し続け、原点合わせが即座に終わる |
| `kServoPulse270` | `{500, 2400, 270.0}` | サーボのデータシートのパルス幅と可動角 | 指令角と実角がずれる。上端で当たり続ける |
| `kServoSlots[].pin` | `4` / `5` / `6` / `7` / `8` | 基板の信号線。**SPI(D11-13) / MCP2515(D3,D10) / RGB(D9) / DIP(A0-A3) と重ならないこと** | `main.cpp` の `static_assert` がビルド時に弾く |
| `kPinDip[4]` | `{14,15,16,17}`（A0–A3） | 基板の DIP がどのピンに落ちているか | オフセットが化けて別のアクチュエータが動く |
| `kServoSlots[].initialAngleDeg` | `0.0` | 電源投入時に持っていく角度 | 起動した瞬間に機構が動く |
| `kEStopDetach` | `false` | 緊急停止時に脱力させたい機構があるか | `true` にすると壁が自重で倒れ、把持中のワークを落とす |

**Arduino Nano は Flash 32KB / SRAM 2KB しかない。** ライブラリを足したらビルド時の
使用率を必ず見ること（現状は Flash 約 49% / RAM 約 40%）。RGB LED に FastLED を使うと
収まらないので Adafruit NeoPixel にしてある。足りなくなったら `ENABLE_SERIAL_DEBUG` を
0 にして落とす。

**MCP2515 を 16MHz 水晶で 1Mbps** はサンプルポイントの余裕が乏しい設定として
知られている。実機で通信エラーが出るなら、バス全体を 500kbps へ下げる判断が要る
（M3508・EDULITE 側も揃える必要がある）。

### solenoid

| 定数 | 現在値 | 確認すること |
|---|---|---|
| `kSolenoidChannels[].pin` | PB7 / PB6 / PB5 / PB4 / PB3 / PA15 | 回路図の `PUMP1_SW`〜`PUMP6_SW` と一致すること（`static_assert` が `main.h` と突き合わせる） |
| `kDipActiveLevel` | `0`（LOW = ON） | **DIP のコモンが GND へ落ちていること。** VCC 側へ引く配線なら極性と `solenoid.ioc` の `GPIO_PuPd` を揃えて反転する |
| DIP の `GPIO_PuPd` | `GPIO_PULLUP`（`solenoid.ioc`） | 外部プルアップが無い前提。**外すと読みが不定になり、電源投入のたびに違うデバイス ID を名乗る** |
| `kFirmwareVersion` | `1` | プロトコルかピン配置を変えたら上げる |

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
この組み合わせでは通信できない。現在は `Prescaler=2` / `BS1=10TQ` / `BS2=4TQ` =
15tq / **73.3%** にしてある（APB1 30MHz、tq = 66.7ns）。

**ビットレートが合っていてもサンプルポイントがずれていると 1 通も通らない。**
症状は `CAN_ESR` の `LEC=5`（Bit dominant error）—— ビットの立ち上がり途中、まだ前の
ビットの dominant が残っている領域をサンプルするために起きる。`candump` には何も
出ないので、そのままだと配線を疑って時間を使うことになる。`.ioc` を CubeMX で
再生成するときは、この 3 つの値が戻っていないか必ず確認すること。

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
| センサ入力 | 無し | **スロットに割り当て（現構成は SV4 / D8 の 1 個。個数自由）。1 個ずつ独立した CAN デバイスとして FEEDBACK のセンサ入力ビットで報告するだけ** | 仕様書 §5.2。判断は PC 側。接触は異常ではないのでヘルスにも動作確認にも影響させない |
| 受け付けるモード | **duty のみ**（他は無視） | **position のみ**（他は無視） | 仕様書 §4 / §7.2 |
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
- `FEEDBACK` の位置・速度が int16 で折り返さず飽和すること
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
