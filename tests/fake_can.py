"""CAN 層に関心のないテスト向けの ``CANManager`` モック。

``RobotServer`` のテストは 8 ファイルあり、そのどれもが
``MagicMock(spec=CANManager)`` に ``motors`` / ``bus_names`` / ``send`` /
``health`` を生やす同じコードを書き写していた。``CANManager`` の公開面を 1 つ
変えるだけで 8 ファイルが同時に赤くなり、そのたびに「モックの追従」として
テストを実装へ合わせる作業が発生する。網の側を実装に合わせて編み直しては、
そもそも網の意味が無い。組み立てはここ 1 箇所に置く。

**実 ``CANManager`` の受信状態を作るのも本ファイルの役目とする。** 受信時刻や
bus-off は本番では受信ループ・ドライバ層だけが動かすもので、外から書き込む口は
無い。テストのためだけに本番へ注入口を生やすと、それは本番コードからも呼べる
API になる (「フィードバックが来たことにする」関数が本番に存在してよい理由は
無い)。代わりに、内部へ手を伸ばす操作をこの 1 ファイルへ閉じ込める。
``tests/server_fixtures.py`` がサーバー内部に対して持つのと同じ特権。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from unittest.mock import AsyncMock, MagicMock

import can

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from tests.fake_health import ok_health_snapshot

#: 特に指定が無いモータの状態。値そのものに意味は無い (存在することだけが要る)
DEFAULT_MOTOR_STATE = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)


def mock_motor(name: str, state: MotorState | None = None) -> MagicMock:
    """``state`` と ``name`` だけを持つモータドライバのモック。

    ``emergency_stop_message()`` は敢えてスタブしない。MagicMock の既定戻り値
    (None ではない) のままにすることで、緊急停止がドライバ固有フレームを
    送る経路をテストが素通りせずに通る。
    """
    motor = MagicMock()
    motor.name = name
    motor.state = state if state is not None else DEFAULT_MOTOR_STATE
    return motor


def mock_can_manager(
    motors: Mapping[str, MotorState | MagicMock] | Iterable[str] = ("m1",),
    *,
    bus_name: str = "bus0",
) -> CANManager:
    """モータ状態と OK ヘルスを返すだけの CANManager モック。

    ``health()`` に OK スナップショットを返させるのは必須。返さないと
    ``RobotServer`` の「判定できないものは DOWN」経路を常に踏み、本番とは
    別物の状態でテストしてしまう (tests/fake_health.py 参照)。
    """
    if isinstance(motors, Mapping):
        drivers = {
            name: value if isinstance(value, MagicMock) else mock_motor(name, value)
            for name, value in motors.items()
        }
    else:
        drivers = {name: mock_motor(name) for name in motors}

    mgr = MagicMock(spec=CANManager)
    mgr.motors = drivers
    mgr.bus_names = (bus_name,)
    mgr.get_motor.side_effect = drivers.__getitem__
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    # 戻り値は「励磁できなかったモータ名」。既定の MagicMock を返させると
    # `_reactivate_motors` が真値として受け取り、無励磁の誤報告が全テストに出る
    mgr.activate_motors = AsyncMock(return_value=[])
    mgr.last_feedback_at.return_value = None
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def set_motors(mgr: CANManager, drivers: Mapping[str, object]) -> None:
    """モックのモータ構成を実ドライバへ差し替える。

    ヘルスと state メッセージの中身は ``motors`` から引かれるため、差し替えは
    必ずこの 1 経路を通す (2 箇所へ書くと構成とヘルスが食い違う)。
    """
    mgr.motors = dict(drivers)
    mgr.get_motor.side_effect = mgr.motors.__getitem__


# ---------------------------------------------------------------------- #
#  実 CANManager の受信状態を作る
# ---------------------------------------------------------------------- #


def deliver_frame(mgr: CANManager, bus_name: str, msg: can.Message) -> None:
    """バスに 1 通届いたことにして、受信ループと同じ経路でモータへ配る。

    受信時刻を直に書くのと違い、宛先判定 (``matches_feedback``) とデコード
    (``update_state``) を実際に通る。フィードバック鮮度は「解釈できたフレーム」
    でしか進んではいけないという契約があるので、そこを迂回して時刻だけ進める
    テストは、途絶検出が壊れても緑のままになる。
    """
    mgr._dispatch_frame(bus_name, mgr._bus_motors[bus_name], msg)


def mark_feedback_at(mgr: CANManager, motor_name: str, at: float) -> None:
    """フィードバック受信時刻だけを任意の値に置く。

    「待機開始より前の受信」「送信の 1ms 後」のように *時刻そのもの* が検証
    対象の場合は、実フレームを流しても時計を狙った位置には置けない。
    ``deliver_frame`` で足りるならそちらを使うこと。
    """
    mgr._last_rx_at[motor_name] = at


def mark_bus_off(mgr: CANManager, bus_name: str, *, value: bool = True) -> None:
    """バスの bus-off 状態を立てる。

    bus-off はコントローラがエラーカウンタ超過で自らバスから切り離された状態で、
    virtual バスでは起こせない。DOWN 判定の経路を踏むにはここで作るしかない。
    """
    mgr._bus_off[bus_name] = value
