#!/usr/bin/env bash
#
# コードを更新したあとの反映を 1 コマンドにまとめる。
#   git pull --ff-only → 依存導入 (uv sync / pnpm install) → Web UI ビルド (pnpm build)
#   → CAN / slcand / ウォッチドッグ / 制御のサービスを再起動 (停止中・failed でも起動する)
#
# !!! pull と依存導入はネットワークを要求しうる。会場では --no-install を使う !!!
#
# 使い方:
#   scripts/deploy.sh              # 一般ユーザーで実行する (sudo を付けない)
#   scripts/deploy.sh --no-install # pull と依存導入を飛ばす (会場用。ネットワークに触れない)
#   scripts/deploy.sh --no-pull    # pull だけ飛ばす (手元のコードをそのまま反映する)
#   scripts/deploy.sh --no-restart # ビルドまで行い、サービスには触らない
#
# 再起動する場合は、サービスが動いているチェックアウト (cbc-control.service の
# WorkingDirectory) の deploy.sh を実行すること。別の worktree からでは止まる。

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG_PREFIX="[deploy]"
LOG_WARN_PREFIX="[deploy]"
LOG_ERR_PREFIX="[deploy]"

CONTROL_SERVICE="cbc-control.service"
WATCHDOG_SERVICE="cbc-can-watchdog.service"

# cbc-control.service は --port を渡さないので main.py の既定値で待ち受ける
PORT=8080

ORIGINAL_ARGS=("$@")
RESTART=1
INSTALL=1
PULL=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-restart) RESTART=0; shift ;;
        --no-install) INSTALL=0; PULL=0; shift ;;
        --no-pull) PULL=0; shift ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    log_err "sudo を付けずに実行してください (サービス再起動時だけ sudo を使います)"
    exit 1
fi

# uv / pnpm は mise 配下にあり、shim は非対話シェルの PATH に入らないことがある。
required_cmds=(pnpm)
[[ $INSTALL -eq 1 ]] && required_cmds+=(uv)
for cmd in "${required_cmds[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_err "${cmd} が PATH にありません。mise を有効化してから実行してください"
        log_err "  eval \"\$(mise activate bash)\"    # もしくは mise exec -- scripts/deploy.sh"
        exit 1
    fi
done

# 別ユーザーのプロセスは ss -p に PID が出ないので、その場合は "?" を返す。
port_listener_pids() {
    local out pids
    out="$(ss -ltnpH "sport = :${PORT}")"
    [[ -z "$out" ]] && return 0
    pids="$(grep -o 'pid=[0-9]*' <<< "$out" | cut -d= -f2 | sort -u)" || true
    echo "${pids:-?}"
}

warn_if_needs_install() {
    if [[ "${DEPLOY_NEEDS_INSTALL:-0}" == 1 ]]; then
        log_warn "取り込んだ変更が unit / udev の定義 (scripts/*.service, install.sh, config/can_buses.yaml) に触れています"
        log_warn "  -> 反映には sudo scripts/install.sh が必要です (全 CAN バスを down/up するので操縦中は避ける)"
    fi
}

cd "$PROJECT_DIR"

# pull とビルドの前に止める。別チェックアウトで走ると、ここを更新しても
# サービスは別のコードのまま再起動される。
if [[ $RESTART -eq 1 ]]; then
    if ! systemctl list-unit-files "$CONTROL_SERVICE" >/dev/null 2>&1 \
        || ! systemctl cat "$CONTROL_SERVICE" >/dev/null 2>&1; then
        log_err "${CONTROL_SERVICE} は未インストールです。sudo scripts/install.sh を先に実行してください"
        log_err "  (ビルドだけなら --no-restart)"
        exit 1
    fi
    service_dir="$(systemctl show -p WorkingDirectory --value "$CONTROL_SERVICE")"
    if [[ -z "$service_dir" ]]; then
        log_err "${CONTROL_SERVICE} の WorkingDirectory を読めません。sudo scripts/install.sh で入れ直してください"
        exit 1
    fi
    if [[ "$(realpath -m -- "$service_dir")" != "$(realpath -- "$PROJECT_DIR")" ]]; then
        log_err "サービスは ${service_dir} のコードで動いています (この deploy.sh は ${PROJECT_DIR})"
        log_err "  ここから実行すると ${PROJECT_DIR} を pull / ビルドし、サービスは別のコードのまま再起動されます"
        log_err "  -> bash ${service_dir}/scripts/deploy.sh ${ORIGINAL_ARGS[*]}"
        log_err "  (このチェックアウトのビルドだけなら --no-restart)"
        exit 1
    fi
fi

if [[ $PULL -eq 1 ]]; then
    if ! branch="$(git symbolic-ref --quiet --short HEAD)"; then
        log_err "HEAD が detached なので pull できません。ブランチを checkout するか --no-pull で実行してください"
        exit 1
    fi
    if ! upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
        log_err "${branch} に upstream がありません"
        log_err "  -> git branch --set-upstream-to=origin/${branch}  か  --no-pull で実行してください"
        exit 1
    fi
    old_head="$(git rev-parse HEAD)"
    log_info "git pull --ff-only (${branch} <- ${upstream})"
    if ! git pull --ff-only; then
        log_err "git pull に失敗しました。考えられる原因:"
        log_err "  - ローカルの ${branch} と ${upstream} が分岐している (git log --oneline --graph --all で確認)"
        log_err "  - 未コミットのローカル変更が取り込む変更と衝突する (git status で確認し、commit か stash)"
        log_err "  - ネットワークに出られない (会場なら --no-install)"
        exit 1
    fi
    new_head="$(git rev-parse HEAD)"
    if [[ "$old_head" == "$new_head" ]]; then
        log_info "更新なし"
    else
        count="$(git rev-list --count "${old_head}..${new_head}")"
        log_info "取り込んだコミット (${count} 件):"
        git --no-pager log --oneline -n 20 "${old_head}..${new_head}" | sed 's/^/    /'
        [[ $count -gt 20 ]] && log_info "    ... ほか $((count - 20)) 件"
        if [[ -n "$(git diff --name-only "$old_head" "$new_head" -- \
            'scripts/*.service' scripts/install.sh config/can_buses.yaml)" ]]; then
            export DEPLOY_NEEDS_INSTALL=1
        fi
        # 残りの手順は pull で更新された deploy.sh の中身で走らせる。
        exec bash "${SCRIPT_DIR}/deploy.sh" --no-pull "${ORIGINAL_ARGS[@]}"
    fi
fi

if [[ $INSTALL -eq 1 ]]; then
    log_info "Python 依存を同期 (uv sync --frozen)"
    uv sync --frozen

    log_info "Web UI の依存を導入 (pnpm install --frozen-lockfile)"
    pnpm --dir web install --frozen-lockfile
else
    if [[ ! -d "${PROJECT_DIR}/web/node_modules" ]]; then
        log_err "--no-install ですが web/node_modules がありません"
        log_err "  -> ネットワークのある場所で一度 scripts/deploy.sh を実行してください"
        exit 1
    fi
    if [[ ! -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
        log_warn ".venv がありません。ビルドはできますが cbc-control.service は起動しません"
        log_warn "  -> ネットワークのある場所で uv sync を済ませておくこと"
    fi
    log_info "pull と依存導入を飛ばしました (--no-install)"
fi

log_info "Web UI をビルド (pnpm build)"
pnpm --dir web build

if [[ ! -f "${PROJECT_DIR}/web/dist/index.html" ]]; then
    log_err "web/dist/index.html が生成されていません。ビルド結果を確認してください"
    exit 1
fi

if [[ $RESTART -eq 0 ]]; then
    log_info "ビルド完了 (--no-restart のためサービスには触れていません)"
    warn_if_needs_install
    exit 0
fi

require_can_config
CAN_SERVICE_NAME="$(can_config_path service_name)"
UNITS=("$CAN_SERVICE_NAME" "$WATCHDOG_SERVICE" "$CONTROL_SERVICE")

# slcan バスの slcand 常駐。対象は can_config.py の「採取済みか」が決める。
if ! slcan_units=$(can_config_slcan --assigned-only); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi
while IFS=$'\t' read -r _name _dev _speed unit; do
    [[ -z "${unit:-}" ]] && continue
    UNITS+=("$unit")
done <<< "$slcan_units"

main_pid="$(systemctl show -p MainPID --value "$CONTROL_SERVICE")"
for pid in $(port_listener_pids); do
    [[ "$pid" == "$main_pid" ]] && continue
    log_err "ポート ${PORT} を ${CONTROL_SERVICE} 以外のプロセスが使っています"
    log_err "  このままでは ${CONTROL_SERVICE} が Address already in use で起動できません"
    if [[ "$pid" == "?" ]]; then
        log_err "  (別ユーザーのプロセスで PID を読めません: sudo ss -ltnp 'sport = :${PORT}' で確認)"
    else
        ps -o pid,etime,cmd -p "$pid" >&2 || true
    fi
    log_err "  -> そのプロセスでロボットを操作している人がいないか確認してから止め (kill ${pid})、再実行してください"
    exit 1
done

# StartLimitBurst=3 に達した unit は reset-failed しないと start を受け付けない。
sudo systemctl reset-failed "${UNITS[@]}" 2>/dev/null || true

# 1 回の restart にまとめると systemd が After= に沿って順に起動する。
log_info "サービスを再起動: ${UNITS[*]}"
restart_ok=1
sudo systemctl restart "${UNITS[@]}" || restart_ok=0

# cbc-control は設定やポートの誤りだと 1 秒以内に落ち、RestartSec=2 で再起動を繰り返す。
sleep 3
failed_units=()
for unit in "${UNITS[@]}"; do
    systemctl is-active --quiet "$unit" || failed_units+=("$unit")
done

# Web サーバーは CAN の初期化のあとに listen するので、しばらく待ってから判定する。
port_ok=1
if systemctl is-active --quiet "$CONTROL_SERVICE"; then
    port_ok=0
    for _ in {1..15}; do
        main_pid="$(systemctl show -p MainPID --value "$CONTROL_SERVICE")"
        if [[ "$main_pid" != 0 && "$(port_listener_pids)" == "$main_pid" ]]; then
            port_ok=1
            break
        fi
        sleep 1
    done
fi

systemctl --no-pager --lines=15 status "$CONTROL_SERVICE" || true

if [[ $restart_ok -eq 0 || ${#failed_units[@]} -gt 0 || $port_ok -eq 0 ]]; then
    if [[ $restart_ok -eq 0 ]]; then
        log_err "sudo systemctl restart が失敗しました"
    fi
    for unit in "${failed_units[@]}"; do
        log_err "${unit} が active になっていません  -> journalctl -u ${unit} -n 30"
    done
    if [[ $port_ok -eq 0 ]]; then
        log_err "${CONTROL_SERVICE} がポート ${PORT} で待ち受けていません  -> journalctl -u ${CONTROL_SERVICE} -n 30"
    fi
    warn_if_needs_install
    exit 1
fi

log_info "${#UNITS[@]} サービスを再起動しました (${CONTROL_SERVICE} は PID ${main_pid} で :${PORT} を待ち受け中)"
warn_if_needs_install
