"""CANManager / Sequence の利用側が他クラスの private を直接触っていないことを固定する。

この参照は機能を足すたびに増える (監査時点 5 箇所 → その後 8 箇所)。増えるたびに
「シーケンスのライフサイクルの一部がサーバー側にある」「モータ一覧の取り出し方が
呼び出し側ごとに違う」といった責務の漏れが積み上がり、シーケンス単体・CAN 層単体では
正しく振る舞えなくなる。振る舞いのテストだけでは *新しく増えた* 参照を止められないので、
参照そのものをここで禁止する。

対象は lib/server.py だけではない。CANManager を外から使うモジュールはどれも同じ形で
private を掴みうるので、利用側を列挙して同じ禁止を掛ける。
"""

from __future__ import annotations

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
_CONSUMER_MODULES = ("lib/server.py", "lib/motor_check.py")


@pytest.mark.parametrize("module_path", _CONSUMER_MODULES)
def test_consumer_does_not_touch_other_objects_private(module_path: str) -> None:
    source = (_REPO_ROOT / module_path).read_text(encoding="utf-8")
    found = _private_accesses(source)
    assert not found, (
        f"{module_path} が他オブジェクトの private を触っています: "
        f"{found} — 公開 API を足して置き換えてください"
    )
