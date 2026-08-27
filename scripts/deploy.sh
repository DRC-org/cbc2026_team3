#!/usr/bin/env bash
#
# コードを更新したあとの反映を 1 コマンドにまとめる。
#   依存導入 (uv sync) → Web UI ビルド (pnpm build) → サービス再起動
#
# ビルドを cbc-control.service の ExecStartPre に置かないのは、会場でのビルド失敗が
# そのまま起動失敗になるため。直前まで動いていた web/dist があるのに立ち上がらない、
# という壊れ方を避ける。service は成果物を配るだけに留める。
#
# 使い方:
#   scripts/deploy.sh              # 一般ユーザーで実行する (sudo を付けない)
#   scripts/deploy.sh --no-restart # ビルドだけ行い、サービスには触らない

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="cbc-control.service"

RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

log()     { echo "[deploy] $*"; }
log_err() { echo "[deploy] $*" >&2; }

# root で実行すると .venv と node_modules が root 所有になり、以降の一般ユーザー
# での手動起動・ビルドが軒並み権限エラーになる
if [[ $EUID -eq 0 ]]; then
    log_err "sudo を付けずに実行してください (サービス再起動時だけ sudo を使います)"
    exit 1
fi

# uv / pnpm は mise 配下にあり、shim は非対話シェルの PATH に入らないことがある
for cmd in uv pnpm; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_err "${cmd} が PATH にありません。mise を有効化してから実行してください"
        log_err "  eval \"\$(mise activate bash)\"    # もしくは mise exec -- scripts/deploy.sh"
        exit 1
    fi
done

cd "$PROJECT_DIR"

log "Python 依存を同期 (uv sync --frozen)"
uv sync --frozen

log "Web UI の依存を導入 (pnpm install --frozen-lockfile)"
pnpm --dir web install --frozen-lockfile

log "Web UI をビルド (pnpm build)"
pnpm --dir web build

# service の ExecStartPre と同じ条件をここでも確かめる。ビルドが黙って
# 成果物を出さなかった場合、気付くのは起動失敗時ではなく今であるべき
if [[ ! -f "${PROJECT_DIR}/web/dist/index.html" ]]; then
    log_err "web/dist/index.html が生成されていません。ビルド結果を確認してください"
    exit 1
fi

if [[ $RESTART -eq 0 ]]; then
    log "ビルド完了 (--no-restart のためサービスには触れていません)"
    exit 0
fi

if ! systemctl list-unit-files "$SERVICE_NAME" >/dev/null 2>&1 \
    || ! systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    log "${SERVICE_NAME} は未インストールです。sudo scripts/install.sh を先に実行してください"
    exit 0
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "${SERVICE_NAME} を再起動"
    sudo systemctl restart "$SERVICE_NAME"
    sudo systemctl --no-pager --lines=20 status "$SERVICE_NAME" || true
else
    # 停止中に勝手に start しない。起動タイミングは操縦者が握る方針のため
    log "ビルド完了。起動する場合: sudo systemctl start ${SERVICE_NAME}"
fi
