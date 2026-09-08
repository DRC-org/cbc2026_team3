// サーボ用自作モタドラの機体依存定数。
//
// **サーボ基板は 3 枚あり、MCU が 2 種類ある。**
//   - 基板 #0 / #1 … **Arduino Nano**（ATmega328P / 8bit / 5V / 16MHz）
//   - 基板 #2      … **Arduino UNO R4 Minima**（RA4M1 / 32bit）
// DC 用（UNO R4 Minima）ともサーボ Nano 版とも CAN の持ち方が違うので、ピン配置を
// 他の基板から類推してはならない。
//   - Nano   … CAN は MCP2515 を SPI で外付け（内蔵 CAN ペリフェラルは無い）。
//              D11/D12/D13 をハードウェア SPI が占有し、**D13 は SCK なので
//              オンボード LED はステータス表示に使えない**（RGB LED がその役目）
//   - R4     … CAN は内蔵ペリフェラルで D4(TX)/D5(RX) 固定。SPI は空くが、
//              その代わり **D4/D5 をサーボにもセンサにも使えない**
//
// **MCU ごとに 1 種類のバイナリを焼く**（§3.4 の版番号照合は「1 つの can_id 帯 =
// 1 つのファーム」を前提にしている）。プロジェクトを分けずに env だけを分けてあるので、
// **can_id 0x40 台に対応する kFirmwareVersion はこのファイルの 1 つだけ**である。
// Nano 用バイナリを R4 へ物理的に焼くことはできない（ツールチェインもブートローダも
// 別物）ので、「同じファームを全基板へ」を MCU 境界で 2 つに割っても焼き間違いの
// 事故は起きない —— 焼き忘れは従来どおり版番号の自己申告が拾う。
//
// 基板ごとに違うもの ——「そのスロットをサーボとして使うかスイッチとして使うか」
// 「挿さっているサーボが 270 度品か 180 度品か」—— は kServoBoards が基板番号ごとに持ち、
// 実行時に DIP で選ぶ。
//
// ここに集約してあるのは「基板を見ないと確定できない値」と「機構が決まるまで動かせない値」。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・サーボのデータシート・
// 実測と突き合わせること。可動範囲を誤ったまま通電するとサーボがメカストッパに当たったまま
// 停動し、短時間で焼損する（仕様書 §7.2）。
//
// パラメータの一部は SET_PARAM で実行時に変更できるが、RAM 上のみで電源断で
// ここの既定値に戻る（仕様書 §3.3）。恒久的に変えたい値はこのファイルを直すこと。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "ServoMotion.h"

// ===========================================================================
// ピン配置
// ===========================================================================

// **MCU で分ける。** ピンの意味（どこが CAN でどこが SPI か）が MCU ごとに違うので、
// 1 つの表に両方を並べると、片方のビルドの静的検査がもう片方のピンを誤検出する
// （R4 ビルドから見た Nano の D4/D5 は「CAN と衝突するサーボピン」に見え、
// Nano ビルドから見た R4 の D9 は「RGB と衝突するサーボピン」に見える）。

#if defined(ARDUINO_ARCH_RENESAS)

// ---------------------------------------------------------------------------
// 基板 #2: Arduino UNO R4 Minima
// ---------------------------------------------------------------------------

// CAN。UNO R4 Minima の CAN ペリフェラルは D4(TX) / D5(RX) に固定されており、
// Arduino_CAN の CAN インスタンスが variant の PIN_CAN0_TX / PIN_CAN0_RX を使う。
// **ここに写しを置かない** —— 正は variant のマクロで、写すと片方だけが古くなる。
// これらのピンを他用途へ割り当てると PC から止められない基板になるので、
// src/main.cpp の pinsAvoidCan() が PIN_CAN0_TX / PIN_CAN0_RX を直接見て衝突を
// ビルド時に検出する（DC 用と同じ扱い）。

// UART（D0=RX / D1=TX）。**ENABLE_SERIAL_DEBUG の値に依らず常に予約する。**
// R4 のデバッグ用シリアルは USB CDC なので D0/D1 を開けているように見えるが、
// この 2 本はハードウェア UART（Serial1）と同じピンで、サーボやスイッチを割り当てると
// Serial1 を開いた瞬間に叩き合う。予約を #if で出し分けると「シリアルを切ったときだけ
// 通る配線」ができ、しかもその構成でしか症状が出ない。
//
// もう 1 つの役割は**ゼロ埋めの捕獲**。kServoBoards は 1 行あたり kServoSlotCount 個を
// 並べる表で、要素を書き忘れた行はゼロ埋めされる。role は Unused（=0）で安全側へ倒れるが
// pin も 0 になるため、D0 を予約していないと 1 スロットぶんの書き忘れが衝突検査を
// 素通りする（2 つ以上ならスロット間のピン重複で落ちる）。
constexpr uint8_t kPinUartRx = 0;
constexpr uint8_t kPinUartTx = 1;

// シリアル RGB LED（1 個）。**Nano 版（D9）と違うのは、この基板では D9 がサーボ SV0 に
// 使われているため。** 基板が違えば配線も違うので、他の基板の値を写さないこと。
constexpr uint8_t kPinRgb = 8;
constexpr uint8_t kRgbBrightness = 30;

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
// A0〜A3 は R4 でも 14〜17。Arduino.h に依存しないよう数値で書いてある。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};
constexpr uint8_t kDipBitCount = 4;

#else

// ---------------------------------------------------------------------------
// 基板 #0 / #1: Arduino Nano
// ---------------------------------------------------------------------------

// MCP2515（CAN コントローラ）。INT は受信バッファが埋まっている間 LOW になる。
constexpr uint8_t kPinMcpInt = 3;
constexpr uint8_t kPinMcpCs = 10;

// ハードウェア SPI。コードからは直接使わないが、他用途へ割り当てると CAN が死ぬので
// 衝突検査の対象に入れてある（src/main.cpp の static_assert）。
constexpr uint8_t kPinSpiMosi = 11;
constexpr uint8_t kPinSpiMiso = 12;
constexpr uint8_t kPinSpiSck = 13;

// UART（D0=RX / D1=TX）。**ENABLE_SERIAL_DEBUG の値に依らず常に予約する。**
// 0 にしても Nano の D0/D1 は基板上の USB-シリアル変換に直結したままなので、
// そこへサーボやスイッチを割り当てると書き込み中に叩き合い、ブートローダの
// スケッチ受信も壊れる。予約を #if で出し分けると「シリアルを切ったときだけ
// 通る配線」ができ、しかもその構成でしか症状が出ない。
//
// もう 1 つの役割は**ゼロ埋めの捕獲**。kServoBoards は 1 行あたり kServoSlotCount 個を
// 並べる表で、要素を書き忘れた行はゼロ埋めされる。role は Unused（=0）で安全側へ倒れるが
// pin も 0 になるため、D0 を予約していないと 1 スロットぶんの書き忘れが衝突検査を
// 素通りする（2 つ以上ならスロット間のピン重複で落ちる）。
constexpr uint8_t kPinUartRx = 0;
constexpr uint8_t kPinUartTx = 1;

// シリアル RGB LED（1 個）。D13 が SPI の SCK に取られているため、
// 状態表示はこれが唯一の手段になる。
constexpr uint8_t kPinRgb = 9;
constexpr uint8_t kRgbBrightness = 30;

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
// A0〜A3 は Nano では 14〜17。Arduino.h に依存しないよう数値で書いてある。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};
constexpr uint8_t kDipBitCount = 4;

#endif  // ARDUINO_ARCH_RENESAS

// ===========================================================================
// スロット表（仕様書 §7.1）
// ===========================================================================

// **この基板は 5 本の信号線（SV0〜SV4）を持ち、どれもサーボにもセンサにもなる。**
// **サーボ・センサ・空きが何個ずつでも動く**（センサだけの基板も可）。
//
// これが成立するのは **1 スロット = 1 CAN デバイス**にしてあるからで、センサも自分の
// デバイス ID で FEEDBACK を送る。サーボのフレームに相乗りさせると、相乗り先の無い
// 「センサだけの基板」が成立せず、載せられる個数も予約ビットの数で頭打ちになる。
constexpr uint8_t kServoSlotCount = 5;

// **Unused を 0 にしてあるのは、書き忘れを安全側へ倒すため。** 下の kServoBoards は
// 1 行あたり kServoSlotCount 個を並べる表で、足りない要素はゼロ埋めされる。先頭が Servo だと
// 書き忘れたスロットが黙って「サーボとして駆動するピン」になり、そこにスイッチが
// 繋がっていれば通電したまま叩く。Unused なら ID を名乗らず駆動もしない側へ落ちる。
//
// **サーボの型（270 / 180）をここへ足してはならない。** 型は pulse が表す。
// Servo270 / Servo180 のような値を足すと isServoSlot() の判定が「どちらかである」に
// 変わり、その呼び出し側すべてが型を意識することになる。役割は「駆動するか、読むか、
// 使わないか」だけを表す軸に保つ。
enum class SlotRole : uint8_t {
    Unused,       // 何も繋がない。pinMode すら触らない
    Servo,        // サーボ出力。deviceId 宛の SET_TARGET で動く
    TouchSensor,  // デジタル入力。自分のデバイス ID で FEEDBACK を送り bit4 で報告するだけ
};

// **1 スロットぶんの全部。役割もパルス仕様もここに入っている。**
// 下の kServoBoards がこれを基板ごとに並べるので、「基板 #0 の SV0 は
// 270 度サーボ、SV1 は 180 度サーボ、SV2 はスイッチ」が基板ごとに独立して書ける。
//
// 実行時にしか確定しない DIP の値でこの表の行を選ぶので、初期角と可動範囲を使う
// ServoChannel は静的初期化子では作れない。**ServoChannel が begin() を持つのは
// そのため**で、src/main.cpp の setup() が行を選んでから初期化する。
struct ServoSlotConfig {
    // デバイス ID は表に持たない。**スロットの添字がそのままデバイス ID の下位 3bit**
    // になるので（仕様書 §2.2）、配線で役割を変えても ID は動かない。
    SlotRole role;
    uint8_t pin;
    float initialAngleDeg;         // 起動時に持っていく角度（仕様書 §5.4）
    motorcan::ServoLimits limits;  // 可動範囲とスルーレート（SET_PARAM 0x04-0x06 で変更可）
    motorcan::ServoPulseSpec pulse;
    // TouchSensor として使う基板があるとき、LOW を「入力あり」とみなすか。
    // 報告ビットは常に FEEDBACK のセンサ入力（自分のデバイス ID で送るので 1 つで足りる）。
    //
    // **極性はスロットごとの配線でしか決まらないので、1 スロットずつ実機で確かめる。**
    // 隣のスロットや別の基板の値へ合わせてはならない。逆に設定すると、零点確定
    // （lib/sequence/homing.py）が「触れている状態から一度離れて寄せ直す」段で
    // **どこまで動かしても OFF にならず**、_RELEASE_STEP_LIMIT に掛かって
    // HomingError で止まる（原点は確定できないまま動作確認が失敗する）。
    bool sensorActiveLow;
    // **表示名は持たない。** かつて `const char *name` があり「シリアルデバッグ表示用」
    // と書いてあったが、どの pollSerial() も一度も表示しなかった。読まれない文字列は
    // Nano では SRAM と Flash を 50 バイトずつ食い（2KB のうち 2.4%）、しかも
    // PC 側 yaml のモータ名と静かにずれても誰も気付けない。対応は下の表の行コメントが持つ。
};

// TODO(実機で確認): 角度 → パルス幅の対応。サンプルの attach(pin, 500, 2400) に合わせてある。
// **サンプルのように 180/270 を掛けて write() の 0-180 に押し込む変換はしない。**
// 分解能が 2/3 に落ち、可動範囲の端が表現できなくなるため（ServoMotion.h 参照）。
// ファームは Servo::writeMicroseconds() でパルス幅を直接指令する。
//
// **サーボの型（180 / 270）を知っているのはここだけ**である（仕様書 §7.7）。PC 側の
// yaml も CAN も出力軸の絶対角 [deg] しか扱わないので、型を載せ替えても
// config/<robot>_positions.yaml は 1 行も変わらない。直すのは下の 1 行と、
// それを使う kServoBoards の行だけ。
//
// **3 値セットで直すこと。** angleRangeDeg だけ 180 にして minUs/maxUs を 270 度用の
// まま残すと、載せ替えた意味がないままずれ方だけが変わる。実物とファームが食い違っても
// 指令どおり動いたようにしか見えないので（FEEDBACK が返すのはクランプ後の指令角）、
// 気付く手段は INFO の自己申告と PC 側 expected_angle_range_deg の照合しかない。
constexpr motorcan::ServoPulseSpec kServoPulse270{500, 2400, 270.0f};

// TODO(実機で確認): 180 度サーボを挿すときのパルス幅。**下の値は仮置きである。**
// 180 度品のパルス幅は 500-2400 とは限らず、1000-2000 や 500-2500 も普通にある。
// データシートを見て 3 値とも実物へ合わせ、kServoBoards のその行を差し替えること。
constexpr motorcan::ServoPulseSpec kServoPulse180{500, 2400, 180.0f};

// TODO(実機で確認): angle_min / angle_max は機構が付いた状態で「当たらない範囲」を
// 実測して入れること。**「機構確定後に広げる」という当初の方針どおりにはならず**、
// wall_f を 180deg 動かす必要（8e34d10）で機構確定前に 270 度まで広げてある。
// 現状 config/main_hand_positions.yaml が使うのは
// gripper 0〜65deg・wall_f 90〜270deg・wall_r 90〜180deg。
// **サブハンドの 5 軸（sub_rotate_r/l・sub_pitch_r/l・sub_offset）が使う角度はまだ
// 決まっていない**（機構が付いていないので位置定数そのものが仮値）。決まったら
// その軸の定数を実可動域へ狭めること。
// **狭すぎる分にはクランプで止まるだけだが、広すぎるとメカストッパに当たったまま
// 停動して焼損する。**
//
// !!! 現在この値は 0〜270deg = サーボの全可動域で、クランプは実質効いていない !!!
// かつては「positions が微小ストロークしか使わない」ことに合わせた安全側の仮値
// (0〜30deg) だったが、位置定数が実運用値へ更新されたときにここを全域へ開けた。
// **機構に当たる角度を送っても止まらない状態である。** 実可動域が決まったら必ず
// 狭めること (棚卸しは docs/mechanism_handoff.md §0)。
//
// スルーレート (第 3 要素 90deg/s) は移動時間をそのまま決める。位置定数の
// `timeout_s` はこの値から逆算した値でなければならず、対応は
// `tests/test_servo_travel_budget.py` が突き合わせている。
//
// **駆動する Servo スロット（gripper / wall_f / wall_r / sub_rotate_r / sub_rotate_l /
// sub_pitch_r / sub_pitch_l / sub_offset）は 1 本ずつ独立した定数を持つ。** 1 つを共有すると、
// 片方の可動範囲を機構に合わせて広げただけで無関係なスロットのクランプまで一緒に緩む
// （実際に上記の wall_f 用の変更で gripper のクランプが外れていた）。値はまだ全スロット
// 同じ仮値のまま、実測はスロットごとに行う。
//
// **左右ペア（sub_rotate_r/l・sub_pitch_r/l）も 1 本ずつ持つ。** ペアは PC 側で 1 論理軸へ
// 束ねるが、逆回転側は scale: -1.0 / offset: 270.0 で絶対角を折り返すので、同じ論理位置でも
// ファームへ届く角度は左右で違う。共有すると片側の実測で相方のクランプが動く。
constexpr motorcan::ServoLimits kGripperLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kWallFLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kWallRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubRotateRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubRotateLLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubPitchRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubPitchLLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubOffsetLimits{0.0f, 270.0f, 90.0f};

// TouchSensor / Unused は駆動しないので共有のままでよい。
constexpr motorcan::ServoLimits kProvisionalLimits{0.0f, 270.0f, 90.0f};

// ===========================================================================
// スロット設定（基板番号ごと）
// ===========================================================================

// **1 つのバイナリが複数の基板を担うので、スロットの中身は基板番号（DIP）ごとに分ける。**
// 表を 1 つだけ持たせていた頃は、基板 #0 の SV3 をスイッチにすると基板 #1 の SV3 も
// 道連れになり、サーボの型も全基板・全スロットで一律だった（1 枚だけ別のファームを
// 焼くのは §3.4 の焼き忘れ検出を無力化するので採らない）。
//
// **基板番号は行の添字ではなく boardNumber として明示的に持つ。** MCU が違うと
// ピンの意味も固定ピンも違うので、3 枚ぶんを 1 つの表に並べると、各ビルドの静的検査が
// 自分の担当でない行まで見ることになる —— R4 ビルドから見た Nano 行の D4/D5 は
// 「CAN(D4/D5) と衝突するサーボピン」に、Nano ビルドから見た R4 行の D9 は
// 「RGB(D9) と衝突するサーボピン」に見え、どちらも実在しない衝突で落ちる。
// 添字と基板番号の暗黙の結合を解けば、各バイナリが自分の担当基板の行だけを持てる。
//
// **ピンは MCU ごとに違う。** Nano 版（#0/#1）は D4〜D8 で揃えてあるが、R4 版（#2）は
// D4/D5 が CAN に取られているので別の 5 本を使う。
//
//   基板  | SV0 | SV1 | SV2 | SV3 | SV4
//   ------+-----+-----+-----+-----+-----
//   #0/#1 | D4  | D5  | D6  | D7  | D8
//   #2    | D9  | D11 | D10 | D6  | D3
//
// **デバイス ID は動かさないこと** — スロットに固定しておくと、配線を差し替えても
// PC 側 yaml の can_id が無変更で済む。デバイス ID は基板番号から決まる（仕様書 §2.2。
// FEEDBACK の CAN ID は 0x300 + デバイス ID なので candump からそのまま読める）。
// PC 側 yaml と一致していることが唯一の接点で、照合する仕組みは無い。ずれるとその
// モータは指令を受け取らず FEEDBACK も来ない（PC からは STALE に見える）。
//
//   基板 | スロット | デバイス ID | PC 側のモータ / 用途
//   -----+----------+------------+--------------------------------
//    #0  | SV0      | 0x40       | gripper                  (メインハンド)
//    #0  | SV1      | 0x41       | wall_f                   (メインハンド)
//    #0  | SV2      | 0x42       | wall_r                   (メインハンド)
//    #0  | SV3      | 0x43       | rotate の原点スイッチ     (メインハンド)
//    #0  | SV4      | 0x44       | y_axis 右の原点スイッチ   (メインハンド)
//    #1  | SV0      | 0x48       | y_axis 左の原点スイッチ   (メインハンド)
//    #1  | SV1      | 0x49       | sub_y_axis 前端スイッチ   (サブハンド)
//    #1  | SV2      | 0x4A       | sub_y_axis 後端スイッチ   (サブハンド)
//    #1  | SV3      | 0x4B       | sub_lift 上端スイッチ     (サブハンド)
//    #1  | SV4      | 0x4C       | sub_lift 下端スイッチ     (サブハンド)
//    #2  | SV0      | 0x50       | sub_rotate_r             (サブハンド)
//    #2  | SV1      | 0x51       | sub_rotate_l             (サブハンド)
//    #2  | SV2      | 0x52       | sub_pitch_r              (サブハンド)
//    #2  | SV3      | 0x53       | sub_pitch_l              (サブハンド)
//    #2  | SV4      | 0x54       | sub_offset               (サブハンド)
//
// **基板 #1 は 2 つのロボットにまたがる。** SV0 がメインハンドの y_axis 左スイッチで、
// SV1〜SV4 がサブハンドのスイッチである。CAN 上はどちらも共有バス can_generic に載るので
// 成立するが、**配線は物理的にメインハンドから基板 #1 まで引く必要がある** ——
// 基板は「どのロボットのものか」ではなく「どのバスに繋がっているか」でしか区切られていない。
// 引き回しを嫌って y_axis 左を基板 #0 の空きへ移すことはできない（#0 は 5 スロットとも埋まっている）。
//
// **Unused 以外のスロットはすべて CAN デバイスとして FEEDBACK を送る。**
// センサは PC 側 yaml の sensors: へ登録すること（登録しないと受信ループが
// そのフレームを誰にも配らない）。途絶は STALE として検出される。

// **1 行 = 1 基板。boardNumber は DIP で選ばれる番号そのもの**（仕様書 §2.2 の bit5-3）。
// 添字ではなく値で持つので、Nano ビルドが {0,1}、R4 ビルドが {2} だけを並べられる。
// 重複と 3bit からのはみ出しは src/main.cpp の static_assert が捕まえる
// （重複すると線形探索で先に見つかった行が黙って勝ち、はみ出した行は永久に選ばれない）。
struct ServoBoardConfig {
    uint8_t boardNumber;
    ServoSlotConfig slots[kServoSlotCount];
};

// **表に無い基板番号は全スロット Unused として扱う**（判断は src/main.cpp）。
// 黙って先頭の行を使うと、DIP を回しすぎた基板が別の基板の役割とデバイス ID を
// 名乗り、同じ ID の 2 ノードが違うデータを送ってバスがエラーフレームで埋まる。
// 全 Unused なら resolveDeviceIds が ID を付けないので、既存の「デバイス ID 未設定 →
// LED 赤の速い点滅・駆動拒否」へそのまま乗る（DIP 8 以上の扱いと同じ思想）。
//
// **行数を明示しないのは、書き忘れを static_assert で捕まえるため**（src/main.cpp）。
// [kServoBoardCount] と書くと足りない行がゼロ埋めで通ってしまう。
//
// **型を混在させるときは pulse を差し替える**（kServoPulse270 / kServoPulse180）。
// パルス仕様は minUs / maxUs / angleRangeDeg の 3 値セットで、1 つだけ実物へ合わせても
// 残り 2 つが古いままだとずれ方が変わるだけで、CAN 越しには指令どおり動いたようにしか
// 見えない（気付く手段は INFO の自己申告と PC 側 expected_angle_range_deg の照合だけ）。

#if defined(ARDUINO_ARCH_RENESAS)

// R4 ビルドが担うのは基板 #2 の 1 枚だけ。
constexpr uint8_t kServoBoardCount = 1;

constexpr ServoBoardConfig kServoBoards[] = {
    // 基板 #2（DIP=2）: サブハンドの回転 2 軸 / ピッチ 2 軸 / オフセット 1 軸
    //
    // **5 スロットとも 270 度サーボ。** サブハンドで角度を持つ機構がちょうど 5 つあり、
    // 1 枚で足りる（スイッチ側は基板 #1 が担う）。
    //
    // **sub_rotate_r / sub_rotate_l と sub_pitch_r / sub_pitch_l は機構的に直結した
    // 左右ペア**で、PC 側の位置定数 yaml で 1 論理軸へ束ね、逆回転側は scale: -1.0 /
    // offset: 270.0 で絶対角を折り返す。**ファーム側は 1 スロット = 1 デバイスのままで
    // ペアを知らない** —— ペアを知る層を増やすと、左右で違う角度を送るという正常な状態を
    // 「食い違い」と読む判定がここにも生まれ、しかも PC 側の折り返しと二重になる。
    // ここに残しておくのは、片方のスロットだけを別のピンや別の基板へ移すと機構が
    // その場で壊れる、という配線上の制約のため。
    //
    // **Servo スロットは通電した瞬間に initialAngleDeg へ駆動する**（setup() が attach して
    // 初期角を出す。仕様書 §5.4）。Unused のスロットは attach すらしないので、この基板が
    // 通電で動くようになるのはここが Servo になったときからである。
    // **下の 0.0f は仮値なので、この値のまま機構を付けてはならない** —— 通電のたび、
    // そして基板が瞬断で再起動するたびに 0deg へ飛ぶ（再起動は FEEDBACK の
    // 「起動後未受信」ビットとして PC 側に出るが、飛んだ後にしか出ない）。
    //
    // TODO(実機で確認): initialAngleDeg は 5 スロットとも仮値。機構を付ける前に、
    // 「そこへ飛んでも当たらない角度」を実測して入れること。limits も全域（0〜270deg）の
    // ままなので、実可動域が決まったら kSubRotate*Limits / kSubPitch*Limits /
    // kSubOffsetLimits を狭めること。
    {2,
     {
         {SlotRole::Servo, 9, 0.0f, kSubRotateRLimits, kServoPulse270, true},   // SV0 sub_rotate_r
         {SlotRole::Servo, 11, 0.0f, kSubRotateLLimits, kServoPulse270, true},  // SV1 sub_rotate_l
         {SlotRole::Servo, 10, 0.0f, kSubPitchRLimits, kServoPulse270, true},   // SV2 sub_pitch_r
         {SlotRole::Servo, 6, 0.0f, kSubPitchLLimits, kServoPulse270, true},    // SV3 sub_pitch_l
         {SlotRole::Servo, 3, 0.0f, kSubOffsetLimits, kServoPulse270, true},    // SV4 sub_offset
     }},
};

#else

// Nano ビルドが担うのは基板 #0 / #1 の 2 枚。
constexpr uint8_t kServoBoardCount = 2;

constexpr ServoBoardConfig kServoBoards[] = {
    // 基板 #0（DIP=0）: メインハンド
    {0,
     {
         {SlotRole::Servo, 4, 0.0f, kGripperLimits, kServoPulse270, false},  // SV0 gripper
         {SlotRole::Servo, 5, 270.0f, kWallFLimits, kServoPulse270, false},  // SV1 wall_f
         {SlotRole::Servo, 6, 90.0f, kWallRLimits, kServoPulse270, false},  // SV2 wall_r
         // **実機で確認済み**（CAN ID 0x343 の FEEDBACK を実測）: 非接触で LOW、
         // 接触で HIGH。したがって sensorActiveLow は false。
         {SlotRole::TouchSensor, 7, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV3 rotate
         // SV4 は y_axis の**右**のリミットスイッチ。左は基板 #1 の SV0（0x48）で、
         // 1 本の直結ペア軸のスイッチが 2 枚の基板にまたがる（基板 #0 に空きが無いため）。
         //
         // TODO(実機で確認): sensorActiveLow は仮値。**同じ基板の SV3 が false だからと
         // いって合わせてはならない** —— 極性はスロットごとの配線で決まる。FEEDBACK の
         // bit4 を非接触・接触の両方で実測して確定すること。
         {SlotRole::TouchSensor, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 y_axis 右
     }},
    // 基板 #1（DIP=1）: **5 スロットとも TouchSensor で、駆動するモータは 1 台も無い。**
    // SV0 だけがメインハンド（y_axis 左）で、SV1〜SV4 はサブハンドなので、
    // **この 1 枚が 2 つのロボットにまたがる**（上のスロット表を参照。CAN は共有バス
    // can_generic なので成立するが、配線はメインハンドからここまで引く必要がある）。
    //
    // **駆動するモータが無いので、この基板の焼き忘れ検出は motors: 側では働かない。**
    // §3.4 の版番号照合は expected_firmware を書いた対象としか突き合わせないため、
    // センサ側（PC 側 sensors: の expected_firmware）が担う。そこが無いと、この基板だけ
    // 古いファームのまま**全スロットが「反応しないスイッチ」として現れ**、症状は配線不良と
    // 区別が付かない。
    //
    // TODO(実機で確認): 5 スロットとも sensorActiveLow は仮値。**スロットごとに**
    // 実測して確定すること（基板 #0 の SV3 の実測値は**この基板の配線を何も保証しない**）。
    {1,
     {
         {SlotRole::TouchSensor, 4, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV0 y_axis 左 (メイン)
         {SlotRole::TouchSensor, 5, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV1 sub_y_axis 前端
         {SlotRole::TouchSensor, 6, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV2 sub_y_axis 後端
         {SlotRole::TouchSensor, 7, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV3 sub_lift 上端
         {SlotRole::TouchSensor, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 sub_lift 下端
     }},
};

#endif  // ARDUINO_ARCH_RENESAS

// ===========================================================================
// デバイス ID（仕様書 §2.2）
// ===========================================================================

// デバイス ID は「基板種別 | 基板番号 | スロット番号」の固定ビット分割。
// **帯も刻み幅も連続ブロック性も要らない。** DIP は基板番号そのもので、
// スロットの添字がそのまま ID の下位 3bit になる。
//
//   基板番号 | SV0  | SV1  | SV2  | SV3  | SV4
//   ---------+------+------+------+------+------
//      0     | 0x40 | 0x41 | 0x42 | 0x43 | 0x44
//      1     | 0x48 | 0x49 | 0x4A | 0x4B | 0x4C
//      2     | 0x50 | 0x51 | 0x52 | 0x53 | 0x54
//
// **MCU が違っても ID の決まり方は同じ。** 基板 #2 は UNO R4 Minima だが、
// 種別 2bit はサーボ基板のままなので 0x40 台の続きに並ぶ（PC 側から見て「どの MCU か」は
// 区別する必要も手段も無い）。
//
// candump に 0x4A が流れていれば「サーボ基板 1 枚目の SV2」と直接読める。
// DIP は 4bit だが基板番号は 3bit なので、8 以上を設定した基板は全スロットが
// 未設定になる（LED 赤点滅・駆動拒否）。黙って丸めると別の基板の ID を名乗る。
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Servo;

// 焼き忘れた基板をセッティングタイムに見つけるための版番号（仕様書 §3.4）。
// **プロトコルかピン配置を変えたら必ず上げること。**
//
// 2: INFO にサーボ可動レンジ（Byte3-4）を追加。v1 は DLC=3 のままなので、PC 側は
//    「レンジ不明」として扱い、期待値が書かれていれば不一致（＝焼き忘れ）と判定する。
// 3: デバイス ID 未設定のスロットが FEEDBACK / INFO を 1 通も送らなくなった（§2.2）。
//    v2 までは CAN ID 0x300（デバイス ID 0x00）で送っていたが、PC 側は can_id を
//    0x01〜0xFE に限るので**そのフレームを claim できるドライバが存在せず**、
//    「デバイス ID 未設定」の報告経路は構造的に死んでいた。しかも複数の基板が同時に
//    未設定だと、異なるノードが同じ ID で異なるデータを送ってバスがエラーフレームで
//    埋まる。設定ミスの通知は LED（赤の速い点滅）が担う。
// 4: スロットの役割を基板番号ごとに持つようにした（kServoBoards が持つ）。
//    基板 #0 の SV3 が Servo（sub_gripper）から TouchSensor（rotate の原点スイッチ）に
//    なり、sub_gripper は基板 #1 の SV0（0x48）へ移った。**焼き忘れた基板は
//    「サーボのつもりのピンが入力のまま」または逆になる**ので、版番号での検出が要る。
// 5: 基板 #0 の SV4（y_axis の原点スイッチ用）が TouchSensor から Unused になった。
//    **スイッチが未装着なので暫定であり、付けたら TouchSensor へ戻して版番号をまた上げる。**
//    デバイス ID 0x44 が FEEDBACK を送るかどうかがバイナリで変わるため、上げないと
//    「SV4 が Unused のファーム」と「SV4 が TouchSensor のファーム」が同じ v4 を名乗り、
//    どちらが焼かれているのかを INFO の照合で切り分けられなくなる（版番号は「バイナリの
//    区別が付く」ことだけが存在理由なので、CAN 上の振る舞いが変わったら必ず上げる）。
// 6: 零点確定用のスイッチ 6 本とサブハンドのサーボ 5 本を割り当て、**3 枚とも役割が
//    変わった**。基板 #0 の SV4 は Unused から TouchSensor（y_axis 右）へ戻り、基板 #1 は
//    sub_gripper の 1 台から 5 スロットとも TouchSensor へ、基板 #2 は全 Unused から
//    5 スロットとも Servo になった。焼き忘れた基板は**サーボのつもりのピンが入力のまま**
//    （逆も同じ）で、しかも 0x44 / 0x49〜0x4C / 0x50〜0x54 が FEEDBACK を送るかどうかが
//    バイナリで変わるので、上げないと新旧が同じ v5 を名乗って INFO の照合で切り分けられない。
//
// **基板 #2（UNO R4 Minima）を足したときは上げていない。これは上げ忘れではない。**
// 版番号が区別したいのは「CAN 上の振る舞い」であって、基板 #0 / #1 のそれは 1 ビットも
// 変わっていない（新しい行は基板番号 2 にしか当たらず、既存 2 枚のスロット表・
// デバイス ID・フレームはすべて同じ）。上げると意味の無い Nano 2 枚の再書き込みと
// config/**/*.yaml の expected_firmware 26 箇所の更新が発生し、揃え損ねた 1 箇所が
// 「正しく焼いたのに FAULT」として返ってくる。**R4 バイナリは Nano と同じ番号を名乗る** ——
// 版番号は MCU ではなく振る舞いを指すので、MCU が増えただけでは分岐させない
// （逆に、上の v6 のように振る舞いが変わるときは 3 枚とも同じ番号で上がる）。
//
// **上げたら config/<robot>.yaml の expected_firmware も揃えること**（仕様書 §3.4）。
// PC 側は INFO の申告値と突き合わせ、食い違ったらそのモータを FAULT にする ——
// これは焼き忘れを見つけるための仕掛けなので、揃え忘れると「正しく焼いたのに
// 全部 FAULT」になる。表示される不一致メッセージに期待値と申告値の両方が出る。
constexpr uint8_t kFirmwareVersion = 6;

// INFO（版番号の自己申告）の送信周期。1Hz なら 8 デバイスでもバス負荷は無視できる。
constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// 制御ループ
// ===========================================================================

// 補間の更新周期。サーボ自身が内部でパルス幅へ追従するので、速くする意味は薄い。
// Servo ライブラリのフレーム周期（20ms）より速く、FEEDBACK 周期（100Hz）と同等。
// **ATmega328P は float がソフトウェア実装**なので、ここを詰めすぎると CAN 受信が痩せる。
constexpr uint32_t kMotionIntervalMs = 5;

// コマンドウォッチドッグ（仕様書 §5.1）。**宛先がデバイス ID ＝ スロットなので、
// ウォッチドッグもスロットごとに独立して動く。** 1 チャンネルへの指令が途絶えても
// 他のチャンネルは動き続ける（片方の壁だけ通信が切れる、という状況が実在するため）。
//
// PC 側は最後に指令した角度を kDefaultCommandTimeoutMs 以内に再送し続ける契約なので、
// 満了は PC の停止かケーブル断を意味する。**サーボは満了しても現在角を保持するので
// 機構が落ちることはない**が、そこから先は動かせない。
//
// 0 にすると途絶しても新しい角度指令を受け付け続け、FEEDBACK の bit2 も報告しなくなる。
// 手で cansend を打つようなベンチ確認のための逃げ道であって、試合では既定の 1 のまま
// 使う。再送が間に合わない状態は運用上の異常なので、ここや command_timeout_ms を
// 触って覆い隠してはならない（仕様書 §8）。
//
// この値は setup() が MotorSafety::setWatchdogEnabled() へ写す。判定を #if で
// main.cpp 側に置くと、同じ分岐を両ファームが各自で持つことになり、片方に入れ忘れても
// 誰も気付けない。有効/無効の判定は MotorSafety にだけある。
#define WATCHDOG_ENABLED 1

// command_timeout_ms / feedback_interval_ms（仕様書 §3.3 の既定値）は PC 側との契約なので
// MotorCanProtocol.h の kDefaultCommandTimeoutMs / kDefaultFeedbackIntervalMs が持つ。
// 到達許容差の既定値（§7.3 / §7.6 の 0）は ServoMotion.h の
// kDefaultServoReachedToleranceDeg が持ち、ServoMotion が自分で適用する。

// ===========================================================================
// 緊急停止・ウォッチドッグ時の振る舞い（仕様書 §7.5）
// ===========================================================================

// true にすると緊急停止・ウォッチドッグ満了で PWM を止めて（detach して）サーボを脱力させる。
//
// **既定は false（現在角を保持）。** サーボは PWM を止めると back-drivable になり、
// 壁が自重で倒れ、グリッパが把持中のワークを落とす。DC 用の「PWM 0%」と
// 意図的に振る舞いを変えている点であり、変更するときは機構側の影響を必ず確認すること。
constexpr bool kEStopDetach = false;

// ===========================================================================
// 表示
// ===========================================================================

// D13 が SPI の SCK に取られているため、状態表示は RGB LED だけが担う。
// 0 にすると状態を知る手段が丸ごと無くなる（現場で切り分けができない）。
#define HAS_RGB_LED 1

// デバイス ID が未設定のスロットがあるとき、および CAN が上がらなかったときの速い点滅
// （仕様書 §2.2 / §7.1）。
constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

// 正常時のハートビート点滅周期。ファームが生きていることを目視で確認するため。
constexpr uint32_t kHeartbeatIntervalMs = 1000;

// CAN 送信が連続して失敗した回数がこれを超えたら「今すぐ直さないと使えない」表示へ倒す。
// FEEDBACK は 5 スロット × 100Hz = 500 通/秒 出るので、50 連続失敗は約 100ms 分の
// 全滅に相当する。1 通の取りこぼし（調停負けや一過性の TX 詰まり）で赤くしないための下限。
constexpr uint16_t kCanTxFailStreakAlarm = 50;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB シリアル（115200 baud）から「<スロット番号> <角度>」で角度を直接指令できる（0 で無効）。
// DIP は A0〜A3 なので、DC 用と違って UART との兼用による制約は無い。
// 緊急停止ラッチ中はシリアルからも駆動できない（ServoChannel が指令を拒否する）。
//
// **Flash 32KB / SRAM 2KB しかないので、容量が足りなくなったらここを 0 にする。**
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
