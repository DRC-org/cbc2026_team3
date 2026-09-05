from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

import can

from lib.config_schema import DEFAULT_HEALTH, HealthThresholds
from lib.control.periodic import LogThrottle
from lib.drivers.base import MotorDriver
from lib.drivers.generic import GenericDriver
from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
)

logger = logging.getLogger(__name__)

_RECV_TIMEOUT = 0.01

# `fileno()` を持つバスで滞留を引き取るときの非ブロッキング呼び出し。
# python-can の `recv` は timeout=0 を「select を 0 秒で打ち切る」と解釈する
# (`None` は「今は 1 通も無い」であって失敗ではない)
_RECV_NO_WAIT = 0.0

# 1 回の起床で取り込むフレーム数の上限。**上限そのものより、上限に達したら
# 明示的に譲ることに意味がある** —— 滞留が深いときにこのループが制御周期を
# 締め出さないための区切りで、64 通 x 実測 47.5us ≒ 3ms は位置制御ループの
# 周期 (5ms) を 1 回落とさない範囲に収まる。
_RX_BATCH_MAX = 64

# --- SocketCAN エラーフレーム (linux/can/error.h) ------------------------------
# python-can の socketcan バスは既定でエラーフレームの受信を有効にする
# (CAN_RAW_ERR_FILTER = 0x1FFFFFFF)。フレームは `is_error_frame` が立ち、
# エラー種別は arbitration_id のビットとして載る。標準 ID 扱いで届くので
# 11bit へ切り詰められるが、定義済みの種別は全て 0x200 以下なので欠けない。
_CAN_ERR_BUSOFF = 0x00000040
_CAN_ERR_RESTARTED = 0x00000100

# 送信エラースコアの増減幅と上限。CAN コントローラの TEC と同じ規則
# (失敗 +8 / 成功 -1 / 上限 255) に合わせてある。
_TX_ERROR_SCORE_FAIL = 8
_TX_ERROR_SCORE_MAX = 255

# バスからの通信が観測できたら bus-off ラッチを外すまでの猶予は置かない。
# bus-off はコントローラがバスから切り離された状態そのものなので、1 通でも
# 送受信できた時点で「切り離されていない」が確定する。

# 受信が読めなくなったときの再試行間隔 [秒]。平常時は 1 度も使われない
# (`_RECV_TIMEOUT` のタイムアウトは成功であって失敗ではない)。
#
# **同一 socket は `ip link` の down/up を跨いで生き残る。** 実測では down 中の
# `recv` が `Network is down [Errno 100]` で失敗し、up 後は同じ socket のまま
# 何事もなく再開した。bus を作り直す必要はないので、ここは待って呼び直すだけでよい。
#
# 短いほど M3508 の累積角が欠ける窓が縮む (欠落中の回転は折り返し推定を狂わせる。
# `lib/drivers/m3508.py` の `_MAX_TRUSTED_GAP_S` を参照) が、down が続く間は
# 呼ぶたびに例外を作るので上限を置く。復旧ウォッチドッグの down/up 窓は実測で約 1 秒。
_RECV_RETRY_MIN_S = 0.02
_RECV_RETRY_MAX_S = 0.2


class _ReadableFd:
    """バスの受信 fd をイベントループの監視に載せ、「読める」を待てるようにする。

    **1 通ごとにエグゼキュータへ往復する形は、受信そのものの上限を作る。**
    `run_in_executor(bus.recv)` はスレッドの起床とイベントループへの復帰を伴い、
    実測で 1 通あたり 168us かかる。C620 は 1 台 1kHz でフィードバックを流すので
    M3508 2 台だけで 2000 通/秒、`can_generic` と合わせて 4000 通/秒を超え、
    **受信だけで 1 コアが埋まる**。追いつかなくなった分はカーネルのソケット
    バッファ溢れとして捨てられ、実機では `can_m3508` の受信 369 万通に対して
    **77 万通 (17%) が `rx_dropped`** に積まれていた。

    捨てられた窓は M3508 の累積角に化ける。滞留を詰めて処理している間は
    ``time.monotonic()`` で測った処理間隔が詰まって見えるので、
    `lib/drivers/m3508.py` の折り返し推定はその窓を「途切れていない」と読む。
    巡航 200mm/s ではモータ軸 1834rpm なので、**16ms 分が欠けるだけで半周を超え、
    累積角に 360deg (= 6.54mm) が注入される**。左右のどちらかにだけ乗れば
    そのまま同期ずれになり、症状は「動作中に軸が荒れて緊急停止」になる。

    そこで受信は fd の可読通知で起こし、起きたら滞留を出し切る。実測で
    1 通 168us → 47.5us、バス 1 本ぶんの CPU で 37.5% → 10.6% になる。

    ``fileno()`` を持たないバス (``--dry-run`` の virtual バス) では作れないので
    ``for_bus`` が None を返し、呼び出し側は従来のエグゼキュータ経由へ落ちる。
    """

    def __init__(self, fd: int) -> None:
        # コルーチンから呼ばれる前提。get_event_loop() は実行中のループが無い文脈で
        # 新しいループを黙って作るので使わない (このファイルの他の箇所と同じ)
        self._loop = asyncio.get_running_loop()
        self._fd = fd
        self._ready = asyncio.Event()
        self._armed = False
        self.resume()

    @classmethod
    def for_bus(cls, bus: can.Bus) -> _ReadableFd | None:
        """監視できるならインスタンスを、できなければ None を返す。

        **できない理由で落ちてはならない。** virtual バスもテストのモックも
        `fileno()` を持たない (あるいは int を返さない) が、どちらも受信そのものは
        従来経路で成立する。ここで例外にすると `--dry-run` が起動しなくなる。
        """
        fileno = getattr(bus, "fileno", None)
        if not callable(fileno):
            return None
        try:
            fd = fileno()
        except Exception:
            return None
        if not isinstance(fd, int) or fd < 0:
            return None
        try:
            return cls(fd)
        except (NotImplementedError, OSError, ValueError):
            # 監視できない fd (プラットフォームや socket の状態による)
            return None

    async def wait(self) -> None:
        """読めるようになるまで待つ。

        **クリアは取り込みの前に行う。** 後にすると、取り込んでいる間に届いた分の
        通知を消してしまい、その滞留は次のフレームが届くまで引き取られない。
        """
        await self._ready.wait()
        self._ready.clear()

    def resume(self) -> None:
        """監視を (再) 開始する。再開直後は 1 度必ず確かめる。"""
        if not self._armed:
            self._loop.add_reader(self._fd, self._ready.set)
            self._armed = True
        self._ready.set()

    def suspend(self) -> None:
        """監視を外す。down した socket で空回りしないための一時停止。"""
        if self._armed:
            self._loop.remove_reader(self._fd)
            self._armed = False
        self._ready.clear()

    def close(self) -> None:
        self.suspend()


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
        # **健全性の判定はこちらで行う。** `_tx_error_count` は起動からの累計で、
        # 一度でもしきい値を超えると二度と下がらない。実機では物理緊急停止で
        # DM3520 の電源が落ちた数秒間に 6000 件積み上がり、CAN が完全に復旧した後も
        # バスが永久に DEGRADED のまま残った (`ip -s link` は ERROR-ACTIVE・
        # bus-off 0 回、送受信ともエラー 0 なのに UI だけが異常を出し続ける)。
        # 「今も壊れているか」に答えられない指標で判定してはならない。
        #
        # 増減は CAN コントローラの送信エラーカウンタ (TEC) に合わせる ——
        # 失敗で +8、成功で -1、上限 255。config の `tx_error_threshold` は
        # 「CAN error_passive 境界」として書かれているので、同じ土俵の値でなければ
        # 意味が合わない。20Hz の再送なら 12 回続けて失敗して警告に入り、
        # 復旧後は約 2.4 秒で警告が消える。
        self._tx_error_score: dict[str, int] = {}
        self._rx_error_count: dict[str, int] = {}
        self._bus_off: dict[str, bool] = {}
        # 受信の口そのものが読めない状態。**送信の成否では外さない** ——
        # インタフェースが戻っても受信だけが死んでいる形を見逃さないため、
        # 外せるのは `bus.recv` が実際に返ったときだけ。
        self._rx_down: dict[str, bool] = {}
        self._rx_down_since: dict[str, float] = {}
        # 途絶の「立ち上がり」を数えたエピソード数。**`_record_rx_down` 側で
        # 数える** (立ち上がりの瞬間) —— `_clear_rx_down` 側で数えると、復帰
        # しないまま試合が終わったケース (=エピソードが 1 度も「復帰」しない)
        # を取りこぼす。試合単位のリセットは `reset_rx_down_episodes()` が持ち、
        # いつ呼ぶかは呼び出し元 (サーバー) が決める —— CANManager は「試合」を
        # 知らない (lib/match_state.py の責務との境界)。
        self._rx_down_episodes: dict[str, int] = {}
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
        self._tx_error_score.setdefault(name, 0)
        self._rx_error_count.setdefault(name, 0)
        self._bus_off.setdefault(name, False)
        self._rx_down.setdefault(name, False)
        self._rx_down_episodes.setdefault(name, 0)

    def add_motor(self, bus_name: str, motor: MotorDriver) -> None:
        """バスにモータを登録する。名前・CAN ID の重複は構成時に弾く。

        名前が衝突すると _motors は後勝ちで上書きされる一方 _bus_motors には両方残り、
        受信ループは孤児になった先勝ちドライバへフィードバックを配り続ける
        (motors は後勝ちを返すので、状態が永久に更新されないモータができる)。
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
            # CAN プロトコル層の送信失敗。カウンタを進めつつ、既存呼び出し元
            # (server.py の e_stop など) との互換性のため例外を再 raise する。
            self._record_tx_failure(bus_name)
            raise
        except Exception:
            # OS / executor / その他の異常も健全性カウンタに反映してから上位へ伝搬。
            self._record_tx_failure(bus_name)
            raise
        else:
            self._last_tx_at[bus_name] = time.time()
            self._record_tx_success(bus_name)

    def _record_tx_failure(self, bus_name: str) -> None:
        """送信 1 回の失敗を、累計 (表示用) と現況スコア (判定用) の両方へ積む。"""
        self._tx_error_count[bus_name] = self._tx_error_count.get(bus_name, 0) + 1
        self._tx_error_score[bus_name] = min(
            _TX_ERROR_SCORE_MAX, self._tx_error_score.get(bus_name, 0) + _TX_ERROR_SCORE_FAIL
        )

    def _record_tx_success(self, bus_name: str) -> None:
        """送信 1 回の成功でスコアを 1 だけ戻す。**累計は減らさない。**

        累計を減らすと「今日このバスで何回送信に失敗したか」が誰にも分からなくなる。
        判定と記録は別の役目なので、別の数で持つ。

        送信できたということはコントローラがバスから切り離されていないので、
        bus-off ラッチもここで外す。**外す経路が必要**なのは、`restart-ms` が 0 の
        インタフェースでは復帰通知 (CAN_ERR_RESTARTED) が届かないためで、
        それが無いと一度立った DOWN が二度と消えない ——
        いま直している `tx_error_count` と同じ壊れ方を bus-off 側に作ることになる。
        """
        self._tx_error_score[bus_name] = max(0, self._tx_error_score.get(bus_name, 0) - 1)
        self._bus_off[bus_name] = False

    async def _receive_loop(self, bus_name: str) -> None:
        """バス 1 本ぶんの受信ループ。**フレームの解釈失敗でも受信の断絶でも降りない。**

        **かつてはインタフェース断でこのタスクごと降りていた。** 「握り潰して回り続けても
        全速で失敗を繰り返すだけ」という判断だったが、その前提が実測で覆った ——
        SocketCAN の socket は `ip link` の down/up を跨いで生き残り、up 後は同じ socket
        のまま受信を再開する。つまり待って呼び直せば戻る。

        戻る経路が無いことの害は大きい。bus-off 復旧ウォッチドッグ
        (`scripts/can_watchdog.sh`) は復旧のたびに down/up を出すので、その 1 秒で
        受信タスクが永久に失われていた。``_tasks`` は誰も await しないため死は
        ログ 1 行にしか現れず、症状は「UI は接続中のまま全モータが STALE」——
        最も復旧しにくい壊れ方そのものだった。

        送信側 (`PeriodicTask._run`) は tick ごとに例外を捕まえて回り続け、復旧すれば
        自力で戻る。**受信側だけが片道だったのが不具合の本体で、ここで対称にする。**

        `bus.recv` を失敗させる事象は down/up だけではない —— `ip link set down`、
        CANable の抜き差し、`setup_can.sh` の再実行、udev 経由の `cbc-can.service`
        再起動はどれも同じ形で現れ、どれも 1 秒以内に戻る一過性のものである。

        再試行は待ってから行う。素の ``continue`` にすると、戻らないインタフェースを
        相手に全速で失敗を繰り返して 1 コアを食い潰し、**同じプロセスに同居している
        位置制御ループ (200Hz) と偏差監視 (50Hz) の周期まで巻き添えにする**。
        ログを ``LogThrottle`` へ通すのも同じ理由で、間引かないと同じ失敗が延々続いて
        本当の死因が流れる。

        黙って回り続けてはならないので、読めていないあいだは `_rx_down` を立てて
        `health()` から `BusHealth.DOWN` として見えるようにする。

        **1 通ごとにエグゼキュータへ往復してはならない (`_ReadableFd` を参照)。**
        往復のコストが受信可能な速度の上限を決めてしまい、C620 の 1kHz に追いつけずに
        カーネルがフレームを捨てる。捨てられた窓は M3508 の折り返し推定を狂わせる。
        """
        bus = self._buses[bus_name]
        motors = self._bus_motors[bus_name]
        retry_s = _RECV_RETRY_MIN_S
        readable = _ReadableFd.for_bus(bus)

        try:
            while True:
                try:
                    msgs = await self._receive_batch(bus, readable)
                except asyncio.CancelledError:
                    # `shutdown()` が畳む唯一の経路。握り潰すと停止できないタスクになる
                    raise
                except Exception:
                    # 受信 API 自体の失敗 (インタフェース断など)。降りずに待って呼び直す。
                    # 呼ぶたびに失敗するので、トレースバックは間引いて残す
                    self._record_rx_down(bus_name)
                    self._rx_log.exception(
                        f"{bus_name}:recv",
                        "CAN 受信に失敗しました。再試行を続けます (bus=%s)",
                        bus_name,
                    )
                    # **待つあいだは fd の監視を外す。** down した socket は「読める」と
                    # 報告され続けるので、載せたまま待つとイベントループが毎周期
                    # 起こされ、位置制御ループ (200Hz) の周期まで巻き添えにする
                    # (素の `continue` を禁じているのと同じ理由)
                    if readable is not None:
                        readable.suspend()
                    await asyncio.sleep(retry_s)
                    retry_s = min(_RECV_RETRY_MAX_S, retry_s * 2)
                    if readable is not None:
                        readable.resume()
                    continue

                retry_s = _RECV_RETRY_MIN_S

                # **1 通も取れなかったことを復帰の証拠にしてはならない。** python-can の
                # socketcan は select がタイムアウトした時点で socket に触れずに None を
                # 返すので、インタフェースが down していても None は返り続ける。実測でも
                # down 中に「30ms で受信が再開しました」と誤判定した。
                # 復帰を確定できるのは**実際に 1 通読めたとき**だけ。
                if not msgs:
                    continue

                self._clear_rx_down(bus_name)
                for msg in msgs:
                    if msg.is_error_frame:
                        self._handle_error_frame(bus_name, msg)
                        continue
                    self._dispatch_frame(bus_name, motors, msg)

                # **滞留が残っていても、次の 1 回は必ずイベントループへ戻る。**
                # 可読通知のコールバックはループが回らないと呼ばれず、上の取り込みと
                # 配布のあいだは 1 度も await しないので、`_ReadableFd.wait()` は
                # 必ず一度サスペンドする。`_RX_BATCH_MAX` はその 1 区切りで同期的に
                # 走る量の上限で、位置制御ループ (200Hz) を締め出さない幅に置いてある
        finally:
            if readable is not None:
                readable.close()

    async def _receive_batch(
        self, bus: can.Bus, readable: _ReadableFd | None
    ) -> Sequence[can.Message]:
        """この起床で取り込めるだけのフレームを 1 回で引き取る。

        ``readable`` を持つバス (SocketCAN) では、イベントループの fd 監視で
        「読める」まで待ってから**滞留を出し切る**。1 通ごとにエグゼキュータへ
        往復する形と比べて実測で 1 通あたり 168us → 47.5us、バス 1 本ぶんの CPU で
        37.5% → 10.6% になる (can_m3508, 2225 フレーム/秒)。

        ``fileno()`` を持たないバス (``--dry-run`` の virtual バス) では従来どおり
        エグゼキュータ経由で 1 通ずつ読む。**1 回の呼び出しで recv を 1 回しか
        呼ばない**性質はそのまま保つ ——ここで先読みすると、テストが並べた
        「次の 1 通」を意図しない時点で引き取ってしまう。

        取り込み中に失敗しても、**既に引き取った分は捨てない**。カーネルの
        バッファから出したフレームはもうどこにも残っていないので、ここで捨てると
        その窓は M3508 の折り返し推定から永久に失われる。失敗は次の呼び出しで
        同じように起きるので、報告が 1 周遅れるだけで済む。
        """
        if readable is None:
            msg: can.Message | None = await self._run_blocking(bus.recv, _RECV_TIMEOUT)
            return () if msg is None else (msg,)

        await readable.wait()

        msgs: list[can.Message] = []
        while len(msgs) < _RX_BATCH_MAX:
            try:
                msg = bus.recv(_RECV_NO_WAIT)
            except asyncio.CancelledError:
                raise
            except Exception:
                if msgs:
                    return msgs
                raise
            if msg is None:
                break
            msgs.append(msg)
        return msgs

    def _record_rx_down(self, bus_name: str) -> None:
        """受信が読めなくなったことを記録する。状態の遷移だけを 1 行残す。

        エピソード数もここ (立ち上がりの瞬間) で数える。`_clear_rx_down` 側の
        「復帰した瞬間」で数えると、復帰しないまま (`rx_down` が立ったまま)
        試合が終わったケースが 1 件も数えられない —— ワーク落下が起きた
        まさにその場合を取りこぼすことになり、本末転倒になる。
        """
        if not self._rx_down.get(bus_name, False):
            self._rx_down_since[bus_name] = time.time()
            self._rx_down_episodes[bus_name] = self._rx_down_episodes.get(bus_name, 0) + 1
            logger.error("CAN 受信が中断しました。復帰まで再試行を続けます (bus=%s)", bus_name)
        self._rx_down[bus_name] = True

    def _clear_rx_down(self, bus_name: str) -> None:
        """フレームを 1 通読めたことを記録する。**外せる経路はここだけ。**

        呼ぶのは実際に 1 通読めたときに限る (タイムアウトの None では呼ばない ——
        down 中も None は返り続けるので、復帰の証拠にならない)。送信の成否でも
        外さない。インタフェースが戻って送信だけが通り、受信は死んだままという形を
        見逃さないため。中断していた時間をログに残すのは、
        M3508 の累積角がその窓で飛びうる (`lib/drivers/m3508.py`) ので、後から
        「あのときの緊急停止はこれか」を突き合わせられるようにするため。
        """
        if self._rx_down.get(bus_name, False):
            since = self._rx_down_since.pop(bus_name, None)
            gap_s = time.time() - since if since is not None else 0.0
            logger.warning("CAN 受信が再開しました (bus=%s, 中断 %.2f 秒)", bus_name, gap_s)
        self._rx_down[bus_name] = False

    def reset_rx_down_episodes(self) -> None:
        """途絶エピソード数を全バス 0 に戻す。

        CANManager は「試合」という概念を知らない (層の境界は
        `lib/match_state.py` が試合の状態、`lib/can_manager.py` が CAN の状態を
        持つこと)。いつリセットするかは呼び出し元 (`lib/server.py`) が決める —
        現在は試合開始 (`match_start`) の成立時に呼んでいる。準備中 (機体の
        配線確認・動作確認) に踏んだ途絶を試合開始時点で洗い流すことで、
        試合中に見えるエピソード数が「この試合で実際に起きたこと」だけを
        表すようにするため (次の試合の準備が始まる `match_reset` まで待つと、
        準備フェーズで踏んだ途絶が次の試合の表示に紛れ込む)。

        現在進行中の途絶 (`_rx_down` が True のまま) はここでは触らない ——
        復帰していないバスを「無かったこと」にしてはならない。次に復帰した
        ときの `_clear_rx_down` はそのまま働き、次の立ち上がりからまた
        1 件目として数え直される。
        """
        for bus_name in self._rx_down_episodes:
            self._rx_down_episodes[bus_name] = 0

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
                    # 受信できている = コントローラはバスから切り離されていない。
                    # `restart-ms` が 0 のインタフェースは復帰通知を送らないので、
                    # 実通信を根拠に外す経路が無いと DOWN が永久に残る
                    self._bus_off[bus_name] = False
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

    def _handle_error_frame(self, bus_name: str, msg: can.Message) -> None:
        """SocketCAN のエラーフレームでバス状態を更新する。**モータへは配らない。**

        python-can の socketcan バスは既定でエラーフレームを受信する。これを
        通常のフレームとして `_dispatch_frame` へ流すと、エラー種別のビット列
        (0x40 = bus-off など) がそのまま arbitration_id として宛先判定に掛かる。
        DM3520 の MST_ID は 0x11 / 0x12 で、`CAN_ERR_TRX|CAN_ERR_TX_TIMEOUT` の
        0x11 や `CAN_ERR_TRX|CAN_ERR_LOSTARB` の 0x12 と衝突しうる —— つまり
        **バスのエラーがモータの実測角として取り込まれる**余地がある。

        **bus-off はここでしか観測できない。** `health()` は `bus.state` も見ているが、
        python-can 4.6 の `SocketcanBus` は `state` を実装しておらず、基底クラスの
        既定 (`BusState.ACTIVE`) が返る。つまり SocketCAN では ERROR / PASSIVE の
        分岐は永久に成立せず、bus-off を立てる経路は今まで 1 つも無かった
        (`_bus_off` はテストからしか True にならなかった)。

        **ただし現行の CANable2 はエラーフレームを 1 通も送ってこない** ——
        実測で確認済み (`docs/checks_and_health.md`)。落ちている間も `can state` は
        ERROR-ACTIVE のままで、`berr-reporting` も `GET_STATE` も未対応。
        つまりこの経路は実機では発火しない。**残しているのは、エラーフレームを送る
        アダプタ (別のブリッジや vcan) へ載せ替えたときに検出が消えないため**で、
        実機で bus-off が現れるのは送信失敗 (`tx_error_count` と送信スコア) だけである。

        鮮度 (`_last_rx_at`) は動かさない。エラーフレームは「モータからの応答」では
        ないので、これで途絶検出を止めると本物の途絶が見えなくなる。
        """
        if msg.arbitration_id & _CAN_ERR_BUSOFF:
            if not self._bus_off.get(bus_name, False):
                logger.error("CAN バスが bus-off になりました (bus=%s)", bus_name)
            self._bus_off[bus_name] = True
        if msg.arbitration_id & _CAN_ERR_RESTARTED:
            # `restart-ms` を設定したインタフェースだけが送ってくる。0 のままだと
            # カーネルは自動復帰しないので、この通知も永久に来ない
            logger.warning("CAN バスが bus-off から自動復帰しました (bus=%s)", bus_name)
            self._bus_off[bus_name] = False

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

    async def run(self) -> list[str]:
        """受信ループを立ててから起動時設定と励磁を行う。

        Returns:
            有効化できなかったモータ名。**呼び出し側は捨ててはならない** ——
            起動時の励磁失敗はここ以外に現れる場所が無く、捨てると症状は
            「指令しても動かない」だけになる。フィードバックは
            `QueryDrivenTargetRefresher` の問い合わせで流れ出すので、
            ヘルスは OK のまま無励磁だけが残る。
        """
        for bus_name in self._buses:
            task = asyncio.create_task(self._receive_loop(bus_name))
            self._tasks.append(task)
        return await self.initialize_motors()

    async def initialize_motors(self) -> list[str]:
        """各モータの起動時設定を宣言順に送り、続けて励磁を有効化する。

        **1 台の送信失敗で残りの起動を諦めない。** 素の for に並べると、最初の
        モータで CAN の送信が失敗しただけで以降のモータは設定も励磁も受けられず、
        しかも症状は「そのバスのモータが全部無励磁」でしかない。
        `shutdown()` や `main()` の後始末と同じ理由 (止める処理・立ち上げる処理を
        途中の 1 例外で丸ごと飛ばさない)。

        Returns:
            有効化できなかったモータ名。呼び出し側が操縦者へ見せる。
        """

        async def initialize_and_activate(motor_name: str) -> bool:
            # **設定と励磁はモータ単位で交互に送る。** 「全モータの設定 → 全モータの
            # 励磁」に組み替えると、EDULITE 05 / DM3520 が activation_steps で読む
            # 実測角が「設定直後」ではなく「他機の設定を挟んだ後」のものになる
            await self._send_steps(motor_name, self._motors[motor_name].initialization_steps())
            return await self.activate_motor(motor_name)

        return await self._activate_each_motor("起動時設定", initialize_and_activate)

    async def activate_motors(
        self,
        *,
        should_abort: Callable[[], bool] | None = None,
        feedback_timeout_s: float = _ACTIVATION_FEEDBACK_TIMEOUT_S,
    ) -> list[str]:
        """全モータの励磁を有効化する。緊急停止解除後の復帰にも使う。

        should_abort は「途中で有効化をやめるべきか」を返す。緊急停止が再び入った
        場合に、残りのモータへ enable を送らないための中断口。

        **1 台の送信失敗で残りの有効化を諦めない。** 緊急停止の原因がそのまま
        CAN の送信失敗を招いている場面 (専用バスに 1 台しか居ない DM3520 が電源を
        失うと ACK が返らず送信が全滅する) では、解除操作のたびに最初のモータで
        例外が上がり、**残りのモータへ enable が 1 通も飛ばない**。しかも
        `RobotServer._reactivate_motors` はこれをログに落とすだけなので、画面上は
        「解除できた」ように見えたまま機体が無励磁で取り残される。

        Returns:
            有効化できなかったモータ名 (中断で飛ばしたものを含む)。
        """

        async def activate(motor_name: str) -> bool:
            return await self.activate_motor(
                motor_name,
                should_abort=should_abort,
                feedback_timeout_s=feedback_timeout_s,
            )

        return await self._activate_each_motor("有効化", activate, should_abort=should_abort)

    async def clear_e_stop_latches(self) -> list[str]:
        """自作モタドラの緊急停止ラッチだけを外す。**中断口を持たない。**

        `activate_motors` の `should_abort` は「励磁」を途中でやめるための口で、
        ロボットを 1 台ずつ順に処理する `RobotServer._reactivate_motors` では
        **後ろのロボットが構造的に不利**になる —— 1 台目の処理中に緊急停止が
        再び入ると 2 台目へは解除フレームが 1 通も飛ばない。ラッチの外れない基板は
        緊急停止ビットを報告し続け、それを `_detect_board_e_stop` が拾って停止を
        再発動するので、解除操作のたびに同じ順序で同じロボットだけが取り残される
        (実機で発生。sub_hand の全基板が `kNeverCommanded` のまま戻らなくなった)。

        **ラッチ解除そのものでは機体は動かない**ので、中断する理由が無い:

        - 停止時に目標値が捨てられている (`DcChannel::stop()` は duty 0、
          `ServoChannel::stop()` は現在角で保持、`SolenoidChannel::stop()` は OFF)
        - ファーム側の `MotorSafety::isOutputAllowed()` が `everFed_` を要求するので、
          解除後に `SET_TARGET` を 1 通も受けるまで出力しない (仕様書 §5.4)

        本当の励磁を伴う EDULITE 05 / DM3520 / M3508 は対象外で、従来どおり
        `activate_motors` の中断ありの経路を通る。

        Returns:
            解除フレームを送れなかったモータ名。
        """

        async def clear(motor_name: str) -> bool:
            motor = self._motors[motor_name]
            if not isinstance(motor, GenericDriver):
                return True
            await self._send_steps(motor_name, motor.activation_steps())
            return True

        # 骨格は起動・励磁と共有する (「1 台の送信失敗で残りを諦めない」握りを
        # 書き写すと、片方だけ外れた状態を作れてしまう)。中断口は渡さない
        return await self._activate_each_motor("緊急停止ラッチの解除", clear)

    async def _activate_each_motor(
        self,
        what: str,
        action: Callable[[str], Awaitable[bool]],
        *,
        should_abort: Callable[[], bool] | None = None,
    ) -> list[str]:
        """全モータへ ``action`` を宣言順に適用し、失敗したモータ名を返す。

        **1 台の送信失敗で残りを諦めない。** 素の for に並べると、最初のモータで
        CAN の送信が失敗しただけで以降のモータは何も受け取れず、しかも症状は
        「そのバスのモータが全部無励磁」でしかない。起動経路と緊急停止解除の経路が
        同じ骨格をそれぞれ持っていると、片方だけ握りを外した状態を作れてしまう。

        中断は「次のモータへ進む直前」にだけ見る。送信の途中で降りると、
        設定だけ入って励磁されていないモータが残る。
        """
        inactive: list[str] = []
        motor_names = list(self._motors)
        for index, motor_name in enumerate(motor_names):
            if should_abort is not None and should_abort():
                logger.warning("モータの有効化を中断しました (残り: %s 以降)", motor_name)
                inactive.extend(motor_names[index:])
                return inactive
            try:
                if not await action(motor_name):
                    inactive.append(motor_name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("モータ '%s' の%sに失敗しました", motor_name, what)
                inactive.append(motor_name)
        return inactive

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
        能動的にモータを動かして確かめるのは統合動作確認シーケンス
        (sequences/motor_check.py) の仕事で、本メソッドは受信状態を読むだけ。
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
            # **ドライバが言うことを持っているなら状態も OK ではない。**
            # detail だけを載せて状態を OK に置くと、`summarizeMotors` が
            # 「All operational」を出して `SubsystemStatus` は畳んだままになり、
            # 報告はどの画面にも現れない ——「報告した」つもりの黙殺が成立する。
            # 出したい詳細があることと、状態が平常でないことを 1 つに束ねておく
            detail = motor.health_detail()
            warning = (
                motor.has_thermal_warning(thresholds.temp_warning_c)
                or motor.has_overcurrent_warning()
                or detail is not None
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
                    # **温度を測れない基板は 0.0 ではなく None を配る。**
                    # 自作モタドラはどの基板も温度センサを持たない (仕様書 §3.2) ので、
                    # `MotorState.temperature` の 0.0 は「測った値」ではなく制御経路が
                    # float を要求するための詰め物にすぎない。素通しにすると
                    # UI に 0.0℃ が並び、操縦者は「冷えている」と読む
                    temperature=(motor.state.temperature if motor.telemetry.temperature else None),
                    # ドライバ固有の事情 (M3508 の累積角再アンカーなど) をそのまま載せる。
                    # 状態 (OK/STALE) では表せない「値は届いているが意味が変わった」を
                    # 運ぶ唯一の口で、ここを None 固定に戻すと報告が画面から消える
                    detail=detail,
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
            tx_score = self._tx_error_score.get(bus_name, 0)
            rx_err = self._rx_error_count.get(bus_name, 0)
            bus_off = self._bus_off.get(bus_name, False)
            rx_down = self._rx_down.get(bus_name, False)
            rx_down_episodes = self._rx_down_episodes.get(bus_name, 0)
            # **バス名や `control_type` の文字列比較ではなく、ドライバ自身に聞く。**
            # `has_on_off_control()` の既定は False で、電磁弁用の GenericDriver
            # だけが True を返す。ここで名前判定 (例: "can_generic" かどうか) に
            # 倒すと、config で弁のバスを変えた瞬間に UI の判定が古いまま残る
            may_affect_workpiece = any(
                motor.has_on_off_control() for motor in self._bus_motors.get(bus_name, [])
            )

            # python-can の bus.state は実装しないインタフェースが多い (SocketCAN も
            # その 1 つで、基底クラスの既定 ACTIVE が返る) ため getattr で防御的に読む。
            # **SocketCAN では下の 2 つは永久に False になる。** 実バスの bus-off は
            # エラーフレーム (`_handle_error_frame`) が拾う。
            can_state = getattr(bus, "state", None)
            error_state = getattr(can.BusState, "ERROR", None)
            passive_state = getattr(can.BusState, "PASSIVE", None)
            is_error = can_state is not None and can_state == error_state
            is_passive = can_state is not None and can_state == passive_state

            # **判定に使うのは累計 (`tx_err`) ではなく現況スコア (`tx_score`)。**
            # 累計は単調増加なので、一度超えたバスは復旧しても DEGRADED から戻れない。
            # 表示には累計をそのまま載せる (この試合で何回失敗したかは残す)
            # **受信が読めていないバスは DOWN。** 受信ループが断絶を握って再試行を
            # 続けるようになったので、ここで出さないと「黙って回り続ける」だけになり、
            # 症状は全モータの STALE にしか現れない (原因がバスなのかモータなのか
            # 区別が付かない、いちばん切り分けにくい形)。
            if bus_off or is_error or rx_down:
                state = BusHealth.DOWN
            elif tx_score >= thresholds.tx_error_threshold or is_passive:
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
                    rx_down=rx_down,
                    rx_down_episodes=rx_down_episodes,
                    may_affect_workpiece=may_affect_workpiece,
                )
            )

        overall = HealthSnapshot.compute_overall(buses, motors)
        return HealthSnapshot(timestamp=now, overall=overall, buses=buses, motors=motors)
