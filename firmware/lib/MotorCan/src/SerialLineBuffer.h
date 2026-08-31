// デバッグ用シリアルの行組み立て。
//
// 両ファームが「1 文字ずつ読んで CR/LF で切り、長すぎる行は切り詰める」という
// 同じ骨格を各自で持っていた。行の切り出しだけをここに置き、行の解釈
// （DC は duty 1 個、サーボは「<ch> <角度>」）は各 main.cpp に残す。
//
// Arduino の String を使わないのは、受信のたびにヒープを触る実装をループの中に
// 置かないため。容量は呼び出し側が配列で与える（基板ごとに必要な長さが違う）。

#pragma once

#include <stdint.h>

namespace motorcan {

class SerialLineBuffer {
   public:
    // storage は終端の '\0' を含めて capacity バイト使う。
    SerialLineBuffer(char *storage, uint8_t capacity);

    // 受信した 1 文字を渡す。行が完成したら true を返し、line() で読めるようになる。
    // 空行（CRLF の 2 文字目や区切りの連打）では true を返さない。
    // 空行で「完成」と報告すると、数値として 0 に化けて duty 0 / 角度 0 の指令になる。
    //
    // line() の内容が有効なのは次に push() を呼ぶまで。
    bool push(char c);

    const char *line() const { return storage_; }

   private:
    char *storage_;
    uint8_t capacity_;
    uint8_t length_;
    bool pendingReset_;
};

}  // namespace motorcan
