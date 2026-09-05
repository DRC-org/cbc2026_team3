// サーボ用自作モタドラの機体依存定数。
//
// 基板は **Arduino Nano**（ATmega328P / 8bit / 5V / 16MHz）。DC 用の UNO R4 Minima とは
// MCU も CAN の持ち方も違うので、ピン配置を DC 用から類推してはならない。
//   - CAN は MCP2515 を SPI で外付け（内蔵 CAN ペリフェラルは無い）
//   - D11/D12/D13 をハードウェア SPI が占有する
//   - **D13 は SCK。オンボード LED はステータス表示に使えない**（RGB LED がその役目）
//
// **同じファームを全基板へ焼く**（§3.4 の版番号照合は 1 種類のバイナリを前提にしている）。
// そのため基板ごとに違うもの ——「そのスロットをサーボとして使うかスイッチとして使うか」
// 「挿さっているサーボが 270 度品か 180 度品か」—— は kSlotsByBoard が基板番号ごとに持ち、
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
// もう 1 つの役割は**ゼロ埋めの捕獲**。kSlotsByBoard は行あたり kServoSlotCount 個を
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

// **Unused を 0 にしてあるのは、書き忘れを安全側へ倒すため。** 下の kSlotsByBoard は
// 行あたり kServoSlotCount 個を並べる表で、足りない要素はゼロ埋めされる。先頭が Servo だと
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
// 下の kSlotsByBoard が [基板番号][スロット] でこれを並べるので、「基板 #0 の SV0 は
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
// それを使う kSlotsByBoard の行だけ。
//
// **3 値セットで直すこと。** angleRangeDeg だけ 180 にして minUs/maxUs を 270 度用の
// まま残すと、載せ替えた意味がないままずれ方だけが変わる。実物とファームが食い違っても
// 指令どおり動いたようにしか見えないので（FEEDBACK が返すのはクランプ後の指令角）、
// 気付く手段は INFO の自己申告と PC 側 expected_angle_range_deg の照合しかない。
constexpr motorcan::ServoPulseSpec kServoPulse270{500, 2400, 270.0f};

// TODO(実機で確認): 180 度サーボを挿すときのパルス幅。**下の値は仮置きである。**
// 180 度品のパルス幅は 500-2400 とは限らず、1000-2000 や 500-2500 も普通にある。
// データシートを見て 3 値とも実物へ合わせ、kSlotsByBoard のその行を差し替えること。
constexpr motorcan::ServoPulseSpec kServoPulse180{500, 2400, 180.0f};

// TODO(実機で確認): angle_min / angle_max は機構が付いた状態で「当たらない範囲」を
// 実測して入れること。現状は config/main_hand_positions.yaml が 0〜6deg の微小ストロークしか
// 使わないのに合わせた安全側の仮値で、広げるのは機構確定後。**狭すぎる分にはクランプで
// 止まるだけだが、広すぎるとメカストッパに当たったまま停動して焼損する。**
constexpr motorcan::ServoLimits kProvisionalLimits{0.0f, 30.0f, 90.0f};

// ===========================================================================
// スロット設定（基板番号ごと）
// ===========================================================================

// **同じファームを全基板へ焼くので、スロットの中身は基板番号（DIP）ごとにここで分ける。**
// 表を 1 つだけ持たせていた頃は、基板 #0 の SV3 をスイッチにすると基板 #1 の SV3 も
// 道連れになり、サーボの型も全基板・全スロットで一律だった（1 枚だけ別のファームを
// 焼くのは §3.4 の焼き忘れ検出を無力化するので採らない）。
//
// **ピンは全基板とも D4〜D8。** 5 スロットとも同じ基板を同じパターンで焼くので、
// ここを基板ごとに変えるのは基板を作り直したときだけである。
//
//   スロット | ピン | デバイス ID の下位 3bit
//   ---------+------+------------------------
//   SV0      | D4   | 0
//   SV1      | D5   | 1
//   SV2      | D6   | 2
//   SV3      | D7   | 3
//   SV4      | D8   | 4
//
// **デバイス ID は動かさないこと** — スロットに固定しておくと、配線を差し替えても
// PC 側 yaml の can_id が無変更で済む。デバイス ID は基板番号から決まる（仕様書 §2.2。
// FEEDBACK の CAN ID は 0x300 + デバイス ID なので candump からそのまま読める）。
// PC 側 yaml と一致していることが唯一の接点で、照合する仕組みは無い。ずれるとその
// モータは指令を受け取らず FEEDBACK も来ない（PC からは STALE に見える）。
//
//   基板 | スロット | デバイス ID | PC 側のモータ / 用途
//   -----+----------+------------+--------------------------------
//    #0  | SV0      | 0x40       | gripper        (メインハンド)
//    #0  | SV1      | 0x41       | wall_f         (メインハンド)
//    #0  | SV2      | 0x42       | wall_r         (メインハンド)
//    #0  | SV3      | 0x43       | rotate の原点スイッチ
//    #0  | SV4      | ―          | 未使用 (y_axis の原点スイッチ用に予約)
//    #1  | SV0      | 0x48       | sub_gripper    (サブハンド)
//    #1  | SV1〜SV4 | ―          | 未使用
//
// **Unused 以外のスロットはすべて CAN デバイスとして FEEDBACK を送る。**
// センサは PC 側 yaml の sensors: へ登録すること（登録しないと受信ループが
// そのフレームを誰にも配らない）。途絶は STALE として検出される。
constexpr uint8_t kServoBoardCount = 2;

// **表に無い基板番号は全スロット Unused として扱う**（判断は src/main.cpp）。
// 黙って基板 #0 の行を使うと、DIP を回しすぎた基板が別の基板の役割とデバイス ID を
// 名乗り、同じ ID の 2 ノードが違うデータを送ってバスがエラーフレームで埋まる。
// 全 Unused なら resolveDeviceIds が ID を付けないので、既存の「デバイス ID 未設定 →
// LED 赤の速い点滅・駆動拒否」へそのまま乗る（DIP 8 以上の扱いと同じ思想）。
//
// **行数を明示しないのは、書き忘れを static_assert で捕まえるため**（src/main.cpp）。
// [kServoBoardCount][...] と書くと足りない行がゼロ埋めで通ってしまう。
//
// **型を混在させるときは pulse を差し替える**（kServoPulse270 / kServoPulse180）。
// パルス仕様は minUs / maxUs / angleRangeDeg の 3 値セットで、1 つだけ実物へ合わせても
// 残り 2 つが古いままだとずれ方が変わるだけで、CAN 越しには指令どおり動いたようにしか
// 見えない（気付く手段は INFO の自己申告と PC 側 expected_angle_range_deg の照合だけ）。
constexpr ServoSlotConfig kSlotsByBoard[][kServoSlotCount] = {
    // 基板 #0（DIP=0）: メインハンド
    {
        {SlotRole::Servo, 4, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV0 gripper
        {SlotRole::Servo, 5, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV1 wall_f
        {SlotRole::Servo, 6, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV2 wall_r
        // **実機で確認済み**（CAN ID 0x343 の FEEDBACK を実測）: 非接触で LOW、
        // 接触で HIGH。したがって sensorActiveLow は false。
        {SlotRole::TouchSensor, 7, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV3 rotate
        // SV4 は y_axis の原点スイッチ用に予約したスロットだが、**スイッチが未装着**の
        // あいだは Unused にしておく。TouchSensor のままだと配線の有無に関わらず
        // FEEDBACK を 100Hz で送り続ける一方、PC 側は受け取り手（config/main_hand.yaml の
        // sensors:）を持たないので、そのフレームは誰にも配られず捨てられるだけになる。
        // **スイッチを付けたら TouchSensor へ戻す**（同時に config/main_hand.yaml の
        // sensors: と config/main_hand_positions.yaml の axes.y_axis.homing も戻す。
        // 3 つのうち 1 つでも欠けると「センサが応答していません」で動作確認が止まる）。
        //
        // TODO(実機で確認): sensorActiveLow は仮値。**同じ基板の SV3 が false だからと
        // いって合わせてはならない** —— 極性はスロットごとの配線で決まる。スイッチを
        // 付けた日に FEEDBACK の bit4 を非接触・接触の両方で実測して確定すること。
        {SlotRole::Unused, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 y_axis (未装着)
    },
    // 基板 #1（DIP=1）: サブハンド
    //
    // TODO(実機で確認): SV1〜SV4 の sensorActiveLow は仮値。TouchSensor にする日に
    // 実測して確定すること（基板 #0 の SV3 の実測値は**この基板の配線を何も保証しない**）。
    {
        {SlotRole::Servo, 4, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV0 sub_gripper
        {SlotRole::Unused, 5, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV1
        {SlotRole::Unused, 6, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV2
        {SlotRole::Unused, 7, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV3
        {SlotRole::Unused, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV4
    },
};

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
// 4: スロットの役割を基板番号ごとに持つようにした（kSlotsByBoard が持つ）。
//    基板 #0 の SV3 が Servo（sub_gripper）から TouchSensor（rotate の原点スイッチ）に
//    なり、sub_gripper は基板 #1 の SV0（0x48）へ移った。**焼き忘れた基板は
//    「サーボのつもりのピンが入力のまま」または逆になる**ので、版番号での検出が要る。
// 5: 基板 #0 の SV4（y_axis の原点スイッチ用）が TouchSensor から Unused になった。
//    **スイッチが未装着なので暫定であり、付けたら TouchSensor へ戻して版番号をまた上げる。**
//    デバイス ID 0x44 が FEEDBACK を送るかどうかがバイナリで変わるため、上げないと
//    「SV4 が Unused のファーム」と「SV4 が TouchSensor のファーム」が同じ v4 を名乗り、
//    どちらが焼かれているのかを INFO の照合で切り分けられなくなる（版番号は「バイナリの
//    区別が付く」ことだけが存在理由なので、CAN 上の振る舞いが変わったら必ず上げる）。
//
// **上げたら config/<robot>.yaml の expected_firmware も揃えること**（仕様書 §3.4）。
// PC 側は INFO の申告値と突き合わせ、食い違ったらそのモータを FAULT にする ——
// これは焼き忘れを見つけるための仕掛けなので、揃え忘れると「正しく焼いたのに
// 全部 FAULT」になる。表示される不一致メッセージに期待値と申告値の両方が出る。
constexpr uint8_t kFirmwareVersion = 5;

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
