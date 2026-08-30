"""scripts/can_config.py の不変条件。

このモジュールは CAN バス命名の要である。`can0`/`can1`/`can2` は USB の列挙順で
入れ替わるため、番号のままでは C620 に EDULITE 用のコマンドが飛んでモータを壊す。
それを防ぐ唯一の仕掛けが「STM32 UID 由来の serial に固定名を紐付ける udev ルール」で、
そのルールを生成しているのがここ。つまり**このファイルの出力が壊れると、
壊れたことに気付けないまま誤ったバスへ電流指令が飛ぶ**。

さらに `scripts/setup_can.sh` は
  - `list` の TSV を `IFS=$'\t' read -r name serial bitrate txqueuelen` で読み
  - serial 列が文字列 `TBD` かどうかで未採取を判定し
  - `udev` の標準出力を配置済みルールと `diff -q` して同期を確認する
という形でこのモジュールの出力書式に直接依存している。試合前点検
(`setup_can.sh --strict`) の合否はここの出力そのものが根拠になるため、
書式もこのテストで固定する。
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "can_config.py"
_REAL_CONFIG = _PROJECT_ROOT / "config" / "can_buses.yaml"
_SYSTEM_CONFIG = _PROJECT_ROOT / "config" / "system.yaml"


def _load_module():
    """scripts/ はパッケージではないのでファイルパスから直接読み込む。

    systemd から `/usr/bin/python3 scripts/can_config.py` として起動される
    スタンドアロンスクリプトなので、パッケージ化して import 経路を作ると
    実運用の起動形態とテスト対象がずれる。
    """
    spec = importlib.util.spec_from_file_location("can_config", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


can_config = _load_module()


def _write(tmp_path: pathlib.Path, config: dict) -> pathlib.Path:
    path = tmp_path / "can_buses.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    return path


def _config(buses: dict) -> dict:
    return {"usb": {"vendor_id": "1d50", "product_id": "606f"}, "buses": buses}


def _bus(serial: str | None = "ABC123", **extra: object) -> dict:
    entry: dict = {"bitrate": 1000000}
    if serial is not None:
        entry["serial"] = serial
    entry.update(extra)
    return entry


class TestLoadConfig:
    """設定不正は「配置してから気付く」形にしてはならない。

    install.sh は `list` が成功して初めて udev ルールを書き出す。ここで弾けなかった
    不正は、そのまま /etc/udev/rules.d へ配置される。
    """

    def test_bus_name_longer_than_ifnamsiz_is_rejected(self, tmp_path) -> None:
        # Linux の IFNAMSIZ は 16 (終端 NUL 込み)。15 文字を超える NAME= を書いた
        # udev ルールは黙って無視され、そのバスだけ can0 等の番号名で上がる。
        # 「固定名にしたはずが番号名」という最も危険な状態になるため、生成前に落とす
        path = _write(tmp_path, _config({"a" * 16: _bus()}))

        with pytest.raises(can_config.ConfigError, match="長すぎます"):
            can_config.load_config(path)

    def test_bus_name_at_ifnamsiz_limit_is_accepted(self, tmp_path) -> None:
        path = _write(tmp_path, _config({"a" * 15: _bus()}))

        assert "a" * 15 in can_config.load_config(path)["buses"]

    @pytest.mark.parametrize("missing", ["vendor_id", "product_id"])
    def test_usb_id_is_required(self, tmp_path, missing: str) -> None:
        # vendor/product が欠けると ATTRS 条件の緩いルールが生成され、
        # CANable 以外の NIC にまで固定名を付けにいく余地が生まれる
        config = _config({"can_x": _bus()})
        del config["usb"][missing]
        path = _write(tmp_path, config)

        with pytest.raises(can_config.ConfigError, match=missing):
            can_config.load_config(path)

    def test_bitrate_is_required(self, tmp_path) -> None:
        # bitrate 不一致は「バスは上がるのに 1 フレームも通らない」形で出る。
        # 既定値で補うと誤った値のまま試合に入るので、必ず yaml に書かせる
        path = _write(tmp_path, _config({"can_x": {"serial": "ABC123"}}))

        with pytest.raises(can_config.ConfigError, match="bitrate"):
            can_config.load_config(path)

    def test_empty_buses_section_is_rejected(self, tmp_path) -> None:
        path = _write(tmp_path, {"usb": {"vendor_id": "1d50", "product_id": "606f"}, "buses": {}})

        with pytest.raises(can_config.ConfigError, match="buses"):
            can_config.load_config(path)

    def test_missing_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(can_config.ConfigError, match="見つかりません"):
            can_config.load_config(tmp_path / "no_such.yaml")


class TestUdevRules:
    """バス名が個体に固定されることを担保しているのはこの出力だけ。"""

    def test_fixed_name_is_always_bound_to_a_serial(self) -> None:
        # NAME= を伴う行に serial 条件が無ければ、最初に列挙された CANable が
        # その名前を取る。番号名と同じ「挿し順で決まる」状態に戻ってしまう
        config = _config({"can_m3508": _bus("AAA"), "can_edulite": _bus("BBB")})

        for line in can_config.cmd_udev(config).splitlines():
            if "NAME=" not in line:
                continue
            assert 'ATTRS{serial}=="' in line, line
            assert 'ATTRS{idVendor}=="1d50"' in line, line
            assert 'ATTRS{idProduct}=="606f"' in line, line

    def test_serial_and_name_pairing_follows_the_yaml(self) -> None:
        config = _config({"can_m3508": _bus("AAA"), "can_edulite": _bus("BBB")})

        rules = can_config.cmd_udev(config)

        assert 'ATTRS{serial}=="AAA", NAME="can_m3508"' in rules
        assert 'ATTRS{serial}=="BBB", NAME="can_edulite"' in rules

    @pytest.mark.parametrize("serial", [can_config.UNASSIGNED, "", "   ", None])
    def test_unassigned_bus_never_gets_a_rule(self, serial) -> None:
        # serial 未採取のバスに NAME= を出すと、条件が serial 以外だけになり
        # 「たまたま最初に見えた個体」がその名前を取る。生成しないのが正しい
        config = _config({"can_generic": _bus(serial)})

        rules = can_config.cmd_udev(config)

        assert 'NAME="can_generic"' not in rules
        # 消えたのではなく意図的に飛ばしたことが読み手に分かる必要がある
        assert "# can_generic: serial 未採取" in rules

    def test_hotplug_restart_does_not_block_udev(self) -> None:
        # RUN+= は udev のイベント処理を止める。--no-block を落とすと
        # setup_can.sh の完了まで udev が詰まり、抜き差しで復旧できなくなる
        config = _config({"can_m3508": _bus("AAA")})

        rules = can_config.cmd_udev(config)

        assert 'RUN+="/usr/bin/systemctl --no-block restart cbc-can.service"' in rules

    def test_output_is_stable_across_runs(self) -> None:
        # setup_can.sh は生成結果と配置済みファイルを diff -q で突き合わせ、
        # 差があれば strict で試合前点検を落とす。出力が揺れると常に落ちる
        config = _config({"can_m3508": _bus("AAA"), "can_generic": _bus(can_config.UNASSIGNED)})

        assert can_config.cmd_udev(config) == can_config.cmd_udev(config)

    def test_generated_file_warns_against_hand_editing(self) -> None:
        # 手で書き換えられると yaml との同期チェックが恒常的に落ち、
        # やがて警告そのものが無視されるようになる
        rules = can_config.cmd_udev(_config({"can_m3508": _bus("AAA")}))

        assert "自動生成" in rules
        assert "scripts/install.sh" in rules


class TestListTsv:
    """setup_can.sh が `while IFS=$'\t' read` で読む書式。"""

    def test_every_line_has_five_tab_separated_fields(self) -> None:
        config = _config({"can_m3508": _bus("AAA"), "can_edulite": _bus("BBB")})

        for line in can_config.cmd_list(config, assigned_only=False).splitlines():
            fields = line.split("\t")
            assert len(fields) == 5, line
            # シェル側は空フィールドを検出できない。空なら ip link に空文字が渡る
            assert all(fields), line
            assert " " not in line, line

    def test_bus_order_in_yaml_is_preserved(self) -> None:
        config = _config(
            {"can_m3508": _bus("AAA"), "can_edulite": _bus("BBB"), "can_generic": _bus("CCC")}
        )

        lines = can_config.cmd_list(config, assigned_only=False).splitlines()
        names = [line.split("\t")[0] for line in lines]

        assert names == ["can_m3508", "can_edulite", "can_generic"]

    def test_unassigned_serial_is_reported_as_the_literal_tbd(self) -> None:
        # setup_can.sh は `[[ "$serial" == "TBD" ]]` で未採取を数え、strict では
        # それを未完了として落とす。別の文字列や空欄になると 0 件と数えられ、
        # serial が入っていないまま試合前点検を通過してしまう
        config = _config({"can_generic": _bus(None), "can_edulite": _bus("   ")})

        lines = can_config.cmd_list(config, assigned_only=False).splitlines()

        assert [line.split("\t")[1] for line in lines] == ["TBD", "TBD"]

    def test_assigned_only_hides_unassigned_buses(self) -> None:
        # 未採取のバスを up 対象に混ぜると、実体のない名前を待って起動が伸びる
        config = _config({"can_m3508": _bus("AAA"), "can_generic": _bus(can_config.UNASSIGNED)})

        output = can_config.cmd_list(config, assigned_only=True)

        assert output.splitlines() == ["can_m3508\tAAA\t1000000\t1000\t100"]

    def test_txqueuelen_defaults_to_1000(self) -> None:
        # カーネル既定の 10 では 200Hz の位置制御ループで送信キューが溢れる
        config = _config({"can_m3508": _bus("AAA")})
        assert "txqueuelen" not in config["buses"]["can_m3508"]

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[3] == "1000"

    def test_explicit_txqueuelen_is_kept(self) -> None:
        config = _config({"can_m3508": _bus("AAA", txqueuelen=4000)})

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[3] == "4000"

    def test_restart_ms_defaults_to_a_nonzero_value(self) -> None:
        # **0 は「bus-off から自動復帰しない」の意味**で、カーネル既定でもある。
        # 既定で 0 が渡ると、一度 bus-off に落ちたバスは手動で down/up するまで
        # 送受信とも死んだままになる。バス上に 1 台しか居ない can_dm3520 では
        # 相手の電源断だけで ACK が返らなくなり、試合中に復旧不能になる
        config = _config({"can_m3508": _bus("AAA")})
        assert "restart_ms" not in config["buses"]["can_m3508"]

        assert int(can_config.cmd_list(config, assigned_only=False).split("\t")[4]) > 0

    def test_explicit_restart_ms_is_kept(self) -> None:
        config = _config({"can_m3508": _bus("AAA", restart_ms=250)})

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[4] == "250"


class TestRealConfig:
    """実際に試合で使う config/can_buses.yaml が要件を満たしているか。

    ここが落ちるのは「コードのバグ」ではなく「設定が試合で使えない状態」を意味する。
    """

    @staticmethod
    def _load() -> dict:
        return can_config.load_config(_REAL_CONFIG)

    def test_loads_without_error(self) -> None:
        assert self._load()["buses"]

    def test_all_buses_used_by_the_program_are_defined(self) -> None:
        # config/system.yaml の can_buses は python-can に渡す実インターフェース名。
        # 片方だけ改名すると、存在しないバスを開こうとして起動時に落ちる
        system = yaml.safe_load(_SYSTEM_CONFIG.read_text())
        defined = set(self._load()["buses"])

        assert set(system["can_buses"].values()) <= defined

    def test_every_bus_has_a_serial(self) -> None:
        # TBD が残っていると setup_can.sh --strict は落ちる。試合当日ではなく
        # 開発中に気付けるようにする
        config = self._load()

        unassigned = [
            name for name, entry in config["buses"].items() if not can_config._is_assigned(entry)
        ]
        assert unassigned == []

    def test_serials_are_unique(self) -> None:
        # 同じ serial を 2 バスに書くと両方のルールが同一デバイスに一致し、
        # 先に適用された名前だけが付いて残りのバスは永久に現れない。
        # コード側に検査は無いので、設定の側でこれを守る
        serials = [str(entry["serial"]).strip() for entry in self._load()["buses"].values()]

        assert len(set(serials)) == len(serials)

    def test_setup_can_strict_can_bring_up_every_bus(self) -> None:
        # --assigned-only の出力が全バスを含む = strict の「未採取 0 件」条件を満たす
        config = self._load()
        listed = can_config.cmd_list(config, assigned_only=True).splitlines()

        assert len(listed) == len(config["buses"])
        for line in listed:
            name, serial, bitrate, txqueuelen, restart_ms = line.split("\t")
            assert serial != can_config.UNASSIGNED
            # 全ドライバ (C620 / EDULITE / 自作) を 1Mbps で統一している
            assert bitrate == "1000000"
            assert int(txqueuelen) >= 1000
            # bus-off から自動復帰しないバスは、相手の電源断だけで試合中に死ぬ
            assert int(restart_ms) > 0
            assert len(name) <= can_config._IFNAME_MAX

    def test_udev_rules_cover_every_bus(self) -> None:
        rules = can_config.cmd_udev(self._load())

        for name in self._load()["buses"]:
            assert f'NAME="{name}"' in rules


class TestCommandLine:
    """install.sh / setup_can.sh はこのスクリプトを標準出力経由でしか使わない。"""

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_udev_stdout_matches_the_generated_rules(self) -> None:
        # install.sh は `can_config.py udev > /etc/udev/rules.d/...` で配置し、
        # setup_can.sh は同じ標準出力を配置済みファイルと diff する
        result = self._run("udev")

        assert result.returncode == 0
        assert result.stdout == can_config.cmd_udev(can_config.load_config(_REAL_CONFIG)) + "\n"

    def test_list_stdout_matches_the_generated_tsv(self) -> None:
        result = self._run("list", "--assigned-only")

        assert result.returncode == 0
        expected = can_config.cmd_list(can_config.load_config(_REAL_CONFIG), assigned_only=True)
        assert result.stdout == expected + "\n"

    def test_broken_config_exits_nonzero_without_output(self, tmp_path) -> None:
        # install.sh は `list` の成功を前提に udev ルールを書き出す。ここで 0 を
        # 返すと、空のルールファイルが配置されて全バスが番号名に戻る
        path = _write(tmp_path, _config({"can_x": {"serial": "AAA"}}))

        result = self._run("list", "--config", str(path))

        assert result.returncode == 1
        assert result.stdout == ""
        assert "エラー" in result.stderr
