"""実時間に依存せず経過時間を制御するためのクロックスタブ。

制御層 (位置制御ループ・同期監視・周期タスク基盤・鮮度判定) は「いつ」を
すべて注入された ``clock`` から読む設計になっている。テストが実時間 sleep で
周期や経過を作ると、CI の負荷で揺れる試験になり、安全機構の試験が
「たまに落ちるから」と無効化される方向へ圧力がかかる。

同じ実装が各テストへ写されていると、片方だけ挙動を変えたときに
「どのテストのクロックか」を読み分ける必要が出るため 1 つに集約する。
"""

from __future__ import annotations


class FakeClock:
    """``time.monotonic`` 互換の単調増加クロック。``advance`` で任意に進める。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt
