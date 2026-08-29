from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import can

from lib.config_schema import DEFAULT_HEALTH, HealthThresholds
from lib.control.periodic import LogThrottle
from lib.drivers.base import MotorDriver
from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_RECV_TIMEOUT = 0.01


class BlockingRunner(Protocol):
    """``bus.send`` / ``bus.recv`` のような同期呼び出しを実行する口。

    python-can の API は同期ブロッキングなので、そのまま呼ぶとイベントループごと
    止まる (受信が止まれば位置制御は全軸フィードバック途絶に落ちる)。既定は
    スレッドプールへの委譲で、テストは差し替えて同期実行する。
    """

    def __call__[T](self, func: Callable[..., T], /, *args: Any) -> Awaitable[T]: ...


async def _run_in_default_executor[T](func: Callable[..., T], /, *args: Any) -> T:
    # コルーチンの中では get_running_loop() が正しい。get_event_loop() は実行中の
    # ループが無い文脈で新しいループを黙って作る (Python 3.12 では非推奨) ため、
    # 「送ったつもりのフレームがどのループでも走らない」経路を作りうる
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


# 励磁の有効化前にフィードバックを待つ上限。1Mbps の CAN でモータが応答するには十分で、
# 応答が無いモータ (電源断・配線ミス) の分だけ起動が遅れる上限でもある。
_ACTIVATION_FEEDBACK_TIMEOUT_S = 0.5
# 待機中に問い合わせフレームを送る間隔。自発的にフィードバックを送らないモータでも
# この周期で応答を引き出せる。
_ACTIVATION_PROBE_INTERVAL_S = 0.05


class CANManager:
    """複数の CAN バスとモータドライバを asyncio で管理する。"""

    def __init__(self, *, run_blocking: BlockingRunner = _run_in_default_executor) -> None:
        self._run_blocking = run_blocking
        self._buses: dict[str, can.Bus] = {}
        self._motors: dict[str, MotorDriver] = {}
        # センサはモータではないので _motors には入れない (仕様書 §5.2)。
        # 動作確認・目標値再送・UI のモータ一覧に「常に 0 のモータ」を並べないため。
        # 受信の振り分けとヘルス監視だけは同じ経路に乗せる
        self._sensors: dict[str, MotorDriver] = {}
        self._motor_bus: dict[str, str] = {}
        self._bus_motors: dict[str, list[MotorDriver]] = {}
        self._tasks: list[asyncio.Task[None]] = []

        # ヘルスチェック (Phase 6) 用の受動監視タイムスタンプとカウンタ。
        # 受信ループは _last_rx_at のみ更新し、送信失敗は send_to_bus 内で
        # _tx_error_count を増やす。
        self._last_rx_at: dict[str, float] = {}
        self._last_tx_at: dict[str, float] = {}
        self._tx_error_count: dict[str, int] = {}
        self._rx_error_count: dict[str, int] = {}
        self._bus_off: dict[str, bool] = {}
        self._bus_channels: dict[str, str] = {}

        # 受信エラーは不正フレームが続く限り毎フレーム発生する。1Mbps の CAN では
        # 1kHz 規模でログが流れ、本当に読みたい 1 行が押し流される
        self._rx_log = LogThrottle(logger)

    def add_bus(self, name: str, bus: can.Bus, channel: str = "") -> None:
        self._buses[name] = bus
        self._bus_motors.setdefault(name, [])

        # channel 文字列はヘルススナップショットの BusHealthInfo.channel に載せる。
        # 呼び出し側が省略した場合は python-can の channel_info から推測 (失敗時は空文字)。
        if not channel:
            channel = getattr(bus, "channel_info", "") or ""
        self._bus_channels[name] = channel

        self._tx_error_count.setdefault(name, 0)
        self._rx_error_count.setdefault(name, 0)
        self._bus_off.setdefault(name, False)

    def add_motor(self, bus_name: str, motor: MotorDriver) -> None:
        """バスにモータを登録する。名前・CAN ID の重複は構成時に弾く。

        名前が衝突すると _motors は後勝ちで上書きされる一方 _bus_motors には両方残り、
        受信ループは孤児になった先勝ちドライバへフィードバックを配り続ける
        (get_motor は後勝ちを返すので、状態が永久に更新されないモータができる)。
        同一バスの can_id 衝突は受信ループが最初にマッチした 1 台で打ち切るため、
        もう一方が永久にフィードバックを得られない。

        ただしロボットごとに別インスタンスを持つ構成のため、
        **ロボット横断の衝突は 1 つの CANManager からは原理的に検出できない**
        (メインハンドとサブハンドは can_edulite / can_generic を物理的に共有する)。
        全ロボットを合わせた一意性は tests/test_robot_sequences.py の
        yaml 静的テストが引き続き担保する。
        """
        self._add_device(bus_name, motor)
        self._motors[motor.name] = motor

    def _add_device(self, bus_name: str, device: MotorDriver) -> None:
        """モータとセンサに共通の登録処理 (受信の振り分け先と重複検査)。

        検査を 2 箇所に書くと、片方だけが緩んで「センサとモータが同じ can_id を
        名乗る」構成が作れてしまう。同じバス上の別デバイスなので壊れ方は同じ。
        """
        if bus_name not in self._buses:
            raise KeyError(f"バス '{bus_name}' が登録されていません")

        existing_bus = self._motor_bus.get(device.name)
        if existing_bus is not None:
            raise ValueError(f"デバイス '{device.name}' は既に登録済み (bus={existing_bus})")

        for existing in self._bus_motors[bus_name]:
            if existing.can_id == device.can_id:
                raise ValueError(
                    f"バス '{bus_name}' の can_id 0x{device.can_id:02X} が重複 "
                    f"('{device.name}' と '{existing.name}')"
                )

        self._motor_bus[device.name] = bus_name
        self._bus_motors[bus_name].append(device)

    def add_sensor(self, bus_name: str, sensor: MotorDriver) -> None:
        """バスにセンサを登録する (仕様書 §5.2)。

        自作基板は 1 スロット = 1 CAN デバイスで、センサも自分のデバイス ID で
        FEEDBACK を送る。**登録しないと受信ループがそのフレームを誰にも配らず、
        接触が PC まで届かない。** 名前・can_id の重複検査はモータと同じ空間で行う
        (同じバス上の別デバイスなので、衝突すれば同じ壊れ方をする)。

        motors とは別に持つのは、動作確認・目標値再送・UI のモータ一覧へ
        「常に 0 のモータ」を並べないため。ヘルス (STALE) は同じ扱いで監視する。
        """
        self._add_device(bus_name, sensor)
        self._sensors[sensor.name] = sensor

    def get_motor(self, name: str) -> MotorDriver:
        return self._motors[name]

    @property
    def sensors(self) -> Mapping[str, MotorDriver]:
        """登録済みセンサの読み取り専用ビュー (宣言順を保つ)。"""
        return MappingProxyType(self._sensors)

    @property
    def motors(self) -> Mapping[str, MotorDriver]:
        """登録済みモータの読み取り専用ビュー (宣言順を保つ)。

        サーバーは全モータの状態配信・停止フレーム送信のために一覧を必要とする。
        書き換え可能な dict を渡すと登録経路が add_motor 以外にも生まれ、
        名前・can_id の重複検査を素通りしたモータが混ざりうる。
        ビューなので登録の追加はそのまま見えるが、外からは変更できない。
        """
        return MappingProxyType(self._motors)

    @property
    def bus_names(self) -> tuple[str, ...]:
        """登録済みバス名 (登録順)。

        バス単位のブロードキャスト (E-STOP の 0x7FF など) の宛先を列挙するために要る。
        ``can.Bus`` そのものは渡さない。生のバスを掴むと send_to_bus を経由しない
        送信ができてしまい、送信失敗が tx_error_count に載らない = ヘルスが
        「正常」と言い続ける経路ができる。
        """
        return tuple(self._buses)

    def bus_of(self, motor_name: str) -> str | None:
        """モータが所属するバス名。未登録なら None。

        どのバスに繋がったモータかは動作確認結果の表示に必要で、
        未登録は「表示できない」だけなので例外にせず None を返す。
        """
        return self._motor_bus.get(motor_name)

    def last_feedback_at(self, motor_name: str) -> float | None:
        """最後にフィードバックを受信した時刻 (time.time 基準)。未受信なら None。

        位置制御ループがフィードバック途絶を検出して電流を落とすために参照する。
        """
        return self._last_rx_at.get(motor_name)

    async def send(self, motor_name: str, msg: can.Message) -> None:
        bus_name = self._motor_bus[motor_name]
        await self.send_to_bus(bus_name, msg)

    async def send_to_bus(self, bus_name: str, msg: can.Message) -> None:
        bus = self._buses[bus_name]
        try:
            await self._run_blocking(bus.send, msg)
        except can.CanError:
            # CAN プロトコル層の送信失敗。tx_error_count を増やしつつ、
            # 既存呼び出し元 (server.py の e_stop など) との互換性のため例外を再 raise する。
            self._tx_error_count[bus_name] = self._tx_error_count.get(bus_name, 0) + 1
            raise
        except Exception:
            # OS / executor / その他の異常も健全性カウンタに反映してから上位へ伝搬。
            self._tx_error_count[bus_name] = self._tx_error_count.get(bus_name, 0) + 1
            raise
        else:
            self._last_tx_at[bus_name] = time.time()

    async def _receive_loop(self, bus_name: str) -> None:
        """バス 1 本ぶんの受信ループ。フレームの解釈失敗では降りない。

        降りる (= 受信の口そのものが失われた) 場合だけは必ずログに残す。
        ``_tasks`` は誰も await しないため、ここで記録しないとタスクの死が
        どこにも現れず、「UI は接続中のまま全モータが STALE」の原因が
        試合後まで分からない。
        """
        bus = self._buses[bus_name]
        motors = self._bus_motors[bus_name]

        try:
            while True:
                msg: can.Message | None = await self._run_blocking(bus.recv, _RECV_TIMEOUT)
                if msg is None:
                    continue
                self._dispatch_frame(bus_name, motors, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 受信 API 自体の失敗 (インタフェース断など) はフレーム 1 通の問題ではなく、
            # 握り潰して回り続けても全速で失敗を繰り返すだけなので伝播させる
            logger.exception("CAN 受信ループが停止しました (bus=%s)", bus_name)
            raise

    def _dispatch_frame(
        self, bus_name: str, motors: Sequence[MotorDriver], msg: can.Message
    ) -> None:
        """受信した 1 通を宛先モータへ配る。1 通の失敗はその 1 通に閉じ込める。

        ここで例外を素通しすると受信ループのタスクごと終わり、しかも ``_tasks`` は
        誰も await しないので例外は握り潰される。結果は「そのバスの全モータが以後
        永久に STALE、UI は接続中のまま」という、試合中に最も復旧しにくい壊れ方になる。
        バス上には他プロトコルの機器も、相方のロボット宛のフレームも流れてくるので、
        解釈できないフレームは必ず来るものとして扱う。

        捕捉するのは ``Exception`` だけ。``asyncio.CancelledError`` (shutdown の停止
        経路) と ``KeyboardInterrupt`` / ``SystemExit`` は ``BaseException`` 側にあり、
        握り潰すと「止められない受信ループ」ができるため素通しする。
        """
        for motor in motors:
            try:
                claimed = motor.matches_feedback(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 宛先判定で落ちるドライバは、他モータ宛のフレームまで巻き添えにしない。
                # 巻き添えの範囲をそのドライバ 1 台に閉じるため次のモータへ進む
                self._record_rx_error(bus_name, motor.name, "宛先判定")
                continue

            if claimed:
                try:
                    motor.update_state(msg)
                    # フィードバック鮮度を MotorHealth の STALE 判定に使う。
                    # デコードに失敗したフレームでは更新しない (解釈できていない値を
                    # 「受信できている」と報告すると、途絶の検出そのものが効かなくなる)
                    self._last_rx_at[motor.name] = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._record_rx_error(bus_name, motor.name, "状態更新")
                return

            # 自作モタドラの INFO (1Hz の自己申告, 仕様書 §3.4)。焼き忘れとサーボの
            # 型違いを見つける唯一の経路なので、FEEDBACK と同じ粒度で囲って配る
            try:
                info_claimed = motor.matches_info(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_rx_error(bus_name, motor.name, "INFO 宛先判定")
                continue

            if not info_claimed:
                continue

            try:
                # **_last_rx_at は更新しない。** INFO は 1Hz、FEEDBACK は 100Hz なので、
                # 自己申告で鮮度を書き換えると feedback_timeout_ms (既定 500ms) を
                # 満たし続け、**FEEDBACK が完全に途絶えてもモータが STALE にならない**。
                # 途絶検出そのものが効かなくなるので、鮮度はフィードバックだけが動かす
                motor.update_info(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_rx_error(bus_name, motor.name, "INFO 解釈")
            return

    def _record_rx_error(self, bus_name: str, motor_name: str, phase: str) -> None:
        """握り潰した受信失敗を、数として残しつつ間引いて記録する。

        件数を ``rx_error_count`` に積むのは、握り潰しが「異常が無い」ことにすり替わる
        のを防ぐため。一方でバスの健全性判定 (``BusHealth``) は動かさない。判定できない
        フレームを流す機器の相乗りは構成上あり得る (メインハンドとサブハンドは
        can_edulite / can_generic を物理的に共有する) ので、それだけで DEGRADED を
        出すと本物の送信障害の警告まで信用されなくなる。実害 —— そのモータの
        フィードバックが来ないこと —— は当該モータの STALE として別に現れる。
        """
        self._rx_error_count[bus_name] = self._rx_error_count.get(bus_name, 0) + 1
        self._rx_log.exception(
            f"{bus_name}:{motor_name}:{phase}",
            "CAN 受信フレームの%sで例外 (bus=%s, motor=%s)。このフレームは破棄します",
            phase,
            bus_name,
            motor_name,
        )

    async def run(self) -> None:
        for bus_name in self._buses:
            task = asyncio.create_task(self._receive_loop(bus_name))
            self._tasks.append(task)
        await self.initialize_motors()

    async def initialize_motors(self) -> None:
        """各モータの起動時設定を宣言順に送り、続けて励磁を有効化する。"""
        for motor_name, motor in self._motors.items():
            await self._send_steps(motor_name, motor.initialization_steps())
            await self.activate_motor(motor_name)

    async def activate_motors(
        self,
        *,
        should_abort: Callable[[], bool] | None = None,
        feedback_timeout_s: float = _ACTIVATION_FEEDBACK_TIMEOUT_S,
    ) -> None:
        """全モータの励磁を有効化する。緊急停止解除後の復帰にも使う。

        should_abort は「途中で有効化をやめるべきか」を返す。緊急停止が再び入った
        場合に、残りのモータへ enable を送らないための中断口。
        """
        for motor_name in self._motors:
            if should_abort is not None and should_abort():
                logger.warning("モータの有効化を中断しました (残り: %s 以降)", motor_name)
                return
            await self.activate_motor(
                motor_name,
                should_abort=should_abort,
                feedback_timeout_s=feedback_timeout_s,
            )

    async def activate_motor(
        self,
        motor_name: str,
        *,
        should_abort: Callable[[], bool] | None = None,
        feedback_timeout_s: float = _ACTIVATION_FEEDBACK_TIMEOUT_S,
    ) -> bool:
        """1 モータの励磁を有効化する。有効化しなかった場合は False。

        位置追従するモータは「現在角を目標に書いてから enable する」ことでしか
        有効化時の飛び出しを防げない。実測角を確認できないうちは有効化せず、
        無励磁のまま残すほうが安全なので、フィードバックが得られなければ諦める。
        """
        motor = self._motors[motor_name]

        if motor.requires_fresh_feedback_for_activation() and not await self._wait_fresh_feedback(
            motor_name, feedback_timeout_s
        ):
            logger.warning(
                "モータ '%s' のフィードバックを %.2fs 以内に受信できないため"
                "有効化を見送りました (無励磁のまま)",
                motor_name,
                feedback_timeout_s,
            )
            return False

        steps = motor.activation_steps()
        if not steps:
            return True
        if should_abort is not None and should_abort():
            logger.warning("モータ '%s' の有効化を中断しました", motor_name)
            return False

        await self._send_steps(motor_name, steps)
        return True

    async def _send_steps(self, motor_name: str, steps: list[tuple[can.Message, float]]) -> None:
        for message, delay_after_s in steps:
            await self.send(motor_name, message)
            if delay_after_s > 0:
                await asyncio.sleep(delay_after_s)

    async def _wait_fresh_feedback(self, motor_name: str, timeout_s: float) -> bool:
        """待機開始より後に届いたフィードバックを待つ。

        待機開始前に受信済みの値は set_zero より前の原点で測ったものかもしれず、
        保持目標として使うと原点の付け替え分だけモータが動いてしまう。そのため
        「新しく届いたこと」を要求し、受信済みの値の再利用は認めない。
        """
        baseline = self._last_rx_at.get(motor_name)
        probe = self._motors[motor_name].feedback_probe_message()
        deadline = time.monotonic() + timeout_s

        while True:
            last_rx = self._last_rx_at.get(motor_name)
            if last_rx is not None and (baseline is None or last_rx > baseline):
                return True
            if time.monotonic() >= deadline:
                return False
            if probe is not None:
                try:
                    await self.send(motor_name, probe)
                except Exception:
                    # 問い合わせが通らないバスでも、自発フィードバックが届く可能性は残る。
                    # 待機自体はタイムアウトまで続ける。
                    logger.debug("モータ '%s' への問い合わせ送信に失敗", motor_name)
            await asyncio.sleep(_ACTIVATION_PROBE_INTERVAL_S)

    async def shutdown(self) -> None:
        """受信タスクを畳み、全バスを閉じる。

        既に例外で死んでいる受信タスク (バスが down しているときの
        ``CanOperationError`` など) の例外をここで再送出しない。``main()`` の
        ``finally`` は 2 台ぶんの ``shutdown()`` を素の for で並べており、
        1 台目が送出すると 2 台目のバスが開いたまま残る。1 本のバスの
        ``bus.shutdown()`` が失敗した場合も同じ理由で残りを閉じ続ける。
        死因は受信ループ側が降りる前に必ずログへ残している。
        """
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("受信タスクは既に異常終了していました")
        self._tasks.clear()

        for bus_name, bus in self._buses.items():
            try:
                bus.shutdown()
            except Exception:
                logger.exception("バスの停止に失敗: bus=%s", bus_name)

    # ------------------------------------------------------------------ #
    #  ヘルスチェック (Phase 6, タスク 6-8)
    # ------------------------------------------------------------------ #

    def health(self, *, thresholds: HealthThresholds = DEFAULT_HEALTH) -> HealthSnapshot:
        """現在の受動監視状態から HealthSnapshot を組み立てる (同期処理)。

        サーバの WS 配信ループや GET /health から呼ばれる前提で副作用を持たない。
        能動 ping は段階⑥ の MotorCheckRunner が担うため本メソッドでは行わない。
        """
        now = time.time()

        buses: list[BusHealthInfo] = []
        motors: list[MotorHealthInfo] = []

        # モータ情報 (バス情報の last_rx_at 集計に必要なので先に確定させる)
        bus_latest_rx: dict[str, float] = {}
        # センサも同じ扱いで監視する。**死んだまま原点合わせを始めると
        # 「いつまでも当たらない」形でしか分からない**ので、途絶は検出したい
        for motor_name, motor in {**self._motors, **self._sensors}.items():
            bus_name = self._motor_bus[motor_name]
            last_fb = self._last_rx_at.get(motor_name)
            age_ms = (now - last_fb) * 1000.0 if last_fb is not None else None

            # 優先度: ハード fault > 温度 critical > フィードバック切れ > warning > OK。
            # FAULT 系は STALE/WARNING より重大なので先に判定する。
            stale = last_fb is None or (
                age_ms is not None and age_ms > thresholds.feedback_timeout_ms
            )
            warning = (
                motor.has_thermal_warning(thresholds.temp_warning_c)
                or motor.has_overcurrent_warning()
            )

            if motor.is_fault() or motor.has_thermal_fault(thresholds.temp_critical_c):
                state = MotorHealth.FAULT
            elif stale:
                state = MotorHealth.STALE
            elif warning:
                state = MotorHealth.WARNING
            else:
                state = MotorHealth.OK

            motors.append(
                MotorHealthInfo(
                    name=motor_name,
                    bus=bus_name,
                    state=state,
                    last_feedback_at=last_fb,
                    feedback_age_ms=age_ms,
                    temperature=motor.state.temperature,
                    detail=None,
                )
            )

            # バス側の last_rx_at にはバス上のいずれかのモータの最新受信時刻を採用
            if last_fb is not None:
                prev = bus_latest_rx.get(bus_name)
                if prev is None or last_fb > prev:
                    bus_latest_rx[bus_name] = last_fb

        # バス情報
        for bus_name, bus in self._buses.items():
            tx_err = self._tx_error_count.get(bus_name, 0)
            rx_err = self._rx_error_count.get(bus_name, 0)
            bus_off = self._bus_off.get(bus_name, False)

            # python-can の bus.state は virtual バスでは未提供のため getattr で防御的に読む。
            # ACTIVE 以外で ERROR/PASSIVE のときだけ降格判定に使う。
            can_state = getattr(bus, "state", None)
            error_state = getattr(can.BusState, "ERROR", None)
            passive_state = getattr(can.BusState, "PASSIVE", None)
            is_error = can_state is not None and can_state == error_state
            is_passive = can_state is not None and can_state == passive_state

            if bus_off or is_error:
                state = BusHealth.DOWN
            elif tx_err >= thresholds.tx_error_threshold or is_passive:
                state = BusHealth.DEGRADED
            else:
                state = BusHealth.OK

            buses.append(
                BusHealthInfo(
                    name=bus_name,
                    channel=self._bus_channels.get(bus_name, ""),
                    state=state,
                    last_tx_at=self._last_tx_at.get(bus_name),
                    last_rx_at=bus_latest_rx.get(bus_name),
                    tx_error_count=tx_err,
                    rx_error_count=rx_err,
                    bus_off=bus_off,
                )
            )

        overall = HealthSnapshot.compute_overall(buses, motors)
        return HealthSnapshot(timestamp=now, overall=overall, buses=buses, motors=motors)
