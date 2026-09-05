"""本番モジュールが「外へ手を伸ばす」形を、振る舞いではなくソースの形で禁止する。

ここに集めた誤りはどれも、混入しても平常時のテストが全て緑のまま通る。
振る舞いのテストでは *新しく増えた 1 箇所* を止められないので、形そのものを禁じる。

**他オブジェクトの private 参照**は機能を足すたびに増える (監査時点 5 箇所 →
その後 8 箇所)。増えるたびに「シーケンスのライフサイクルの一部がサーバー側にある」
「モータ一覧の取り出し方が呼び出し側ごとに違う」といった責務の漏れが積み上がり、
シーケンス単体・CAN 層単体では正しく振る舞えなくなる。対象は lib/server.py だけでは
ない。CANManager を外から使うモジュールはどれも同じ形で private を掴みうるので、
利用側を列挙して同じ禁止を掛ける。

**暗黙のイベントループ取得**は lib/ 全体に掛ける。誤って書いても実行中のループが
ある限り同じループが返るため、混入した瞬間には何も起きない。
"""

from __future__ import annotations

import ast
import io
import pathlib
import tokenize

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 自分自身の状態だけは private のままでよい
_ALLOWED_OWNERS = frozenset({"self", "cls"})

_IGNORED_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.STRING,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
)


def _private_accesses(source: str) -> list[str]:
    """``名前._private`` の形を字句解析で拾う (コメント・文字列リテラルは対象外)。"""
    tokens = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in _IGNORED_TOKENS
    ]
    found: list[str] = []
    for owner, dot, attr in zip(tokens, tokens[1:], tokens[2:], strict=False):
        if owner.type is not tokenize.NAME or owner.string in _ALLOWED_OWNERS:
            continue
        if dot.string != "." or attr.type is not tokenize.NAME:
            continue
        if attr.string.startswith("_"):
            found.append(f"{owner.string}.{attr.string} (line {owner.start[0]})")
    return found


#: CANManager / Sequence を外から使うモジュール。ここに載せた分だけ再発を止められる
_CONSUMER_MODULES = (
    "lib/server.py",
    "lib/server_motor_check.py",
    "lib/ws_hub.py",
    "sequences/motor_check.py",
)


@pytest.mark.parametrize("module_path", _CONSUMER_MODULES)
def test_consumer_does_not_touch_other_objects_private(module_path: str) -> None:
    source = (_REPO_ROOT / module_path).read_text(encoding="utf-8")
    found = _private_accesses(source)
    assert not found, (
        f"{module_path} が他オブジェクトの private を触っています: "
        f"{found} — 公開 API を足して置き換えてください"
    )


def _lib_modules() -> list[pathlib.Path]:
    return sorted((_REPO_ROOT / "lib").rglob("*.py"))


@pytest.mark.parametrize("module_path", _lib_modules(), ids=lambda p: p.name)
def test_no_implicit_event_loop_acquisition(module_path: pathlib.Path) -> None:
    """``asyncio.get_event_loop()`` を使わない (``get_running_loop()`` を使う)。

    ``get_event_loop()`` は実行中のループが無い文脈で新しいループを黙って作る。
    そのループは誰も回さないので、そこへ ``create_task`` した WS 配信や
    ``run_in_executor`` した CAN 送信は「呼んだのに一度も走らない」形で消える。
    例外も警告も出ないため、試合中に起きても操縦者には何も見えない。
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "get_event_loop"
    ]
    assert not offenders, (
        f"{module_path.relative_to(_REPO_ROOT)} が asyncio.get_event_loop() を "
        f"使っています (line {offenders}) — asyncio.get_running_loop() へ置き換えてください"
    )
