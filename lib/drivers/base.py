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


# 許容差の単一情報源 (POSITION=1deg / VELOCITY=5rpm)。
# 到達判定 (シーケンス) と動作確認 (セッティングタイム) の双方がここだけを見る。
# ドライバ固有の単位や減速比は default_tolerance のオーバーライドで換算する
_DEFAULT_REACH_TOLERANCES: dict[ControlMode, float] = {
    ControlMode.POSITION: 1.0,
    ControlMode.VELOCITY: 5.0,
}

# 動作確認で「指令変位のうち何割動けば合格とみなすか」。
# 許容差が指令変位以上だと |静止 - 目標| <= 許容差 が成立し、まったく動いていない
# モータが合格する。config の magnitude と許容差の大小関係は config 側からは保証
# できないため、判定側で許容差を指令変位で頭打ちにして構造的に塞ぐ
_CHECK_MOTION_RATIO = 0.5


@dataclass(frozen=True)
class CheckContext:
    """``check_command`` が ``evaluate_check_result`` へ渡す指令内容。

    文字列キーの辞書だと、キー名の綴りやモード表現 (``ControlMode.value`` と生文字列)
    がドライバごとにずれても誰も気付けないため型を付ける。
    """

    mode: ControlMode
    # 目標値。単位はドライバの内部単位 (EDULITE 05 なら rad / rad/s)
    target: float
    # 指令直前の観測値。``target`` との差が「この指令で動くはずの量」になる。
    # 静止と区別できる指令かどうかの判定に要る
    reference: float = 0.0
    # 内部単位 → 操縦者向け表示単位への係数と単位名。
    # 操縦者は config に書いた deg / rpm しか知らないため、内部単位のまま出すと原因を追えない
    display_scale: float = 1.0
    display_unit: str = ""

    @property
    def commanded_displacement(self) -> float:
        """この指令で動くはずの量 (絶対値)。"""
        return abs(self.target - self.reference)

    def display(self, value: float) -> str:
        return f"{value * self.display_scale:.2f}{self.display_unit}"


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

        到達判定 (``is_target_reached``) と動作確認 (``check_tolerance``) の双方が
        ここから引く。ドライバ側に別の定数を置くと、片方だけ直したときに
        「到達判定は新しい値、動作確認は古い値」という乖離が起きる。
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

    # ------------------------------------------------------------------ #
    #  起動手順と、モータ単位の能動テスト用 API
    # ------------------------------------------------------------------ #
    # `initialization_steps` / `activation_steps` / `requires_fresh_feedback_for_activation`
    # / `feedback_probe_message` は起動経路 (`CANManager.initialize_motors`) が使う。
    #
    # **`check_*` 系は現在どこからも呼ばれていない。** 動作確認をモータ単位で駆動する
    # `MotorCheckRunner` のための API だったが、両ハンドを 1 本のシーケンスで駆動する形
    # (robots/motor_check.py) へ移したので、判定はシーケンスエンジンの到達判定が担う。
    # 新しいドライバを足す人に不要な実装を強いないよう、次に触るときへ撤去を残してある。

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

    def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
        """動作確認前に順次送る ``(message, delay_after_seconds)``。既定は初期化不要。"""
        return []

    def check_safety_error(self) -> str | None:
        """既知の異常により動作確認できない場合、その理由を返す。"""
        return None

    def requires_fresh_feedback_for_check(self) -> bool:
        """動作確認前と初期化中に新鮮なfeedbackを要求するか。"""
        return False

    def emergency_stop_message(self) -> can.Message | None:
        """ドライバ固有の非常停止フレーム。共通停止だけで足りる場合は ``None``。"""
        return None

    @abc.abstractmethod
    def check_command(self, *, magnitude: float) -> tuple[can.Message, CheckContext]:
        """動作確認用の指令メッセージと ``CheckContext`` を返す。"""

    @abc.abstractmethod
    def evaluate_check_result(self, context: CheckContext) -> tuple[bool, str | None]:
        """現在のフィードバックが check_command の指令に追従したか判定する。

        戻り値:
            (passed, detail)
            detail は失敗時の人間向け説明。成功時は基本 None。
        """

    @abc.abstractmethod
    def reset_after_check(self) -> can.Message:
        """動作確認後に元の安全状態に戻す指令メッセージを返す。

        駆動状態を残さないための最後の砦なので、実装しない選択肢を与えない。
        """

    def check_tolerance(self, context: CheckContext) -> float:
        """動作確認の合否に使う許容差。

        既定許容差 (``default_tolerance``) と「指令変位の一定割合」の小さい方を採る。
        許容差が指令変位以上のときに素通しすると、静止したままのモータが
        「目標との差が許容差以内」で合格してしまう。
        """
        return min(
            self.default_tolerance(context.mode),
            context.commanded_displacement * _CHECK_MOTION_RATIO,
        )

    def evaluate_tracking(self, context: CheckContext) -> tuple[bool, str | None]:
        """目標値とフィードバックが同次元のモード共通の追従判定。

        CURRENT / DUTY のように指令とフィードバックの次元が違うモードは
        「追従」を定義できないため、各ドライバが回転検出などの別基準を持つ。
        """
        observed = self._observed_for(context.mode)
        if observed is None:
            return False, f"追従を判定できない制御モード: {context.mode.value}"

        if abs(observed - context.target) <= self.check_tolerance(context):
            return True, None

        detail = f"目標 {context.display(context.target)}, 観測 {context.display(observed)}"
        if context.commanded_displacement <= self.default_tolerance(context.mode):
            # 指令量が小さすぎて合否そのものが成立しない。「動かなかった」と
            # 混同されないよう、操縦者に config を直す先を示す
            detail += (
                f" (指令量 {context.display(context.commanded_displacement)} が"
                f"許容差 {context.display(self.default_tolerance(context.mode))} 以下で"
                "静止と区別できない。config の motor_check.magnitude を大きくすること)"
            )
        return False, detail
