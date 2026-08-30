"""リミットスイッチまで軸を寄せて零点を確定する。

電源投入位置をそのまま原点にすると、前の試合の終了姿勢や、搬送中に手で動かした
ぶんがそのまま座標のずれになる。位置定数はすべて原点からの相対値なので、
ずれた原点のまま走らせると全ステップが同じだけずれた場所へ動く。

**この操作は「当たるまで動かす」ので、止める仕組みが要る。** 3 つ用意してある:

1. **探索距離の上限** (`HomingSpec.search_distance`) — 超えたら失敗として降りる。
   配線が抜けている・センサが死んでいる場合の唯一の無人の歯止め
2. **センサ鮮度の事前確認** — フィードバックが途絶しているセンサでは 1 歩も動かさない。
   死んだセンサは「いつまでも当たらない」形でしか現れず、探索距離いっぱいまで
   機構を押し込んでから初めて分かる
3. **緊急停止** — 目標値を送る経路 (`AxisHandle`) が既にインターロックを通る

**原点確定はグループ単位でしか行わない。** 左右直結ペアを別々の時刻に確定すると、
その間に片方が動いたぶんだけ消えないオフセットが残り、正常な動作でも即座に
偏差超過で止まる (`M3508PositionLoop.set_group_origin_here` と同じ理由)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from lib.sequence.motors import AxisHandle
from lib.sequence.positions import AxisSpec, HomingSpec

logger = logging.getLogger(__name__)

__all__ = ["HomingError", "HomingRunner"]


class HomingError(RuntimeError):
    """零点を確定できなかった。シーケンスを止めて操縦者に知らせる。

    「原点がずれたまま走る」より「動かないまま止まる」ほうが安全なので、
    黙って続行してはならない。
    """


SensorActive = Callable[[str], bool]
SensorStale = Callable[[str], bool]
CaptureOrigin = Callable[[str], None]
SleepFunc = Callable[[float], Awaitable[None]]


class HomingRunner:
    """1 軸ぶんのホーミングを実行する。

    センサの読み取りと原点確定の実体は注入する。ここが `CANManager` や
    `M3508PositionLoop` を直接掴むと、CAN を立てないと 1 行も検証できなくなる。
    """

    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_is_stale: SensorStale,
        capture_origin: CaptureOrigin,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            sensor_active: センサ名 → 接触しているか (`GenericDriver.sensor_active`)
            sensor_is_stale: センサ名 → フィードバックが途絶しているか
            capture_origin: 軸名 → その軸の現在位置を原点として確定する
                (左右ペアはグループ全員へ展開されること)
            sleep: 1 ステップごとの待ち (テストで差し替える)
        """
        self._sensor_active = sensor_active
        self._sensor_is_stale = sensor_is_stale
        self._capture_origin = capture_origin
        self._sleep = sleep

    async def home(self, spec: AxisSpec, handle: AxisHandle, *, start_value: float = 0.0) -> float:
        """`spec.homing` に従って軸を寄せ、当たった位置を原点として確定する。

        Args:
            spec: 対象軸。`homing` を持たない軸を渡すのは呼び出し側の誤り
            handle: 目標値の送り口。**軸単位で 1 回だけ指令する**
                (左右が別々の時刻に動くとその場で機構が壊れる)
            start_value: 探索を始める位置 [軸の unit]。現在位置が読めない場合は 0

        Returns:
            原点確定までに動かした距離 [軸の unit]。ログと検証用。

        Raises:
            HomingError: センサが途絶している / 探索距離を超えても当たらなかった
        """
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        # **1 歩も動かす前に見る。** 死んだセンサは「いつまでも当たらない」形でしか
        # 現れず、探索距離いっぱいまで機構を押し込んでから初めて分かる
        if self._sensor_is_stale(homing.sensor):
            raise HomingError(
                f"軸 '{spec.name}' の原点センサ '{homing.sensor}' が応答していません"
                " (配線・基板の電源を確認してください)"
            )

        if self._sensor_active(homing.sensor):
            # 既に触れている。動かさずに確定する (押し込む方向へ動かさない)
            logger.info("[homing] %s: 既にセンサに触れているためその場を原点にする", spec.name)
            self._capture_origin(spec.name)
            return 0.0

        travelled = 0.0
        value = start_value
        while travelled < homing.search_distance:
            value += homing.direction * homing.step
            travelled += homing.step
            await handle.set_target_value(spec.to_commands(value))
            await self._sleep(homing.settle_s)

            if self._sensor_active(homing.sensor):
                logger.info(
                    "[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit
                )
                self._capture_origin(spec.name)
                return travelled

        raise HomingError(
            f"軸 '{spec.name}' が {homing.search_distance}{spec.unit} 動かしても"
            f" 原点センサ '{homing.sensor}' に到達しませんでした"
            " (探索方向・機構の引っかかり・センサの配線を確認してください)"
        )
