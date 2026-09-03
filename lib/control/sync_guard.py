"""左右直結ペアを「この周期で電流 0 に落とすか」の判断に変える層。

位置制御ループの中でこの判断とラッチを抱えていたが、電流フレームの合成
(C620 の 0x200 に 4 モータ分を詰める) とは関心が違う。片方は「どの機構を守るか」、
もう片方は「どうバスへ出すか」で、混ざっていると保護の条件を読むために送信の
コードを読むことになる。ここは判断とラッチ、および同期補正量の算出だけを持ち、
電流を落とす行為も補正を指令へ足す行為も呼び出し側 (``M3508PositionLoop``) に残す。

超過の境界そのものは ``SyncGroup.violation`` に一本化してある。この層に固有なのは
「debounce しない」「ラッチする」「グループ単位で扱う」の 3 点で、いずれも
壊れるまでの猶予が短い局所保護であることに由来する
(3 層の比較は lib/axis_sync.py のモジュール docstring を参照)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping

from lib.axis_sync import SyncGroup

logger = logging.getLogger(__name__)

__all__ = ["SyncGuard"]

PositionReader = Callable[[str], float]


class SyncGuard:
    """機構的に直結したモータ組の登録・偏差判定・ラッチを持つ。"""

    def __init__(self, *, context: str = "", logger: logging.Logger = logger) -> None:
        """
        Args:
            context: ログに添える文脈 (例: ``bus=can_m3508``)。試合中に
                「どの系統が止まったか」が分からないと復旧手順を選べない
            logger: 発報の出力先
        """
        self._context = f" ({context})" if context else ""
        self._logger = logger
        self._groups: dict[str, SyncGroup] = {}
        # モータ名 → 所属グループ名。周期処理でグループを引くための逆引き
        self._group_of: dict[str, str] = {}
        # 偏差超過のラッチ。reset() を通るまで電流 0 を維持する
        self._violations: set[str] = set()
        # フィードバック途絶の遷移でのみログを出すためのフラグ
        self._stale_groups: set[str] = set()

    # ------------------------------------------------------------------ #
    #  構成
    # ------------------------------------------------------------------ #

    def add(self, group: SyncGroup) -> None:
        """監視するグループを登録する。

        メンバがこのループに載っているかの確認は呼び出し側が行う (どのモータを
        握っているかを知っているのは呼び出し側だけ)。ここが弾くのは、同じ
        グループ名の二重登録と、1 台が 2 グループに属する構成の 2 つ。後者を
        通すとどちらの許容値で止めるかが曖昧になる。
        """
        if group.name in self._groups:
            raise ValueError(f"同期グループ '{group.name}' は既に登録済み")

        for member in group.members:
            existing = self._group_of.get(member.name)
            if existing is not None:
                raise ValueError(f"モータ '{member.name}' は既に同期グループ '{existing}' に所属")

        self._groups[group.name] = group
        for member in group.members:
            self._group_of[member.name] = group.name

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(self._groups)

    def __contains__(self, group_name: object) -> bool:
        return group_name in self._groups

    def group_of(self, motor_name: str) -> str | None:
        """モータが属するグループ名。属していなければ None。"""
        return self._group_of.get(motor_name)

    def members_of(self, group_name: str) -> tuple[str, ...]:
        """グループを構成するモータ名。

        Raises:
            KeyError: 未登録のグループ名
        """
        return tuple(member.name for member in self._groups[group_name].members)

    # ------------------------------------------------------------------ #
    #  判断
    # ------------------------------------------------------------------ #

    @property
    def violations(self) -> frozenset[str]:
        """偏差超過でラッチ中のグループ名。"""
        return frozenset(self._violations)

    def reset(self, name: str | None = None) -> None:
        """偏差超過のラッチを解除する (None で全グループ)。

        解除は「人間がずれを直した」という宣言であり、通す経路は操縦者の緊急停止解除
        (``RobotServer._reset_sync_latches``) だけに限る。制御ループ内部の復帰処理
        (緊急停止中の目標解除・動作確認からの復帰) からは決して呼ばない。機構が
        物理的にずれているという事実はそれらの操作では直らず、自動解除すると人間が
        原因に気付かないまま再び駆動してしまうため。
        解除しても偏差判定そのものは無効化されない。直っていなければ次の周期で
        再びラッチする。

        Raises:
            KeyError: 未登録のグループ名
        """
        if name is None:
            self._violations.clear()
            return
        if name not in self._groups:
            raise KeyError(name)
        self._violations.discard(name)

    def blocked(self, *, stale: Mapping[str, bool], position_of: PositionReader) -> frozenset[str]:
        """この周期で電流 0 に落とすグループ名を決める。

        判定は「メンバの誰かが途絶した」と「左右の偏差が許容を超えた」の 2 つ。どちらも
        「左右のうち片方だけが動き続ける」状況を作らせないための保護で、グループ単位で
        しか意味を持たない。

        途絶しているグループでは偏差判定を行わない。欠けたメンバを含む比較は
        「ずれていない」とも「ずれている」とも言えず、どのみち電流は 0 に落ちている。

        Args:
            stale: モータ名 → フィードバックが古いか
            position_of: モータ名 → 現在位置 (指令単位)。途絶したグループでは
                呼ばれない
        """
        blocked = set(self._violations)
        for group in self._groups.values():
            if any(stale.get(member.name, True) for member in group.members):
                # 左右直結の機構では片方だけ止めると残った側が押し続けて壊れるため、
                # 1 台でも途絶したらグループ全員を電流 0 にする
                blocked.add(group.name)
                if group.name not in self._stale_groups:
                    self._stale_groups.add(group.name)
                    self._logger.warning(
                        "同期グループのフィードバック途絶のため全員を電流 0 に落とす (axis=%s)%s",
                        group.name,
                        self._context,
                    )
                continue
            self._stale_groups.discard(group.name)
            if group.name in self._violations:
                continue
            if self._check_deviation(group, position_of):
                blocked.add(group.name)
        return frozenset(blocked)

    def corrections(
        self, *, position_of: PositionReader, skip_groups: Collection[str]
    ) -> dict[str, float]:
        """この周期で各モータへ加える同期補正量 (モータ名 → 指令単位の操作量)。

        判定と換算そのものは ``SyncGroup.corrections`` が持つ。この層に固有なのは
        「今この周期で出してよいグループはどれか」だけで、``skip_groups`` に挙がった
        グループは 1 台も辞書に載せない。

        ``skip_groups`` には少なくとも ``blocked()`` の返り値を渡すこと。あちらは
        「電流 0 に落とす」判断なので、そこへ補正だけが生き残ると、力を抜いたはずの
        周期で左右が押し合う。**途絶したグループでは ``position_of`` を呼ばない**のも
        同じ経路で担保される (未受信の 0.0 を現在位置として平均へ混ぜない)。

        Args:
            position_of: モータ名 → 現在位置 (指令単位)
            skip_groups: 補正を出さないグループ名
        """
        corrections: dict[str, float] = {}
        for group in self._groups.values():
            if group.name in skip_groups or group.sync_kp == 0.0:
                continue
            positions = {member.name: position_of(member.name) for member in group.members}
            corrections.update(group.corrections(positions))
        return corrections

    def _check_deviation(self, group: SyncGroup, position_of: PositionReader) -> bool:
        """グループの左右ずれを判定し、超過ならラッチして True を返す。

        ``SyncMonitor`` (50Hz) と意図的に二重の判定になっている。こちらは「電流を即 0 に
        する」局所保護で、機構が壊れる前に力を抜くことだけを担う。SyncMonitor は
        「試合を止めて人間に知らせる」全体保護で役割が違うため、片方があれば十分とは
        しない。連続サンプル数による debounce も入れない (壊れるまでの猶予が短く、
        1 周期でも早く力を抜く方が安全側。誤発報しても代償は電流 0 だけで済む)。
        """
        positions = {member.name: position_of(member.name) for member in group.members}
        deviation = group.violation(positions)
        if deviation is None:
            return False

        self._violations.add(group.name)
        self._logger.error(
            "同期ずれのため電流 0 にラッチ (axis=%s, deviation=%.3f, tolerance=%.3f)%s",
            group.name,
            deviation,
            group.tolerance,
            self._context,
        )
        return True
