#!/usr/bin/env bash
#
# slcan バス 1 本の slcand を前景で抱える常駐。
#
# slcand を起こすのはこのスクリプトだけで、監視と再起動は systemd
# (cbc-slcand@<バス名>.service) が持つ。人が直接打つことは無い。
#
# 使い方:
#   scripts/slcand.sh can_dc     # tty を待つ -> slcand 起動 -> netdev を up -> 見張る
#
# tty が現れるまでは何も言わずに待つ (基板を挿していないときに journal を埋めないため)。

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG_PREFIX="[slcan]"
LOG_WARN_PREFIX="[slcan]"
LOG_ERR_PREFIX="[slcan]"

POLL_INTERVAL=0.5
# 取り残された netdev が消えるのを待つ上限 [周期]
STALE_NETDEV_TICKS=20
# netdev が現れて up するまでの待ち [秒]
LINK_WAIT=15

IFACE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage ;;
        -*) log_err "不明な引数: $1"; exit 2 ;;
        *)
            if [[ -n "$IFACE" ]]; then
                log_err "バス名は 1 つだけです: $1"
                exit 2
            fi
            IFACE="$1"; shift ;;
    esac
done

if [[ -z "$IFACE" ]]; then
    log_err "バス名を渡してください (例: scripts/slcand.sh can_dc)"
    exit 2
fi

require_can_config

if ! slcan_list=$(can_config_slcan); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi

DEV=""
SPEED=""
while IFS=$'\t' read -r name dev speed _unit; do
    [[ "${name:-}" == "$IFACE" ]] || continue
    DEV="$dev"
    SPEED="$speed"
done <<< "$slcan_list"

if [[ -z "$DEV" ]]; then
    log_err "${IFACE} は slcan のバスではありません (config/can_buses.yaml の link: を確認)"
    exit 2
fi

# 「採取済みか」の判定は can_config.py が単一情報源。ここへ条件を書き写さない。
if ! assigned_list=$(can_config_slcan --assigned-only); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi

assigned=0
while IFS=$'\t' read -r name _rest; do
    [[ "${name:-}" == "$IFACE" ]] && assigned=1
done <<< "$assigned_list"

if [[ $assigned -eq 0 ]]; then
    # udev ルールが生成されないので tty も生えない。失敗にはせず黙って待つ。
    log_warn "${IFACE}: 個体識別情報が未採取 (config/can_buses.yaml が TBD)。${DEV} を待ちます"
fi

while [[ ! -e "$DEV" ]]; do
    sleep "$POLL_INTERVAL"
done

if ! command -v slcand &>/dev/null; then
    log_err "${IFACE}: slcand がありません -> sudo apt install can-utils"
    exit 1
fi

# 取り残された slcand が tty を掴んだままだと netdev 名が衝突して起動できない。
"${SUDO[@]}" pkill -f "^slcand .* ${IFACE}\$" 2>/dev/null || true

waited=0
while ip link show "$IFACE" &>/dev/null; do
    if [[ $waited -ge $STALE_NETDEV_TICKS ]]; then
        log_warn "${IFACE}: netdev が残ったままです。このまま slcand を起動します"
        break
    fi
    sleep "$POLL_INTERVAL"
    waited=$(( waited + 1 ))
done

log_info "${IFACE}: slcand を起動します (${DEV} -s${SPEED})"
# -o は必須。基板は 'O' を受けるまでフレームを 1 通も送らない。
"${SUDO[@]}" slcand -F -o -c -s"$SPEED" "$DEV" "$IFACE" &
slcand_pid=$!

# netdev の up と txqueuelen は setup_can.sh が単一情報源。ここへ書き写さない。
if ! "${SCRIPT_DIR}/setup_can.sh" --only "$IFACE" --wait "$LINK_WAIT"; then
    log_warn "${IFACE}: netdev を up できませんでした"
fi

# USB を抜いても slcand は終了せず、無効になった fd を掴んだまま残る (実機で確認)。
# 残ると wait がずっと返らず unit の Restart も発動しないので、挿し直しても誰も
# tty を開かない = 基板が送信できないまま CAN エラー表示になる。netdev は tty と
# 一緒に消えるので、それを途絶の判定に使って slcand を落とし、繋ぎ直させる。
while kill -0 "$slcand_pid" 2>/dev/null; do
    if ! ip link show "$IFACE" &>/dev/null; then
        log_warn "${IFACE}: netdev が消えました。slcand を落として繋ぎ直します"
        "${SUDO[@]}" kill "$slcand_pid" 2>/dev/null || true
        break
    fi
    sleep "$POLL_INTERVAL"
done

status=0
wait "$slcand_pid" || status=$?
log_warn "${IFACE}: slcand が終了しました (status=${status})"
exit "$status"
