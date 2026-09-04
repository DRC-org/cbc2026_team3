"""scripts/tune_y_axis.py の試行の組み立てと単位換算。

**このツールは機体を動かす。** 実機で試すまで分からない部分 (応答そのもの) は
ここでは扱えないが、動かす前に確定する部分 —— どんな条件で何回動くか、
プロファイルの制限が指令単位へ正しく換算されるか、記録窓が移動を含みきるか ——
は実機なしで固定できる。ここを外すと、間違った条件のまま機体が動く。

CAN は要らない。読み込みと組み立ては純粋なロジックとして切り出してある。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import yaml

from lib.config_schema import load_robot_config
from lib.sequence.positions import MotionSpec, load_position_table

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _ROOT / "scripts" / "tune_y_axis.py"


def _load_module():
    """scripts/ はパッケージではないのでファイルパスから直接読み込む。

    tests/test_sync_probe.py と同じ理由と同じ作法 (exec_module の前に
    sys.modules へ入れる。@dataclass が型注釈の解決でモジュールを引くため)。
    """
    spec = importlib.util.spec_from_file_location("tune_y_axis", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tune = _load_module()

#: y_axis の実測値。左右で符号が逆 (機構的に逆回転で同一動作)
Y_SCALE = 55.0131

BASE = {"kp": 32.0, "ki": 10.0, "kd": 1.0, "sync_kp": 8.0, "output_limit": 2000.0}


def _args(*argv: str):
    return tune._parse_args(list(argv))


def _trials(*argv: str, base_motion: MotionSpec | None = None):
    return tune._build_trials(_args(*argv), dict(BASE), base_motion)


class TestProfileArguments:
    """``motion`` を書いていない軸は従来どおり。書いてあれば本番と同じ制御で測る。"""

    def test_引数もconfigも無ければステップ入力のまま(self) -> None:
        trials = _trials()

        assert len(trials) == 1
        assert trials[0].motion is None

    def test_位置定数のmotionが既定になる(self) -> None:
        """本番と違う軌道で測った結果を config へ書き戻させない。"""
        base = MotionSpec(max_velocity=50.0, max_acceleration=300.0, velocity_ff=1.0)

        trials = _trials(base_motion=base)

        assert trials[0].motion == base

    def test_引数はconfigのmotionを上書きする(self) -> None:
        base = MotionSpec(max_velocity=50.0, max_acceleration=300.0, velocity_ff=1.0)

        trials = _trials("--max-velocity", "80", base_motion=base)

        assert trials[0].motion == MotionSpec(
            max_velocity=80.0, max_acceleration=300.0, velocity_ff=1.0
        )

    def test_片方だけの指定は拒否する(self) -> None:
        """片方では軌道を組み立てられない。既定値で補うと「書いたのに効かない制限」。"""
        with pytest.raises(SystemExit, match="対で指定"):
            _trials("--max-velocity", "50")

        with pytest.raises(SystemExit, match="対で指定"):
            _trials("--max-acceleration", "300")

    def test_プロファイル無しのvelocity_ffは拒否する(self) -> None:
        """参照速度が存在しないので、黙って効かない値になる。"""
        with pytest.raises(SystemExit, match="velocity-ff"):
            _trials("--velocity-ff", "1.0")

    def test_不正な値は位置定数と同じ理由で拒否される(self) -> None:
        """検証は MotionSpec に委ねる。ツール側へ書き写すと片方だけ直せてしまう。"""
        with pytest.raises(SystemExit, match="max_velocity"):
            _trials("--max-velocity", "0", "--max-acceleration", "300")

        with pytest.raises(SystemExit, match="velocity_ff"):
            _trials("--max-velocity", "50", "--max-acceleration", "300", "--velocity-ff", "-1")


class TestSweep:
    """同一条件で値だけを変えた試行を並べるのがこのツールの存在意義。"""

    def test_max_velocityをスイープできる(self) -> None:
        trials = _trials("--max-velocity", "50,80,110", "--max-acceleration", "300")

        assert [t.motion.max_velocity for t in trials] == [50.0, 80.0, 110.0]
        # 変わってよいのは振っている 1 つだけ
        assert {t.motion.max_acceleration for t in trials} == {300.0}
        assert {(t.kp, t.ki, t.kd, t.sync_kp) for t in trials} == {(32.0, 10.0, 1.0, 8.0)}

    def test_max_accelerationをスイープできる(self) -> None:
        trials = _trials("--max-velocity", "50", "--max-acceleration", "300,600,1000")

        assert [t.motion.max_acceleration for t in trials] == [300.0, 600.0, 1000.0]

    def test_velocity_ffをスイープできる(self) -> None:
        base = MotionSpec(max_velocity=50.0, max_acceleration=300.0)

        trials = _trials("--velocity-ff", "0,1.0,1.6", base_motion=base)

        assert [t.motion.velocity_ff for t in trials] == [0.0, 1.0, 1.6]

    @pytest.mark.parametrize(
        "argv",
        [
            ("--kp", "24,32", "--max-velocity", "50,80", "--max-acceleration", "300"),
            ("--max-velocity", "50,80", "--max-acceleration", "300,600"),
            ("--sync-kp", "0,8", "--max-velocity", "50,80", "--max-acceleration", "300"),
            ("--output-limit", "2000,8000", "--kp", "24,32"),
            ("--output-limit", "2000,8000", "--max-velocity", "50,80", "--max-acceleration", "300"),
        ],
    )
    def test_2つ同時のスイープは拒否する(self, argv: tuple[str, ...]) -> None:
        """どちらが効いたのか読めない結果が組み合わせの数だけ機体を動かして並ぶ。"""
        with pytest.raises(SystemExit, match="1 つだけ"):
            _trials(*argv)

    def test_output_limitをスイープできる(self) -> None:
        """実運用ストロークを速くするときの律速はここ。振れないと config を書き換えて
        測り直すことになり、同一条件での比較にならない。"""
        trials = _trials("--output-limit", "2000,4000,8000")

        assert [t.output_limit for t in trials] == [2000.0, 4000.0, 8000.0]
        # 変わってよいのは振っている 1 つだけ
        assert {(t.kp, t.ki, t.kd, t.sync_kp) for t in trials} == {(32.0, 10.0, 1.0, 8.0)}

    def test_output_limitはC620のフルスケールで頭打ちになる(self) -> None:
        """ESC 側で頭打ちになる値をそのまま並べると、「上げたのに応答が変わらない」
        試行が比較表に出て、機構の限界と区別が付かなくなる。"""
        trials = _trials("--output-limit", "8000,20000")

        assert [t.output_limit for t in trials] == [8000.0, 16384.0]

    def test_output_limitを振らなければconfigの値になる(self) -> None:
        trials = _trials()

        assert [t.output_limit for t in trials] == [2000.0]

    def test_ラベルにoutput_limitが出る(self) -> None:
        """比較表で行を見分ける唯一の手掛かり。振った値が消えてはならない。"""
        trials = _trials("--output-limit", "2000,8000")

        labels = [t.label() for t in trials]
        assert "olim=2000" in labels[0]
        assert "olim=8000" in labels[1]

    def test_ラベルにプロファイルの値が出る(self) -> None:
        """比較表で行を見分ける唯一の手掛かりなので、振った値が消えてはならない。"""
        trials = _trials("--max-velocity", "50,80", "--max-acceleration", "300")

        labels = [t.label() for t in trials]
        assert "v=50" in labels[0]
        assert "v=80" in labels[1]
        assert "a=300" in labels[0]
        assert "vff=0" in labels[0]


class TestUnitConversion:
    """人間の単位 (mm) → モータの指令単位 (deg)。**符号は落とす。**"""

    def _cruise_velocity(self, profile, *, dt: float = 0.005, steps: int = 4000) -> float:
        """十分に長い移動で巡航に入ったときの参照速度。"""
        profile.reset(0.0)
        profile.retarget(1.0e6)
        peak = 0.0
        for _ in range(steps):
            _, velocity = profile.advance(dt)
            peak = max(peak, abs(velocity))
        return peak

    def test_指令単位へ換算した上限になる(self) -> None:
        motion = MotionSpec(max_velocity=50.0, max_acceleration=300.0)

        profile = tune._build_profile(motion, Y_SCALE)

        assert self._cruise_velocity(profile) == pytest.approx(50.0 * Y_SCALE)

    def test_逆回転側も同じ正の上限になる(self) -> None:
        """速度・加速度の制限は向きを持たない量。

        符号付きの ``scale`` を掛けると片側の上限が負値になり、プロファイルが
        組み立てられない (``TrapezoidalProfile`` が ValueError を投げる)。
        """
        motion = MotionSpec(max_velocity=50.0, max_acceleration=300.0)

        forward = tune._build_profile(motion, Y_SCALE)
        reverse = tune._build_profile(motion, -Y_SCALE)

        assert self._cruise_velocity(reverse) == pytest.approx(self._cruise_velocity(forward))

    def test_加速度も換算される(self) -> None:
        motion = MotionSpec(max_velocity=1.0e6, max_acceleration=300.0)
        profile = tune._build_profile(motion, -Y_SCALE)
        profile.reset(0.0)
        profile.retarget(1.0e9)

        _, velocity = profile.advance(0.005)

        assert velocity == pytest.approx(300.0 * Y_SCALE * 0.005)

    def test_表示は指令単位まで展開する(self) -> None:
        """``scale`` の取り違え (符号・桁) が画面のどこにも現れないのを防ぐ。"""
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "motors": {
                            "y_axis_r": {"scale": Y_SCALE},
                            "y_axis_l": {"scale": -Y_SCALE},
                        },
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="test",
        )

        text = tune._describe_profile(
            table.axis("y_axis"), MotionSpec(max_velocity=50.0, max_acceleration=300.0)
        )

        assert "v<=50mm/s" in text
        # 55.0131 deg/mm で換算した値。左右とも正
        assert "y_axis_r v<=2750.7 a<=16503.9" in text
        assert "y_axis_l v<=2750.7 a<=16503.9" in text


class TestDwellWindow:
    """記録窓が移動を含みきらないと、指標は一律に良い方へ嘘をつく。"""

    def _trial(self, **kwargs) -> object:
        motion = MotionSpec(max_velocity=50.0, max_acceleration=300.0, **kwargs)
        return tune.TrialConfig(
            kp=32.0, ki=10.0, kd=1.0, sync_kp=8.0, output_limit=2000.0, motion=motion
        )

    def test_移動が窓に収まらなければ拒否する(self) -> None:
        # 15mm を v=50 / a=300 で走ると 0.467s。既定の窓 1.5s には収まるので、
        # ここでは明らかに短い窓で見る
        with pytest.raises(SystemExit, match="--dwell"):
            tune._check_dwell([self._trial()], amplitude=15.0, dwell_s=0.3)

    def test_行き過ぎを読む余白が無い窓も拒否する(self) -> None:
        """行き過ぎは減速が終わってから出る。移動時間ぴったりでは測れない。"""
        trial = self._trial()
        exactly = trial.motion.duration_for(15.0)

        with pytest.raises(SystemExit, match="--dwell"):
            tune._check_dwell([trial], amplitude=15.0, dwell_s=exactly)

    def test_余白があれば通る(self) -> None:
        trial = self._trial()
        enough = trial.motion.duration_for(15.0) + tune.MOTION_DWELL_MARGIN_S

        tune._check_dwell([trial], amplitude=15.0, dwell_s=enough)

    def test_プロファイル無しの試行は対象外(self) -> None:
        """従来のステップ入力に「移動時間」は無い。既存の使い方を塞がない。"""
        tune._check_dwell(
            [tune.TrialConfig(kp=32.0, ki=10.0, kd=1.0, sync_kp=8.0, output_limit=2000.0)],
            amplitude=15.0,
            dwell_s=0.1,
        )


class TestSaturationReport:
    """飽和している間はゲインを変えても応答が変わらない。**必ず見えること。**"""

    def _trace(self, *, metrics: object | None, saturation: float):
        return tune.MotorTrace(
            name="y_axis_r",
            samples=[],
            metrics=metrics,
            peak_current=0.0,
            saturation_ratio=saturation,
        )

    def test_指標が出せない応答でも飽和率を出す(self) -> None:
        """動かなかった試行こそ飽和を疑う場面なので、指標経由にしない。"""
        line = tune._format_metrics(self._trace(metrics=None, saturation=1.0), "mm")

        assert "飽和 100%" in line

    def test_比較表の飽和欄も記録から数える(self, capsys: pytest.CaptureFixture) -> None:
        trial = tune.TrialConfig(kp=32.0, ki=10.0, kd=1.0, sync_kp=8.0, output_limit=2000.0)
        result = tune.StepResult(
            target=15.0,
            motors=[self._trace(metrics=None, saturation=0.75)],
            peak_deviation=0.1,
            final_deviation=0.05,
        )

        tune._print_comparison([(trial, [result])], "mm")

        assert "75%" in capsys.readouterr().out


def _read_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _axis_spec(motor_names: tuple[str, str] = ("y_axis_r", "y_axis_l")):
    """左右ペアの y_axis を 1 つだけ持つ最小の位置定数。"""
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "sync_tolerance": 2.0,
                    "motors": {
                        motor_names[0]: {"scale": Y_SCALE},
                        motor_names[1]: {"scale": -Y_SCALE},
                    },
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="test",
    ).axis("y_axis")


def _robot(buses_by_motor: dict[str, str]):
    """モータ名 → バス別名だけを与えた最小の robot config。"""
    return load_robot_config(
        {
            "robot_name": "main_hand",
            "motors": {
                name: {"driver": "m3508", "bus": bus, "can_id": index + 1}
                for index, (name, bus) in enumerate(buses_by_motor.items())
            },
        },
        source="test",
    )


class TestBusSelection:
    """開くのは **``--axis`` で指定した軸のモータが載っているバス** ただ 1 本。

    モータ構成に現れるバスの集合から 1 本選ぶと、複数バスを持つ config
    (本番の config/main_hand.yaml も config/bench/main_hand/ も 3 本) では
    選ばれるバスが実行ごとに変わる。対象軸の載っていないバスを開いた回は
    フィードバックが 1 通も届かず、症状は「同じコマンドなのに動いたり動かなかったり
    する」だけになる。
    """

    def _resolve(self, config: pathlib.Path, positions: pathlib.Path, axis: str = "y_axis") -> str:
        robot = load_robot_config(_read_yaml(config), source=str(config))
        table = load_position_table(_read_yaml(positions), source=str(positions))
        return tune._resolve_bus_alias(robot, table.axis(axis))

    def test_複数バスのベンチでもm3508のバスを選ぶ(self) -> None:
        """M3508 と EDULITE を同時に載せたセット。edulite_bus を開いた回は 1 台も動かない。"""
        bench = _ROOT / "config" / "bench" / "main_hand"

        alias = self._resolve(bench / "main_hand.yaml", bench / "main_hand_positions.yaml")

        assert alias == "m3508_bus"

    def test_1バスのチューニング用ベンチ(self) -> None:
        bench = _ROOT / "config" / "bench" / "y_axis_tuning"

        alias = self._resolve(bench / "main_hand.yaml", bench / "main_hand_positions.yaml")

        assert alias == "m3508_bus"

    def test_3バスの本番configも選べる(self) -> None:
        """本番 config を渡せることは手順 (docs/mechanism_handoff.md §3-2) の前提。"""
        alias = self._resolve(
            _ROOT / "config" / "main_hand.yaml",
            _ROOT / "config" / "main_hand_positions.yaml",
        )

        assert alias == "m3508_bus"

    def test_バスの並びが同じ2構成で対象軸の側を選ぶ(self) -> None:
        """**バスの集合から 1 本選ぶ実装は、ここで必ず落ちる。**

        set の反復順は文字列ハッシュと投入順で決まるので、1 例だけでは集合から
        選ぶ実装が偶然通る回がある。2 つの構成でバス名も**その並びも**同じにして
        対象軸だけを入れ替えれば、集合から選ぶ実装は同じ集合を同じ順で組み立てる
        —— 2 つへ必ず同じ答えを返すので、どちらかが落ちる。
        """
        spec = _axis_spec()
        on_alpha = _robot(
            {
                "y_axis_r": "alpha_bus",
                "y_axis_l": "alpha_bus",
                "other_r": "beta_bus",
                "other_l": "beta_bus",
            }
        )
        on_beta = _robot(
            {
                "other_r": "alpha_bus",
                "other_l": "alpha_bus",
                "y_axis_r": "beta_bus",
                "y_axis_l": "beta_bus",
            }
        )

        assert tune._resolve_bus_alias(on_alpha, spec) == "alpha_bus"
        assert tune._resolve_bus_alias(on_beta, spec) == "beta_bus"

    def test_左右が別バスなら落とす(self) -> None:
        """C620 の電流指令は 1 通に同一バスの 4 モータ分。別バスでは同時に指令できない。"""
        robot = _robot({"y_axis_r": "alpha_bus", "y_axis_l": "beta_bus"})

        with pytest.raises(SystemExit, match="別のバス"):
            tune._resolve_bus_alias(robot, _axis_spec())

    def test_対象軸のモータがconfigに居なければ落とす(self) -> None:
        """開くバスが決まらない。黙って別のバスを開くと 1 台も動かない試行が並ぶ。"""
        robot = _robot({"rotate_r": "edulite_bus"})

        with pytest.raises(SystemExit, match="y_axis_r"):
            tune._resolve_bus_alias(robot, _axis_spec())


def test_同梱の本番configがそのまま試行になる() -> None:
    """実運用ストロークを測るときに指す config。読めなければ現場で気付く術がない。"""
    path = _ROOT / "config" / "main_hand_positions.yaml"
    table = load_position_table(yaml.safe_load(path.read_text(encoding="utf-8")), source=str(path))

    trials = _trials(base_motion=table.axis("y_axis").motion)

    assert trials[0].motion is not None
    # 位置定数の値がそのまま届く。ここが既定値で埋まると本番と違う軌道で測ることになる
    assert trials[0].motion == table.axis("y_axis").motion
    tune._check_dwell(trials, amplitude=15.0, dwell_s=3.0)
