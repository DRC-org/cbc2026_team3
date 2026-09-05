from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lib.match_state import Court
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import PositionTable

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup

logger = logging.getLogger(__name__)


class SequenceTimeoutError(RuntimeError):
    """目標位置に到達しないままタイムアウトしたときに送出される。

    run() が例外を捕まえてシーケンスを停止させるため、掴めていないワークを
    搬送するといった「黙って次のステップへ進む」事故を防げる。
    """


class AxisSyncError(RuntimeError):
    """左右ペア軸の位置ずれ (sync_tolerance 超過) を検知したときに送出される。

    左右 2 台が機構的に直結した軸では、ずれたまま動かし続けると押し合いになって
    その場で機構が壊れる。到達判定を満たしていても偏差が残っていれば次のステップへ
    進ませず、run() に捕捉させてシーケンスを止める。
    """


@dataclass
class StepInfo:
    label: str
    method_name: str
    require_trigger: bool
    #: このステップが指令する軸。**空 = 宣言なしで、構成に依らず必ず登録する**
    #: (宣言を省いたステップまで除外候補にすると、軸を持たないステップ ——
    #: 零点確定のように対象を実行時に決めるもの —— が構成次第で消える)。
    axes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExcludedStep:
    """構成に存在しない軸を指令するため登録しなかったステップ。

    **除外を黙って行ってはならない。** 動作確認の目的は「指令どおり動くか」を
    確かめることなので、存在しない軸のステップを黙って落とすと、本番構成で
    1 軸が config から漏れていてもそのステップごと消えて全ステップが成功する。
    症状は「動作確認は通ったのに試合でその軸だけ動かない」で、確認そのものが
    意味を失う。除外したステップと欠けている軸を配信に載せれば、操縦者は
    「機構が未装着だから減っている」のか「config の書き忘れで減っている」のかを
    画面で判断できる。
    """

    label: str
    missing_axes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"step": self.label, "missing_axes": list(self.missing_axes)}


@dataclass(frozen=True)
class StepFailure:
    """失敗したステップと理由。**操縦者が失敗を知る唯一の手掛かり。**

    到達タイムアウト・左右ずれ・零点確定失敗はどれもステップ単位の try で握られる。
    握った結果を残さないと journal 以外どこにも出ず、画面は「待機中」と同じ表示に
    戻る (3 層保護の第 1 層である `AxisSyncError` が画面から無音になる)。

    メソッド名は載せない。`StepInfo.method_name` を配信に含めないのと同じ理由で、
    操縦者に見せて意味があるのはラベルと理由だけ。
    """

    step_index: int
    label: str
    message: str

    def to_dict(self) -> dict:
        return {"step_index": self.step_index, "step": self.label, "message": self.message}


def step(
    label: str,
    *,
    require_trigger: bool = False,
    axes: Collection[str] | None = None,
) -> Callable:
    """ステップを宣言する。

    Args:
        axes: このステップが ``move_to`` で指令する軸。省略したステップは
            ``restrict_to_axes()`` の対象外で、構成に依らず必ず登録される。

    **必要な軸はステップの隣で宣言する。** ステップと必要軸の表を別々に持つと
    片方だけ直せてしまい、ステップが軸を 1 つ増やしたときに判定だけが古いまま
    残る (軸名の衝突を `PositionTable.merged` が起動ごとに落とすのと同じ方針)。
    """
    declared = frozenset(axes or ())

    def decorator(method: Callable) -> Callable:
        method._step_label = label  # type: ignore[attr-defined]
        method._step_require_trigger = require_trigger  # type: ignore[attr-defined]
        method._step_axes = declared  # type: ignore[attr-defined]
        return method

    return decorator


class Sequence:
    _steps: list[StepInfo]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        steps: list[StepInfo] = []
        for name, value in cls.__dict__.items():
            if callable(value) and hasattr(value, "_step_label"):
                steps.append(
                    StepInfo(
                        label=value._step_label,
                        method_name=name,
                        require_trigger=value._step_require_trigger,
                        axes=value._step_axes,
                    )
                )
        cls._steps = steps

    def __init__(self, name: str) -> None:
        self.name = name
        self._current_index: int = 0
        self._waiting_trigger: bool = False
        self._running: bool = False
        self._trigger_event: asyncio.Event = asyncio.Event()
        # request_stop で run() ループを抜けさせるイベント
        self._stop_event: asyncio.Event = asyncio.Event()
        # 通常停止・完走後に外部から再開要求を受けるためのイベント
        self._resume_event: asyncio.Event = asyncio.Event()
        # request_jump で次の反復に反映する目標 index
        self._jump_request: int | None = None
        # 直近の実行で失敗したステップ。次の実行が始まるまで保持する
        # (平常時は None。値が無いことを空文字で表さない)
        self._last_error: StepFailure | None = None
        # 自陣コート。赤青で配置が左右反転するため各 step 内で参照して動作を分ける
        self._court: Court = Court.RED
        # モータアクセス層。bind_motors で外部から注入する (未注入でもシーケンスは動作する)
        self._motors: MotorGroup | None = None
        # 機構位置の定数表。bind_positions で外部から注入する (未注入でもシーケンスは動作する)
        self._positions: PositionTable | None = None
        # 構成に存在する軸。None = 制限なし (restrict_to_axes を呼んでいない)。
        # 空集合と None を区別する: 前者は「軸が 1 本も無い構成」で、全ての指令が落ちる
        self._available_axes: frozenset[str] | None = None
        # 構成に無い軸を指令するため登録しなかったステップ
        self._excluded_steps: tuple[ExcludedStep, ...] = ()

    # ------------------------------------------------------------------ #
    #  モータアクセス
    # ------------------------------------------------------------------ #

    def bind_motors(self, group: MotorGroup) -> None:
        self._motors = group

    @property
    def has_motors(self) -> bool:
        return self._motors is not None

    @property
    def motors(self) -> MotorGroup:
        # 未 bind でもシーケンス自体は動かせる必要があるため、参照された時点で初めて弾く
        if self._motors is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に MotorGroup が bind されていません "
                "(bind_motors を呼んでください)"
            )
        return self._motors

    # ------------------------------------------------------------------ #
    #  機構位置の定数
    # ------------------------------------------------------------------ #

    def bind_positions(self, table: PositionTable) -> None:
        self._positions = table

    @property
    def positions(self) -> PositionTable:
        # bind_motors と同じ方針。未 bind でもシーケンス定義自体は成立させる
        if self._positions is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に PositionTable が bind されていません "
                "(bind_positions を呼んでください)"
            )
        return self._positions

    # ------------------------------------------------------------------ #
    #  構成による絞り込み
    # ------------------------------------------------------------------ #

    def restrict_to_axes(self, available: Collection[str]) -> None:
        """構成に存在する軸だけを対象にする。**絞り込みの判定はここにしかない。**

        ``@step(axes=...)`` で宣言した軸のうち、1 本も存在しないステップは登録から
        外し、`excluded_steps` へ理由 (欠けている軸) とともに残す。一部だけ存在する
        ステップは残し、`move_to` が存在する軸だけへ指令する —— ステップの登録可否と
        指令先の絞り込みを別の場所に置くと、片方だけが古い構成観のまま残る。

        機構が未装着のハンドを外して実機を動かすため (config/bench/main_hand) に要る。
        **除外したことは必ず外から読めるようにする** (`ExcludedStep` の docstring)。
        """
        allowed = frozenset(available)
        self._available_axes = allowed

        kept: list[StepInfo] = []
        excluded: list[ExcludedStep] = []
        # 元の宣言はクラス属性が持つ。self._steps を起点にすると、2 度呼んだときに
        # 1 度目の絞り込み結果へさらに絞りが掛かり、除外理由も 1 度目のぶんが消える
        for info in type(self)._steps:
            if not info.axes:
                kept.append(info)
                continue
            present = info.axes & allowed
            if present:
                kept.append(replace(info, axes=present))
            else:
                excluded.append(
                    ExcludedStep(label=info.label, missing_axes=tuple(sorted(info.axes - allowed)))
                )
        self._steps = kept
        self._excluded_steps = tuple(excluded)

    @property
    def excluded_steps(self) -> tuple[ExcludedStep, ...]:
        """構成に無い軸を指令するため登録しなかったステップ。制限が無ければ空。"""
        return self._excluded_steps

    def available_targets(self, targets: Mapping[str, str]) -> dict[str, str]:
        """``{軸名: 位置名}`` を、構成に存在する軸だけへ絞る。

        複数軸をまとめて指令するステップ (初期姿勢への復帰など) は、片方のハンドが
        不在でも残る必要がある —— 特に最後の復帰ステップを落とすと「必ず初期姿勢で
        終わる」性質が消え、操縦者が試合前に手で戻すことになる。

        絞り込みを掛けるのは `restrict_to_axes()` を呼んだシーケンス
        (`MotorCheckSequence.move_to`) だけ。**通す口を 1 つに絞ってあるので、
        各ステップの本体には絞り込みを書かない** —— 書き忘れた 1 行だけが
        `PositionLookupError` で落ち、しかも症状はその構成でしか出ない。
        """
        if self._available_axes is None:
            return dict(targets)
        return {
            axis: position for axis, position in targets.items() if axis in self._available_axes
        }

    async def move_to(
        self,
        targets: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> None:
        """``{軸名: 位置名}`` を位置定数から引いて指令し、全軸の到達を待つ。

        単位換算・許容差・待ち時間はすべて位置定数 yaml の責務なので、
        シーケンス本体には生の数値が現れない。到達しない軸があれば
        SequenceTimeoutError を、到達しても左右がずれている軸があれば
        AxisSyncError を送出して停止させる。待機中に緊急停止などで目標が
        消えれば ``lib.sequence.motors.WaitInterruptedError`` が伝播する
        (到達しなかったのではなく中断されたので、タイムアウトとは別の例外にして
        操縦者へ見せる文言が嘘にならないようにしてある)。
        指令値は保持したままにする (落下すると危険な軸で保持トルクを失わないため)。
        """
        table = self.positions
        pending: list[tuple[AxisHandle, str, Awaitable[bool]]] = []

        for axis, position_name in targets.items():
            spec = table.axis(axis)
            # 未定義のモータ名は MotorGroup 側が利用可能な名前付きの例外にしてくれる
            handle = AxisHandle(spec, [getattr(self.motors, name) for name in spec.motor_names])
            await handle.set_target_value(
                table.commands(axis, position_name, court=self.court),
            )
            pending.append(
                (
                    handle,
                    position_name,
                    handle.wait_reached(timeout=spec.timeout_s if timeout is None else timeout),
                )
            )

        results = await asyncio.gather(*(awaitable for _, _, awaitable in pending))
        failed = [
            f"{handle.name}->{position_name}"
            for (handle, position_name, _), reached in zip(pending, results, strict=True)
            if not reached
        ]
        if failed:
            raise SequenceTimeoutError(
                f"シーケンス '{self.name}': 目標位置に到達しませんでした ({', '.join(failed)})"
            )

        # 到達判定を満たしていても左右がずれていれば押し合いで機構が壊れるため先へ進めない。
        # 判定そのものは SyncGroup.violation (3 層共通) が持ち、ここは結果を例外に変えるだけ
        desynced = []
        for handle, _, _ in pending:
            error = handle.sync_violation()
            if error is None:
                continue
            allowed = table.sync_tolerance(handle.name) or 0.0
            desynced.append(f"{handle.name}: 偏差 {error:.3f} > 許容 {allowed:.3f}")
        if desynced:
            raise AxisSyncError(
                f"シーケンス '{self.name}': 軸内のモータ位置がずれています ({', '.join(desynced)})"
            )

    @property
    def court(self) -> Court:
        return self._court

    def set_court(self, court: Court) -> None:
        self._court = court

    @property
    def current_step(self) -> StepInfo | None:
        if 0 <= self._current_index < len(self._steps):
            return self._steps[self._current_index]
        return None

    @property
    def waiting_trigger(self) -> bool:
        return self._waiting_trigger

    @property
    def is_running(self) -> bool:
        """run() の実行中か。

        「今このシーケンスが制御権を握っているか」は動作確認の排他や停止処理の
        判断材料になるため外から読めなければならない。実行フラグそのものは
        run() だけが書き換えるので読み取り専用で公開する。
        """
        return self._running

    @property
    def last_error(self) -> StepFailure | None:
        """直近の実行で失敗したステップ。失敗していなければ None。

        次の実行 (`run()`) が始まった時点で捨てる。残すと、走り直した後の画面に
        前回の失敗が出たままになり、操縦者は今の実行が失敗したのだと読む。
        """
        return self._last_error

    @property
    def steps(self) -> tuple[StepInfo, ...]:
        """宣言順のステップ表 (読み取り専用ビュー)。

        ``_steps`` は ``__init_subclass__`` が組み立てるクラス属性で、同じ
        シーケンスクラスの全インスタンスで共有される。実体の list をそのまま
        渡すと、受け取った側の 1 回の append/sort が以後生成される全インスタンスの
        進行順を書き換えてしまうため、コピーした tuple しか外へ出さない。
        """
        return tuple(self._steps)

    @property
    def steps_info(self) -> list[dict]:
        """ステップ表を配信用の dict へ落としたもの (method_name は含めない)。"""
        return [
            {
                "index": i,
                "label": s.label,
                "require_trigger": s.require_trigger,
            }
            for i, s in enumerate(self.steps)
        ]

    @property
    def progress(self) -> dict:
        return {
            "sequence": self.name,
            "current_step": self.current_step.label if self.current_step else None,
            "step_index": self._current_index,
            "total_steps": len(self._steps),
            "waiting_trigger": self._waiting_trigger,
            "running": self._running,
            "steps": self.steps_info,
            # 失敗を配信に載せる唯一の経路。載せないと、止まった理由が journal に
            # しか残らず、画面は「待機中 — START で開始」と描くだけになる
            "last_error": self._last_error.to_dict() if self._last_error is not None else None,
        }

    def trigger(self) -> None:
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_jump(self, index: int) -> None:
        """指定インデックスへジャンプ。実行中なら次の境界で反映、停止中なら再開。"""
        if not (0 <= index < len(self._steps)):
            return
        self._request_index(index)

    def request_stop(self) -> None:
        """通常停止 (緊急停止と異なり CAN 層には介入しない)。"""
        self._stop_event.set()
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_start(self) -> None:
        """先頭から実行開始。完走後・停止後の再起動に使う。"""
        self._request_index(0)

    def _request_index(self, index: int) -> None:
        """次に実行するステップを予約する。開始要求もジャンプ要求もここを通る。

        **実行中は再開イベントを立てない。** 立てると、その 1 通が次の通常停止まで
        イベントとして残り、`run()` が降りた瞬間に `run_forever` が拾って、操縦者が
        何も押していないのに先頭から全工程を走り直す。実行中に届く 2 通目の開始要求は
        現実に起きる —— 操縦者 2 名 + 予備タブが同じ画面を開いており、配信周期
        (50ms) 以内の二度押しでも、詰まったクライアントが最大 1 秒古い `running:false`
        を描いていても届く。`match_finish` 経由なら、フェーズが `finished` なのに
        機体だけが動き続けることになる (`run_forever` はフェーズを見ない)。

        開始とジャンプで分岐を書き分けないのは、片方だけがこの規則を失うのを
        防ぐため (症状は書き直した側にしか出ない)。
        """
        self._jump_request = index
        if self._running:
            # 実行中: トリガー待機を解除して次の境界で反映させる
            self._trigger_event.set()
        else:
            # 通常停止後・完走後: 再開イベントで run() ループを起こす
            self._resume_event.set()

    def discard_pending_start(self) -> None:
        """まだ run_forever に拾われていない開始/ジャンプ要求を捨てる。

        緊急停止の直前に届いた開始要求を残したままにすると、停止処理を終えた
        次の瞬間にその要求が発火し、操縦者が何も押していないのに機体が動き出す。

        **破棄したことの正は `_jump_request` が None であることに置く。**
        `_resume_event.clear()` だけでは、既に待機に入っている `run_forever` の
        1 回を取り消せない (`asyncio.Event` は `set()` の時点で待機中の future を
        解決してしまう)。イベントを落とすのは、まだ待機に入っていない場合に
        余計な起床そのものを省くため。
        """
        self._resume_event.clear()
        self._jump_request = None

    async def run_forever(self) -> None:
        """開始要求を待って run() し、通常停止なら先頭へ巻き戻して再び待つ常駐ループ。

        起動時は resume を立てない。操縦者の明示的な開始合図 (request_start /
        request_jump) があるまでロボットを動かしてはならない。

        「停止したらどこへ戻るか」はシーケンス自身の状態遷移であって、呼び出し側に
        持たせると同じ巻き戻しを各所で書き写すことになる (書き忘れた経路だけが
        停止位置から再開し、操縦者の想定と違うステップが走る)。

        run() 内部の例外はステップ単位で握られているが、それでも漏れた場合に
        常駐ループごと終わらせてはならない。ループが死ぬとサーバーは生きたまま
        以後の開始要求だけが無反応になり、操縦者には原因が見えない。
        """
        while True:
            await self._resume_event.wait()
            self._resume_event.clear()
            if self._jump_request is None:
                # **`clear()` だけでは要求を取り消せない。** `asyncio.Event` は
                # `set()` の時点で待機中の future を解決してしまうので、その後の
                # `clear()` は既に起きることが決まった 1 回を止められない
                # (`run_forever` は常にここで待っているので、本番では必ずこの形になる)。
                # 破棄されたかどうかは要求そのもの (`_jump_request`) で判断する ——
                # 無ければ、緊急停止や通常停止が捨てた要求で起こされただけなので
                # 1 歩も動かさずに待ち直す
                continue
            try:
                await self.run()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("シーケンス '%s' の実行中に例外", self.name)
            # 通常停止された場合のみ先頭へ戻す。
            # 完走 (current_index == total) は位置を保持したままにする。
            if self._stop_event.is_set():
                self._current_index = 0
                self._stop_event.clear()

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        # 前回の失敗は今回の実行には掛からない。残すと、走り直した後の画面に
        # 前回の理由が出たままになる
        self._last_error = None
        try:
            while not self._stop_event.is_set():
                if self._jump_request is not None:
                    self._current_index = self._jump_request
                    self._jump_request = None
                if self._current_index >= len(self._steps):
                    break
                step_info = self._steps[self._current_index]

                if step_info.require_trigger:
                    self._waiting_trigger = True
                    self._trigger_event.clear()
                    await self._trigger_event.wait()
                    self._waiting_trigger = False
                    if self._stop_event.is_set():
                        break
                    if self._jump_request is not None:
                        continue

                method = getattr(self, step_info.method_name)
                try:
                    await method()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "シーケンス '%s' のステップ '%s' で例外", self.name, step_info.label
                    )
                    # ログだけでは操縦者に届かない。到達タイムアウトも左右ずれも
                    # 零点確定失敗もここへ集まるので、配信できる形にして残す
                    self._last_error = StepFailure(
                        step_index=self._current_index,
                        label=step_info.label,
                        message=str(exc) or "ステップの実行に失敗しました",
                    )
                    break

                if self._jump_request is None:
                    self._current_index += 1
        finally:
            self._running = False
            self._waiting_trigger = False

    async def reset(self) -> None:
        self._current_index = 0
        self._waiting_trigger = False
        self._running = False
        self._trigger_event.clear()
        self._stop_event.clear()
        self._jump_request = None
        self._last_error = None
