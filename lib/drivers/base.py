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


@dataclass(frozen=True)
class MotorState:
    position: float = 0.0
    velocity: float = 0.0
    current: float = 0.0
    temperature: float = 0.0
    reached: bool = False


# 到達判定の既定許容差。GenericDriver の動作確認しきい値と同じ値を使う
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
    #  目標到達判定 (シーケンスの wait_reached 用)
    # ------------------------------------------------------------------ #
    # 到達フラグの有無やフィードバック単位はプロトコル固有のため、判定はドライバ層に置く。
    # 既存の evaluate_check_result と同じ流儀で、基底にデフォルト実装を持たせ
    # 特別扱いが必要なドライバだけオーバーライドする。

    def default_tolerance(self, mode: ControlMode) -> float:
        """到達判定の既定許容差。

        値は動作確認 (evaluate_check_result) の既定値と揃えてある
        (POSITION=1deg / VELOCITY=5rpm)。
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

    def _observed_for(self, mode: ControlMode) -> float | None:
        """モードに対応するフィードバック量。比較対象がない場合は None。"""
        if mode is ControlMode.POSITION:
            return self._state.position
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

    def has_thermal_warning(self, temp_warning_c: float, temp_critical_c: float) -> bool:
        """温度警告判定。基底実装は MotorState.temperature と warning しきい値の比較。"""
        return self._state.temperature >= temp_warning_c

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        """温度異常 (FAULT) 判定。基底実装は MotorState.temperature と critical しきい値の比較。"""
        return self._state.temperature >= temp_critical_c

    def has_overcurrent_warning(self) -> bool:
        """過電流警告判定。デフォルトは判定材料がないので False (各サブクラスで上書き)。"""
        return False

    def is_fault(self) -> bool:
        """ハード障害フラグ。デフォルトは False (各サブクラスで上書き)。"""
        return False

    # ------------------------------------------------------------------ #
    #  アクチュエータ動作確認 (Phase 6 段階⑦)
    # ------------------------------------------------------------------ #
    # MotorCheckRunner からの能動テスト用 API
    # 抽象メソッドにすると既存のテスト用 mock や派生クラスを破壊するため、
    # デフォルトは NotImplementedError raise としてサブクラスで個別に実装する

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

    def prepare_check(self) -> list[can.Message]:
        """動作確認前に順次送る初期化メッセージ。既定は初期化不要。"""
        return []

    def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
        """動作確認前のメッセージと送信後待機時間。"""
        return [(message, 0.0) for message in self.prepare_check()]

    def check_safety_error(self) -> str | None:
        """既知の異常により動作確認できない場合、その理由を返す。"""
        return None

    def requires_fresh_feedback_for_check(self) -> bool:
        """動作確認前と初期化中に新鮮なfeedbackを要求するか。"""
        return False

    def emergency_stop_message(self) -> can.Message | None:
        """ドライバ固有の非常停止フレーム。共通停止だけで足りる場合は ``None``。"""
        return None

    def check_command(self, *, magnitude: float) -> tuple[can.Message, dict]:
        """動作確認用の指令メッセージとコンテキストを返す。

        戻り値:
            (msg, context)
            context は evaluate_check_result で参照する辞書で、
            最低限 {"target": float} を含む。
        """
        raise NotImplementedError(f"{type(self).__name__} は check_command を実装していません")

    def evaluate_check_result(
        self,
        state: MotorState,
        context: dict,
        *,
        tolerance: float | None = None,
    ) -> tuple[bool, str | None]:
        """フィードバック state が check_command の指令に追従したか判定する。

        戻り値:
            (passed, detail)
            detail は失敗時の人間向け説明。成功時は基本 None。
        """
        raise NotImplementedError(
            f"{type(self).__name__} は evaluate_check_result を実装していません"
        )

    def reset_after_check(self) -> can.Message:
        """動作確認後に元の安全状態に戻す指令メッセージを返す。"""
        raise NotImplementedError(f"{type(self).__name__} は reset_after_check を実装していません")
