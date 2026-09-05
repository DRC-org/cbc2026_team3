from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import can


class ControlMode(Enum):
    POSITION = "position"
    VELOCITY = "velocity"
    CURRENT = "current"
    DUTY = "duty"
    # 離散状態アクチュエータ (電磁弁) の 2 値指令。0 = OFF / 非 0 = ON。
    # duty を流用しないのは、「duty 0.3 の電磁弁」という意味を持たない指令を
    # 構造的に作れなくするため (docs/motor_driver_can_protocol.md §9.2)。
    # DUTY と同じく到達判定の対象外 (default_tolerance が inf を返す) で、
    # 待ちは位置定数 yaml の settle_s が持つ。
    ON_OFF = "on_off"


@dataclass(frozen=True)
class MotorState:
    position: float = 0.0
    velocity: float = 0.0
    current: float = 0.0
    temperature: float = 0.0
    reached: bool = False


@dataclass(frozen=True)
class TelemetrySupport:
    """このドライバが **実際に測れる** フィードバック項目の宣言。

    ``MotorState`` の 4 値は制御経路 (位置制御ループ・偏差監視・ホーミング) が
    float であることに依存しているので、測る手段が無い項目もそこでは 0.0 のまま
    運ぶ。**その 0.0 をそのまま UI やヘルス判定へ流すと「測ったように見える 0」に
    なる** —— 自作モタドラの DC 基板と電磁弁基板は電流も温度も測る手段を持たず
    (仕様書 §3.2)、操縦者は「本当に 0」なのか「そもそも測っていない」のかを
    画面から区別できない。測定可否を制御値とは別のメタ情報として持ち、
    配信の境界 (`RobotServer._build_state_message` / `CANManager.health`) で
    測れない項目を ``None`` へ倒すために使う。

    **4 値は必ずこの 1 組で運ぶ。** バラの bool 引数に分解すると、一部だけ配線した
    経路が作れてしまい、残りが黙って既定値のまま効く (`HealthThresholds` を
    1 組で運ぶのと同じ理由)。判定をドライバ側に置くのは、UI にドライバ種別を
    書き写さないため。
    """

    position: bool = True
    velocity: bool = True
    current: bool = True
    temperature: bool = True


#: 4 値すべてを測れるドライバ (M3508 / EDULITE 05 / DM3520) の宣言
FULL_TELEMETRY = TelemetrySupport()

#: 状態フラグしか返さない基板 (DC・電磁弁) の宣言
NO_TELEMETRY = TelemetrySupport(position=False, velocity=False, current=False, temperature=False)


# 許容差の単一情報源 (POSITION=1deg / VELOCITY=5rpm)。
# 到達判定 (シーケンス) と動作確認 (セッティングタイム) の双方がここだけを見る。
# ドライバ固有の単位や減速比は default_tolerance のオーバーライドで換算する
_DEFAULT_REACH_TOLERANCES: dict[ControlMode, float] = {
    ControlMode.POSITION: 1.0,
    ControlMode.VELOCITY: 5.0,
}


class MotorDriver(abc.ABC):
    """モータドライバの基底クラス。各プロトコル固有のドライバはこれを継承する。"""

    def __init__(self, name: str, can_id: int) -> None:
        self.name = name
        self.can_id = can_id
        self._state = MotorState()

    @property
    def state(self) -> MotorState:
        return self._state

    @property
    def telemetry(self) -> TelemetrySupport:
        """測れるフィードバック項目の宣言 (``TelemetrySupport`` の docstring 参照)。

        既定は「4 値とも測れる」。C620 (M3508) / EDULITE 05 / DM3520 は
        フィードバックに位置・速度・電流・温度をすべて載せるので、
        オーバーライドが要るのは測れない項目を持つドライバだけになる。
        """
        return FULL_TELEMETRY

    @abc.abstractmethod
    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        """目標値を CAN メッセージにエンコードする。"""

    @abc.abstractmethod
    def decode_feedback(self, msg: can.Message) -> MotorState:
        """CAN メッセージからフィードバックをデコードする。"""

    def update_state(self, msg: can.Message) -> MotorState:
        """フィードバックメッセージを受けて内部状態を更新する。"""
        self._state = self.decode_feedback(msg)
        return self._state

    @abc.abstractmethod
    def matches_feedback(self, msg: can.Message) -> bool:
        """受信した CAN メッセージがこのモータのフィードバックかどうか判定する。"""

    # ------------------------------------------------------------------ #
    #  自己申告フレーム (自作モタドラの INFO)
    # ------------------------------------------------------------------ #
    # 既定は「そんなフレームは無い」。M3508 と EDULITE 05 は版番号を自己申告しないので、
    # 受信ループから見て「誰も名乗り出ない」= 捨てる、で正しい。

    def matches_info(self, msg: can.Message) -> bool:
        """受信した CAN メッセージがこのデバイスの自己申告かどうか判定する。"""
        return False

    def update_info(self, msg: can.Message) -> None:  # noqa: B027
        """自己申告フレームを受けて内部状態を更新する。既定は何もしない。

        abstractmethod にしないのは、**自己申告を持たないドライバに空実装を書かせない**
        ため。M3508 と EDULITE 05 は INFO を送らず matches_info が常に False を返すので、
        そもそもここへは来ない。
        """

    # ------------------------------------------------------------------ #
    #  目標到達判定 (シーケンスの wait_reached 用)
    # ------------------------------------------------------------------ #
    # 到達フラグの有無やフィードバック単位はプロトコル固有のため、判定はドライバ層に置く。
    # 基底にデフォルト実装を持たせ、単位換算や減速比が要るドライバだけオーバーライドする。

    def default_tolerance(self, mode: ControlMode) -> float:
        """許容差の単一情報源 (POSITION=1deg / VELOCITY=5rpm)。

        到達判定 (``is_target_reached``) はここだけから引く。ドライバ側に別の定数を
        置くと、位置定数 yaml の tolerance を省いた軸だけが古い値のまま残る。
        CURRENT / DUTY は開ループ指令でフィードバック量と目標値の次元が一致しないため、
        「到達判定しない」意味で無限大を返す。
        """
        return _DEFAULT_REACH_TOLERANCES.get(mode, math.inf)

    def is_target_reached(
        self,
        target: float,
        mode: ControlMode,
        *,
        tolerance: float | None = None,
    ) -> bool:
        """現在のフィードバックが目標値に到達しているか判定する。"""
        tol = self.default_tolerance(mode) if tolerance is None else tolerance
        if math.isinf(tol):
            return True

        observed = self._observed_for(mode)
        if observed is None:
            return True
        return abs(observed - target) <= tol

    def feedback_position(self) -> float:
        """多回転を含む位置フィードバック。

        ドライバ種別によらず同じ意味で比較できる値を返す (単回転でラップしない)。
        単位はドライバごとの位置単位のまま (M3508/自作=deg, EDULITE 05=rad) なので、
        比較してよいのは同一機構に直結した同種モータ同士。
        """
        return self._state.position

    def _observed_for(self, mode: ControlMode) -> float | None:
        """モードに対応するフィードバック量。比較対象がない場合は None。"""
        # 位置フィードバックの定義は feedback_position に一本化する。
        # 到達判定と偏差監視で別々の定義を持つと、片方だけ直したときに気付けない
        if mode is ControlMode.POSITION:
            return self.feedback_position()
        if mode is ControlMode.VELOCITY:
            return self._state.velocity
        if mode is ControlMode.CURRENT:
            return self._state.current
        return None

    # ------------------------------------------------------------------ #
    #  ヘルスチェック判定 (Phase 6)
    # ------------------------------------------------------------------ #
    # しきい値は config/*.yaml の health セクション由来 (デフォルト: warning=65, critical=80)
    # サブクラスは過電流フラグや fault フラグを持つ場合のみオーバーライドする

    def has_thermal_warning(self, temp_warning_c: float) -> bool:
        """温度警告判定。基底実装は MotorState.temperature と warning しきい値の比較。

        **温度を測れないドライバでは判定そのものを行わない。** 測れない基板の
        ``temperature`` は 0.0 のまま動かないので比較しても警告は出ないが、
        しきい値が 0 以下になった構成では「測っていない 0」が警告に化ける。
        判定材料の有無は `telemetry` が単一情報源なので、ここもそれを見る。
        """
        if not self.telemetry.temperature:
            return False
        return self._state.temperature >= temp_warning_c

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        """温度異常 (FAULT) 判定。基底実装は MotorState.temperature と critical しきい値の比較。

        測れないドライバで判定しないのは ``has_thermal_warning`` と同じ理由。
        """
        if not self.telemetry.temperature:
            return False
        return self._state.temperature >= temp_critical_c

    def has_overcurrent_warning(self) -> bool:
        """過電流警告判定。デフォルトは判定材料がないので False (各サブクラスで上書き)。"""
        return False

    def is_fault(self) -> bool:
        """ハード障害フラグ。デフォルトは False (各サブクラスで上書き)。"""
        return False

    def is_energized(self) -> bool | None:
        """励磁されているか。**判定手段を持たないドライバは None を返す。**

        `False` と `None` を分けるのは、「無励磁だと分かっている」と「励磁状態を
        報告しない基板なので分からない」を同じ値にすると、後者が「無励磁」として
        警告に化けるため。自作モタドラも C620 も励磁の有無を報告しないので、
        既定は None のままにしておくこと。

        呼び出し側 (`RobotServer._safety_state`) は「緊急停止していないのに
        無励磁のまま」を異常として拾う。指令フレームは出続けるのに機体が動かない
        という状態は、フィードバックもヘルスも正常に見えるので他に現れる場所が無い。
        """
        return None

    def health_detail(self) -> str | None:
        """ヘルスに添える 1 行。**言うことが無いドライバは None を返す。**

        `MotorHealthInfo.detail` へそのまま載る。状態そのもの (OK / STALE など) では
        表せない事情 —— 「値は届いているが意味が変わった」たぐい —— を操縦者へ渡す口で、
        既存の WS 契約に既にあるフィールドなので追加の配信経路は要らない。

        `is_energized()` と同じく、判定手段を持たないドライバは既定の None のまま
        にしておくこと。空文字を返すと「詳細がある」と「無い」が同じ見た目になる。
        """
        return None

    # ------------------------------------------------------------------ #
    #  起動手順
    # ------------------------------------------------------------------ #
    # `initialization_steps` / `activation_steps` / `requires_fresh_feedback_for_activation`
    # / `feedback_probe_message` は起動経路 (`CANManager.initialize_motors`) が使う。
    #
    # 動作確認 (セッティングタイム) はここに専用 API を持たない。両ハンドを 1 本の
    # シーケンス (sequences/motor_check.py) で駆動する形にしたので、合否はシーケンス
    # エンジンの到達判定 (`is_target_reached`) がそのまま担う。

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        """起動時に送る ``(message, delay_after_seconds)``。既定は初期化不要。

        励磁の有効化はここに含めない (activation_steps を使う)。
        """
        return []

    def activation_steps(self) -> list[tuple[can.Message, float]]:
        """励磁を有効化する ``(message, delay_after_seconds)``。既定は有効化不要。

        有効化した瞬間にモータが動き出さない順序 (現在値を目標に書いてから enable する等)
        はドライバ自身が決める。``requires_fresh_feedback_for_activation`` が True の
        ドライバでは、呼び出し側が新しいフィードバックを受信してから呼ぶ契約になっている
        ため、本メソッドは常に「最新の ``state`` を読んでよい」前提で組み立ててよい。
        """
        return []

    def requires_fresh_feedback_for_activation(self) -> bool:
        """activation_steps を組み立てる前に新しいフィードバックが必要か。

        目標位置へ追従するモータは、現在角を目標として書かずに励磁すると原点へ飛ぶ。
        実測角が確認できない限り有効化してはならないドライバが True を返す。
        """
        return False

    def feedback_probe_message(self) -> can.Message | None:
        """フィードバックを引き出すための問い合わせフレーム。

        無励磁のまま応答だけを得られるフレームに限る (励磁や目標変更を伴ってはならない)。
        自発的にフィードバックを送るモータでは不要なので既定は ``None``。
        """
        return None

    def emergency_stop_message(self) -> can.Message | None:
        """ドライバ固有の非常停止フレーム。共通停止だけで足りる場合は ``None``。"""
        return None
