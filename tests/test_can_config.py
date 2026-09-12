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
_UNIT_TEMPLATES = sorted((_PROJECT_ROOT / "scripts").glob("*.service"))
_DOC_URI_PREFIX = "file://@PROJECT_DIR@/"


def _load_module():
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
    # Linux の IFNAMSIZ は 16 (終端 NUL 込み)。15 文字を超える NAME= を書いた udev ルールは
    # 黙って無視され、そのバスだけ can0 等の番号名で上がる。
    def test_bus_name_longer_than_ifnamsiz_is_rejected(self, tmp_path) -> None:
        path = _write(tmp_path, _config({"a" * 16: _bus()}))

        with pytest.raises(can_config.ConfigError, match="長すぎます"):
            can_config.load_config(path)

    def test_bus_name_at_ifnamsiz_limit_is_accepted(self, tmp_path) -> None:
        path = _write(tmp_path, _config({"a" * 15: _bus()}))

        assert "a" * 15 in can_config.load_config(path)["buses"]

    @pytest.mark.parametrize("missing", ["vendor_id", "product_id"])
    def test_usb_id_is_required(self, tmp_path, missing: str) -> None:
        config = _config({"can_x": _bus()})
        del config["usb"][missing]
        path = _write(tmp_path, config)

        with pytest.raises(can_config.ConfigError, match=missing):
            can_config.load_config(path)

    def test_bitrate_is_required(self, tmp_path) -> None:
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
    def test_fixed_name_is_always_bound_to_a_serial(self) -> None:
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
        config = _config({"can_generic": _bus(serial)})

        rules = can_config.cmd_udev(config)

        assert 'NAME="can_generic"' not in rules
        assert "# can_generic: serial 未採取" in rules

    def test_hotplug_restart_does_not_block_udev(self) -> None:
        config = _config({"can_m3508": _bus("AAA")})

        rules = can_config.cmd_udev(config)

        assert 'RUN+="/usr/bin/systemctl --no-block restart cbc-can.service"' in rules

    def test_output_is_stable_across_runs(self) -> None:
        config = _config({"can_m3508": _bus("AAA"), "can_generic": _bus(can_config.UNASSIGNED)})

        assert can_config.cmd_udev(config) == can_config.cmd_udev(config)

    def test_generated_file_warns_against_hand_editing(self) -> None:
        rules = can_config.cmd_udev(_config({"can_m3508": _bus("AAA")}))

        assert "自動生成" in rules
        assert "scripts/install.sh" in rules


class TestListTsv:
    def test_every_line_has_five_tab_separated_fields(self) -> None:
        config = _config({"can_m3508": _bus("AAA"), "can_edulite": _bus("BBB")})

        for line in can_config.cmd_list(config, assigned_only=False).splitlines():
            fields = line.split("\t")
            assert len(fields) == 5, line
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
        config = _config({"can_generic": _bus(None), "can_edulite": _bus("   ")})

        lines = can_config.cmd_list(config, assigned_only=False).splitlines()

        assert [line.split("\t")[1] for line in lines] == ["TBD", "TBD"]

    def test_assigned_only_hides_unassigned_buses(self) -> None:
        config = _config({"can_m3508": _bus("AAA"), "can_generic": _bus(can_config.UNASSIGNED)})

        output = can_config.cmd_list(config, assigned_only=True)

        assert output.splitlines() == ["can_m3508\tAAA\t1000000\t1000\t100"]

    def test_txqueuelen_defaults_to_1000(self) -> None:
        config = _config({"can_m3508": _bus("AAA")})
        assert "txqueuelen" not in config["buses"]["can_m3508"]

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[3] == "1000"

    def test_explicit_txqueuelen_is_kept(self) -> None:
        config = _config({"can_m3508": _bus("AAA", txqueuelen=4000)})

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[3] == "4000"

    def test_restart_ms_defaults_to_a_nonzero_value(self) -> None:
        config = _config({"can_m3508": _bus("AAA")})
        assert "restart_ms" not in config["buses"]["can_m3508"]

        assert int(can_config.cmd_list(config, assigned_only=False).split("\t")[4]) > 0

    def test_explicit_restart_ms_is_kept(self) -> None:
        config = _config({"can_m3508": _bus("AAA", restart_ms=250)})

        assert can_config.cmd_list(config, assigned_only=False).split("\t")[4] == "250"


# 実機から個体識別情報 (serial / VID / PID) をまだ採取できていないバス。
# 採取したらここから外す。他のバスが黙って未採取へ落ちることは引き続き弾く。
_PENDING_BUSES = {"can_dc"}


class TestRealConfig:
    @staticmethod
    def _load() -> dict:
        return can_config.load_config(_REAL_CONFIG)

    @classmethod
    def _assigned(cls) -> dict:
        return {
            name: entry
            for name, entry in cls._load()["buses"].items()
            if can_config._is_assigned(entry)
        }

    def test_loads_without_error(self) -> None:
        assert self._load()["buses"]

    def test_all_buses_used_by_the_program_are_defined(self) -> None:
        system = yaml.safe_load(_SYSTEM_CONFIG.read_text())
        defined = set(self._load()["buses"])

        assert set(system["can_buses"].values()) <= defined

    def test_every_bus_has_a_serial(self) -> None:
        config = self._load()

        unassigned = {
            name for name, entry in config["buses"].items() if not can_config._is_assigned(entry)
        }
        assert unassigned <= _PENDING_BUSES

    def test_serials_are_unique(self) -> None:
        serials = [str(entry["serial"]).strip() for entry in self._assigned().values()]

        assert len(set(serials)) == len(serials)

    def test_setup_can_strict_can_bring_up_every_bus(self) -> None:
        config = self._load()
        listed = can_config.cmd_list(config, assigned_only=True).splitlines()

        assert len(listed) == len(self._assigned())
        for line in listed:
            name, serial, bitrate, txqueuelen, restart_ms = line.split("\t")
            assert serial != can_config.UNASSIGNED
            assert bitrate == "1000000"
            assert int(txqueuelen) >= 1000
            assert int(restart_ms) > 0
            assert len(name) <= can_config._IFNAME_MAX

    def test_udev_rules_cover_every_bus(self) -> None:
        rules = can_config.cmd_udev(self._load())

        for name, entry in self._assigned().items():
            # slcan は netdev を slcand が作るので、固定するのは tty の symlink のほう。
            if can_config._link(entry) == can_config.LINK_SLCAN:
                symlink = can_config.slcan_device(name).removeprefix("/dev/")
                assert f'SYMLINK+="{symlink}"' in rules
            else:
                assert f'NAME="{name}"' in rules

    def test_udev_restart_does_not_take_down_the_control_program(self) -> None:
        rules = can_config.cmd_udev(self._load())
        assert f"restart {can_config._SERVICE_NAME}" in rules, (
            "前提が崩れている: udev ルールが cbc-can.service を restart していない"
        )

        unit = (_PROJECT_ROOT / "scripts" / "cbc-control.service").read_text(encoding="utf-8")
        directives = [
            line.strip()
            for line in unit.splitlines()
            if line.strip().startswith(("Requires=", "Wants="))
        ]

        assert f"Requires={can_config._SERVICE_NAME}" not in directives
        assert f"Wants={can_config._SERVICE_NAME}" in directives


class TestUnitDocumentation:
    # glob が空振りすると下の parametrize がゼロ件で黙って通る。
    def test_unit_templates_are_collected(self) -> None:
        assert [path.name for path in _UNIT_TEMPLATES]

    # systemctl status / show -p Documentation から辿るリンクなので、文書を消しても
    # 改名しても unit 側は黙ったまま切れたパスを指し続ける。
    @pytest.mark.parametrize("unit", _UNIT_TEMPLATES, ids=lambda path: path.name)
    def test_documentation_points_at_an_existing_document(self, unit: pathlib.Path) -> None:
        for line in unit.read_text(encoding="utf-8").splitlines():
            if not line.strip().startswith("Documentation="):
                continue

            # man 5 systemd.unit: Documentation= は空白区切りで URI を複数取れる。
            for uri in line.split("=", 1)[1].split():
                if not uri.startswith(_DOC_URI_PREFIX):
                    continue

                relative = uri[len(_DOC_URI_PREFIX) :]
                assert (_PROJECT_ROOT / relative).exists(), (
                    f"{unit.name} の Documentation= が存在しない文書を指している: {relative}"
                )


class TestCommandLine:
    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_udev_stdout_matches_the_generated_rules(self) -> None:
        result = self._run("udev")

        assert result.returncode == 0
        assert result.stdout == can_config.cmd_udev(can_config.load_config(_REAL_CONFIG)) + "\n"

    def test_list_stdout_matches_the_generated_tsv(self) -> None:
        result = self._run("list", "--assigned-only")

        assert result.returncode == 0
        expected = can_config.cmd_list(can_config.load_config(_REAL_CONFIG), assigned_only=True)
        assert result.stdout == expected + "\n"

    def test_broken_config_exits_nonzero_without_output(self, tmp_path) -> None:
        path = _write(tmp_path, _config({"can_x": {"serial": "AAA"}}))

        result = self._run("list", "--config", str(path))

        assert result.returncode == 1
        assert result.stdout == ""
        assert "エラー" in result.stderr
