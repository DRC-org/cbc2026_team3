from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.generic import CommandType, GenericDriver
from tests.feedback_frames import feed_generic

#: FEEDBACK Byte7 の予約ビット (仕様書 §3.2)。名前が付いていない = 誰も報告しない
_RESERVED_BITS = 0xE0


class TestCanIdRange:
    """仕様書 §2.2 のデバイス ID 範囲 (0x01〜0xFE)。

    M3508 (1〜4) と EDULITE 05 (0〜0xFF) には範囲検査があるのに、generic だけ
    無検査だった。範囲外は静かに壊れる:

    - ``0xFF`` は E_STOP ブロードキャストの予約 ID。``activation_steps()`` が
      緊急停止**解除**フレームを 0x0FF へ送り、共有 can_generic バス上の
      全基板のラッチをまとめて外す
    - ``0x1FF`` はコマンド種別のビットを侵食し、E_STOP が SET_TARGET として
      読まれるフレームになる
    - ``0x00`` は「DIP 設定忘れ」の予約。ファームは駆動を拒否する
    """

    @pytest.mark.parametrize("can_id", [0x01, 0x05, 0x7F, 0xFE])
    def test_ids_in_range_are_accepted(self, can_id: int):
        assert GenericDriver("m", can_id).can_id == can_id

    @pytest.mark.parametrize("can_id", [-1, 0x00, 0xFF, 0x100, 0x1FF])
    def test_ids_out_of_range_are_rejected(self, can_id: int):
        with pytest.raises(ValueError, match="can_id"):
            GenericDriver("m", can_id)


class TestBuildCanId:
    def test_build_can_id(self):
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0x01) == 0x001
        assert GenericDriver.build_can_id(CommandType.SET_TARGET, 0x01) == 0x101
        assert GenericDriver.build_can_id(CommandType.SET_PARAM, 0x10) == 0x210
        assert GenericDriver.build_can_id(CommandType.FEEDBACK, 0x01) == 0x301
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0xFF) == 0x0FF

    def test_e_stop_outranks_every_other_frame(self):
        """CAN の調停は ID が小さいほど優先。止めるフレームが追い越されてはならない。

        かつては E_STOP が 0b111 で、ブロードキャスト停止の 0x7FF は
        Standard ID 全 2048 個のうち最も優先度が低かった。
        """
        dev = 0x7F
        estop = GenericDriver.build_can_id(CommandType.E_STOP, dev)
        for other in (
            CommandType.SET_TARGET,
            CommandType.SET_PARAM,
            CommandType.FEEDBACK,
            CommandType.INFO,
        ):
            assert estop < GenericDriver.build_can_id(other, dev)
        # ブロードキャスト停止も、他のどのフレームより先に通ること
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0xFF) < GenericDriver.build_can_id(
            CommandType.SET_TARGET, 0x00
        )


class TestParseCanId:
    def test_parse_can_id(self):
        cmd, dev = GenericDriver.parse_can_id(0x101)
        assert cmd == CommandType.SET_TARGET
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x301)
        assert cmd == CommandType.FEEDBACK
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x0FF)
        assert cmd == CommandType.E_STOP
        assert dev == 0xFF


class TestEncodeTarget:
    """SET_TARGET (仕様書 §3.1)。Byte0=制御タイプ / Byte1-2=目標値 (int16)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_encode_target_position(self):
        msg = self.drv.encode_target(ControlMode.POSITION, 90.0)
        assert msg.arbitration_id == 0x101
        assert msg.is_extended_id is False
        # 途中に予約バイトを挟まないので DLC=3
        assert len(msg.data) == 3
        assert msg.data[0] == 0  # position
        assert struct.unpack_from("<h", msg.data, 1)[0] == 900  # 0.1deg 単位

    def test_encode_target_duty(self):
        msg = self.drv.encode_target(ControlMode.DUTY, 0.75)
        assert msg.data[0] == 2  # duty
        assert struct.unpack_from("<h", msg.data, 1)[0] == 7500  # 1/10000 単位

    def test_encode_target_keeps_sign(self):
        msg = self.drv.encode_target(ControlMode.DUTY, -0.75)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -7500

    def test_encode_target_saturates_instead_of_wrapping(self):
        """int16 をそのまま折り返すと +4000deg が負値に化け、基板が逆方向へ動く。"""
        msg = self.drv.encode_target(ControlMode.POSITION, 1e6)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 32767

        msg = self.drv.encode_target(ControlMode.POSITION, -1e6)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -32768

    def test_encode_target_rounds_instead_of_truncating(self):
        """切り捨てると 0.1deg 刻みの指令が 1 つ下へずれ続ける。

        yaml に書く値は 10 進小数なので、5.55deg のように刻みの中間へ落ちる。
        int() で落とすと常に手前側へ寄り、機構が目標に届かない。
        """
        msg = self.drv.encode_target(ControlMode.POSITION, 5.55)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 56

        msg = self.drv.encode_target(ControlMode.POSITION, -5.55)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -56

        # 0.3 は 2 進で正確に表せない。3000 になること (2999 では duty がずれる)
        msg = self.drv.encode_target(ControlMode.DUTY, 0.3)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 3000

    def test_encode_target_rejects_nan(self):
        """yaml に NaN が紛れても 0 に落とす。CAN 上には int16 しか流れない (§4)。"""
        msg = self.drv.encode_target(ControlMode.POSITION, float("nan"))
        assert struct.unpack_from("<h", msg.data, 1)[0] == 0


class TestDecodeFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_decode_feedback(self):
        # Byte0=状態フラグ / Byte1-2=位置 (0.1deg)
        data = bytearray([0x00])
        data.extend(struct.pack("<h", 1800))  # 180.0 deg

        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)

        assert state.position == pytest.approx(180.0)
        assert state.reached is False

    def test_decode_feedback_without_position(self):
        """位置を持たない基板は状態フラグ 1 バイトだけを送る (DLC=1)。"""
        msg = can.Message(arbitration_id=0x301, data=bytes([0x00]), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.position == pytest.approx(0.0)

    def test_decode_feedback_ignores_trailing_bytes(self):
        """Byte3 以降は予約。素通しにすると、他プロトコルの相乗りフレームが値になる。"""
        data = bytearray([0x00])
        data.extend(struct.pack("<h", 1800))
        data.extend(b"\xff\xff\xff")
        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.position == pytest.approx(180.0)
        assert state.current == pytest.approx(0.0)
        assert state.temperature == pytest.approx(0.0)
        # Byte4-6 は予約。**読んではならない** (仕様書 §3.2)。どちらの基板もセンサを
        # 持たないので、値を持ち込むと「常に 0 の電流・温度」が UI とヘルス判定へ流れ込む。
        # ここではあえて 0 以外を積んであり、素通しにすると 1500.0 / 45.0 が漏れて落ちる
        assert state.current == pytest.approx(0.0)
        assert state.temperature == pytest.approx(0.0)

    def test_decode_feedback_with_flags(self):
        data = bytearray([0b00000001])  # reached=True
        data.extend(struct.pack("<h", 0))

        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        assert self.drv.decode_feedback(msg).reached is True

        data[0] = 0b00000101  # reached=True, watchdog=True
        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        assert self.drv.decode_feedback(msg).reached is True


class TestMatchesFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_matches_feedback(self):
        msg = can.Message(arbitration_id=0x301, data=bytes(8), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is True

    def test_matches_feedback_wrong_device(self):
        msg = can.Message(arbitration_id=0x302, data=bytes(3), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is False

        msg_target = can.Message(arbitration_id=0x101, data=bytes(3), is_extended_id=False)
        assert self.drv.matches_feedback(msg_target) is False


class TestEncodeEStop:
    def test_encode_e_stop(self):
        msg = GenericDriver.encode_e_stop()
        # ブロードキャスト停止は他のどのフレームより優先度が高い ID (仕様書 §3.5)
        assert msg.arbitration_id == 0x0FF
        assert msg.is_extended_id is False
        assert msg.data == bytes(3)


class TestHealth:
    """ヘルスチェック判定 (Phase 6 段階②)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initial_flags_are_clear(self):
        # 初期化直後はどのフラグも立っていない
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False

    def test_reserved_sensor_bytes_never_raise_warnings(self):
        """予約バイトに何が載っていてもヘルス判定を動かさないこと。

        自作モタドラは電流センスも温度センサも持たない (仕様書 §3.2)。
        素通しにすると、他プロトコルの相乗りフレームや古いファームのゴミが
        そのまま過熱・過電流として読まれ、試合中に理由のない FAULT が出る。
        """
        feed_generic(self.drv, position=0.0, flags=_RESERVED_BITS, reserved=b"\xff\xff\xff")
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.has_thermal_warning(temp_warning_c=65.0) is False
        assert self.drv.has_thermal_fault(temp_critical_c=80.0) is False
        assert self.drv.is_fault() is False

    def test_sensor_and_reserved_bits_do_not_affect_health(self):
        # センサ入力は異常ではない。予約ビットもヘルス判定に影響させない
        self._feed(sensor=True, flags=_RESERVED_BITS)
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False


class TestSensorInput:
    """センサ入力 (FEEDBACK Byte7, 仕様書 §5.2)。原点合わせ用。

    センサは 1 個ずつ独立した CAN デバイスとして FEEDBACK を送るので、
    ドライバ 1 つがセンサ 1 個に対応する。
    """

    def setup_method(self):
        self.drv = GenericDriver("origin_sensor", 0x02)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initially_inactive(self):
        assert self.drv.sensor_active is False

    def test_sensor_bit_reports_input(self):
        self._feed(sensor=True)
        assert self.drv.sensor_active is True

    def test_clears_when_released(self):
        self._feed(sensor=True)
        self._feed()
        assert self.drv.sensor_active is False

    def test_reserved_bits_are_not_the_sensor(self):
        # 予約ビットを取り違えるとセンサが一度も反応しない
        self._feed(flags=_RESERVED_BITS)
        assert self.drv.sensor_active is False

    def test_contact_is_not_an_abnormality(self):
        """センサに触れているだけでヘルスや動作確認を止めてはならない。

        原点合わせは「触れさせる」操作なので、異常扱いにすると原点を取るたびに
        機体が FAULT になりシーケンスが止まる。基板は状態を報告するだけで、
        それをどう使うかは PC 側のシーケンスが決める (仕様書 §5.2)。
        """
        self._feed(sensor=True)
        assert self.drv.is_fault() is False
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.check_safety_error() is None

    def test_does_not_disturb_other_flags(self):
        # 同じ Byte7 に載るので、ビットを取り違えると緊急停止やウォッチドッグと混ざる
        self._feed(e_stop=True, watchdog=True, sensor=True)
        assert self.drv.sensor_active is True
        assert self.drv.e_stop_active is True
        assert self.drv.watchdog_active is True
        assert self.drv.device_id_unconfigured is False


class TestMotorCheck:
    """アクチュエータ動作確認 API (Phase 6 段階⑦)。"""

    def _feed(self, drv: GenericDriver, *, position_dg: int = 0, **kwargs: object) -> None:
        # フィードバックの位置は 0.1deg 単位。呼び出し側が生値で書いているため deg へ戻す
        feed_generic(drv, position=position_dg / 10.0, **kwargs)  # type: ignore[arg-type]

    def test_check_command_default_position(self):
        drv = GenericDriver("test_motor", 0x01)
        msg, context = drv.check_command(magnitude=0.1)
        # control_type デフォルトは POSITION
        assert msg.data[0] == 0  # position
        assert struct.unpack_from("<h", msg.data, 1)[0] == 1  # 0.1deg → 生値 1
        assert context.target == pytest.approx(0.1)
        assert context.mode is ControlMode.POSITION

    def test_check_command_velocity_mode(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        msg, context = drv.check_command(magnitude=50.0)
        assert msg.data[0] == 1  # velocity
        assert struct.unpack_from("<h", msg.data, 1)[0] == 500
        assert context.mode is ControlMode.VELOCITY

    def test_check_command_duty_mode(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.DUTY)
        msg, _context = drv.check_command(magnitude=0.3)
        assert msg.data[0] == 2  # duty
        assert struct.unpack_from("<h", msg.data, 1)[0] == 3000  # 1/10000 単位

    def test_evaluate_position_passed_when_reached_and_within_tolerance(self):
        drv = GenericDriver("test_motor", 0x01)
        _, context = drv.check_command(magnitude=10.0)
        # position=10.0deg, reached フラグ立ち上がり
        self._feed(drv, position_dg=100, reached=True)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is True
        assert detail is None

    def test_evaluate_position_failed_when_not_reached(self):
        drv = GenericDriver("test_motor", 0x01)
        _, context = drv.check_command(magnitude=10.0)
        # 目標 10.0deg に対して 5.0deg しか動いていない (許容 1.0 超え)
        self._feed(drv, position_dg=50)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None

    def test_evaluate_velocity_is_never_auto_judged(self):
        """速度もプロトコルから外れたので自動判定できない (仕様書 §3.2)。"""
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        _, context = drv.check_command(magnitude=100.0)
        self._feed(drv)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None
        assert "magnitude" in detail

    def test_evaluate_duty_is_never_auto_judged(self):
        """duty は自動判定できない。回転が観測されたように見えても PASSED にしない。

        duty を使うのは自作 DC モタドラだけで、その基板はエンコーダを持たず
        FEEDBACK の velocity は常に 0 (仕様書 §8)。ここで velocity を信じると、
        バス上の別フレームを取り違えた値で「動作確認 PASSED」を出しかねない。
        目視確認 (config/checklist.yaml) へ回すのが唯一正しい扱い。
        """
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.DUTY)
        _, context = drv.check_command(magnitude=0.3)
        self._feed(drv)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None
        # 失敗理由が配線不良に見えないよう、除外の手順まで書いてあること
        assert "magnitude" in detail

    def test_reset_after_check_sends_zero(self):
        drv = GenericDriver("test_motor", 0x01)
        msg = drv.reset_after_check()
        assert msg.data[0] == 0  # POSITION
        assert struct.unpack_from("<h", msg.data, 1)[0] == 0

    def test_reset_after_check_velocity_mode_sends_zero(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        msg = drv.reset_after_check()
        assert msg.data[0] == 1  # VELOCITY
        assert struct.unpack_from("<h", msg.data, 1)[0] == 0


class TestMatchesFeedbackRobustness:
    """受信ループを殺さないための頑健性 (仕様書 §2.1)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    @pytest.mark.parametrize("command_type", [0b100, 0b101, 0b110])
    def test_reserved_command_type_returns_false(self, command_type: int):
        # 予約コマンド種別は CommandType enum に無いが、例外を投げてはならない
        msg = can.Message(
            arbitration_id=(command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        assert self.drv.matches_feedback(msg) is False

    def test_extended_frame_returns_false(self):
        # 同一バスに Extended Frame の他プロトコルが相乗りしても壊れないこと
        msg = can.Message(arbitration_id=0x301, data=bytes(8), is_extended_id=True)
        assert self.drv.matches_feedback(msg) is False

    def test_try_parse_can_id_returns_none_for_reserved(self):
        assert GenericDriver.try_parse_can_id((0b101 << 8) | 0x01) is None
        assert GenericDriver.try_parse_can_id(0x301) == (CommandType.FEEDBACK, 0x01)

    def test_parse_can_id_still_raises_for_reserved(self):
        # 後方互換: 明示的に解析する経路では従来どおり例外を投げる
        # 予約は 0b101-0b111 (仕様書 §2.1)
        with pytest.raises(ValueError):
            GenericDriver.parse_can_id((0b101 << 8) | 0x01)


class TestEncodeEStopClear:
    """緊急停止の解除フレーム (仕様書 §3.5)。"""

    def test_encode_e_stop_clear_broadcast(self):
        msg = GenericDriver.encode_e_stop_clear()
        assert msg.arbitration_id == 0x0FF
        assert msg.is_extended_id is False
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert len(msg.data) == 3

    def test_encode_e_stop_clear_specific_device(self):
        msg = GenericDriver.encode_e_stop_clear(0x03)
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x03)
        assert msg.data[0] == 0x01

    def test_encode_e_stop_is_unchanged(self):
        # 停止フレームは Byte0=0x00 のまま (仕様書 §3.5)
        msg = GenericDriver.encode_e_stop()
        assert msg.arbitration_id == 0x0FF
        assert msg.data == bytes(3)


class TestActivationSteps:
    def test_activation_steps_sends_e_stop_clear_to_own_device(self):
        drv = GenericDriver("test_motor", 0x05)
        steps = drv.activation_steps()

        assert len(steps) == 1
        msg, delay = steps[0]
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x05)
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert delay == pytest.approx(0.0)

    def test_activation_does_not_require_fresh_feedback(self):
        # 解除フレームは目標値を変えず、ファームは解除後 target=0 から始める
        drv = GenericDriver("test_motor", 0x05)
        assert drv.requires_fresh_feedback_for_activation() is False


class TestSafetyStatusFlags:
    """FEEDBACK Byte7 の緊急停止 / ウォッチドッグ / デバイス ID 未設定 (仕様書 §3.2)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initial_flags_are_clear(self):
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False

    def test_e_stop_flag(self):
        self._feed(e_stop=True)
        assert self.drv.e_stop_active is True
        # 緊急停止中は異常ではないので FAULT にはしない
        assert self.drv.is_fault() is False
        assert "緊急停止" in (self.drv.check_safety_error() or "")

    def test_watchdog_flag(self):
        self._feed(watchdog=True)
        assert self.drv.watchdog_active is True
        assert self.drv.is_fault() is False
        assert "ウォッチドッグ" in (self.drv.check_safety_error() or "")

    def test_unconfigured_device_id_is_fault(self):
        """デバイス ID 未設定だけが FAULT。設定ミスは試合前に必ず気付く必要がある。"""
        self._feed(unconfigured_id=True)
        assert self.drv.device_id_unconfigured is True
        assert self.drv.is_fault() is True
        assert "デバイス ID" in (self.drv.check_safety_error() or "")

        # 新しいフレームで降りること (DIP を直したのに FAULT が残ると原因を追えない)
        self._feed()
        assert self.drv.is_fault() is False

    def test_no_safety_error_when_flags_clear(self):
        self._feed(reached=True)
        assert self.drv.check_safety_error() is None

    def test_flags_clear_on_recovery(self):
        self._feed(e_stop=True, watchdog=True, unconfigured_id=True)
        assert self.drv.is_fault() is True

        self._feed()
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False
        assert self.drv.is_fault() is False
        assert self.drv.check_safety_error() is None
