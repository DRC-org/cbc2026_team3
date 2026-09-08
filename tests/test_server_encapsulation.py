from __future__ import annotations

import ast
import io
import pathlib
import tokenize

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

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
