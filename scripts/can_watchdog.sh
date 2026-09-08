#!/usr/bin/env bash
#
# bus-off で送信停止した CAN バスを down/up で復旧させる常駐。
#
# 使い方:
#   scripts/can_watchdog.sh                # 常駐 (cbc-can-watchdog.service)
#   scripts/can_watchdog.sh --max-ticks 5  # 5 周期で終了 (テスト・デバッグ用)
#
# オプション:
#   --interval SEC              ポーリング周期 [秒] (既定 1)
#   --stall-ticks N             復旧に踏み切るまでの連続滞留周期数 (既定 3)
#   --min-recover-interval SEC  復旧の最短間隔 [秒] (既定 5)
#   --max-ticks N               この周期数で終了。0 で無限 (既定 0)

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG_PREFIX="[ WD ]"

INTERVAL=1
STALL_TICKS=3
MIN_RECOVER_INTERVAL=5
MAX_TICKS=0

LOG_EVERY=12

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)
            require_number --interval "${2-}"; INTERVAL="$2"; shift 2 ;;
        --stall-ticks)
            require_integer --stall-ticks "${2-}"; STALL_TICKS="$2"; shift 2 ;;
        --min-recover-interval)
            require_integer --min-recover-interval "${2-}"; MIN_RECOVER_INTERVAL="$2"; shift 2 ;;
        --max-ticks)
            require_integer --max-ticks "${2-}"; MAX_TICKS="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

require_can_config

# 監視対象のバス名は scripts/can_config.py が単一情報源。ここへ書き写さない。
if ! bus_list=$(can_config_list --assigned-only); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi

IFACES=()
while IFS=$'\t' read -r name _rest; do
    [[ -z "${name:-}" ]] && continue
    IFACES+=("$name")
done <<< "$bus_list"

if [[ ${#IFACES[@]} -eq 0 ]]; then
    log_err "監視対象の CAN バスが 1 本もありません (config/can_buses.yaml の serial が全て未採取?)"
    exit 1
fi

declare -A stall_count=() last_tx=() last_recover=() recover_streak=()

# bash はシグナルハンドラを実行中の前景コマンドが終わってから走らせるので、
# down の最中に届いた TERM でも EXIT トラップが up を打ち直せる。
RECOVERING_IFACE=""

on_exit() {
    if [[ -n "$RECOVERING_IFACE" ]]; then
        "${IP[@]}" link set "$RECOVERING_IFACE" up 2>/dev/null || true
        RECOVERING_IFACE=""
    fi
}
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# `|| true` は必須 —— pipefail の下では `ip` の失敗が代入の失敗になり set -e で落ちる。
tx_packets() {
    ip -s link show "$1" 2>/dev/null | awk '/TX:/{getline; print $2; exit}' || true
}

qdisc_backlog() {
    local n
    n=$(tc -s qdisc show dev "$1" 2>/dev/null \
        | grep -m1 -oE 'backlog [0-9]+b [0-9]+p' \
        | grep -oE '[0-9]+p' | tr -d 'p')
    echo "${n:-0}"
}

recover() {
    local iface="$1" now n
    now=$(date +%s)

    if [[ $(( now - ${last_recover[$iface]:-0} )) -lt $MIN_RECOVER_INTERVAL ]]; then
        return
    fi
    last_recover[$iface]=$now
    recover_streak[$iface]=$(( ${recover_streak[$iface]:-0} + 1 ))
    n=${recover_streak[$iface]}

    if [[ $n -eq 1 || $(( n % LOG_EVERY )) -eq 0 ]]; then
        log_warn "${iface}: 送信が ${STALL_TICKS} 周期進んでいません。down/up で復旧を試みます (${n} 回目)"
    fi

    # bitrate と txqueuelen は down/up をまたいで保たれるので入れ直さない。
    RECOVERING_IFACE="$iface"
    "${IP[@]}" link set "$iface" down 2>/dev/null || true
    "${IP[@]}" link set "$iface" up 2>/dev/null || true
    RECOVERING_IFACE=""

    stall_count[$iface]=0
}

tick() {
    local iface tx backlog
    for iface in "${IFACES[@]}"; do
        tx=$(tx_packets "$iface")
        [[ -n "$tx" ]] || continue
        backlog=$(qdisc_backlog "$iface")

        if [[ "$backlog" -gt 0 && "$tx" == "${last_tx[$iface]:-}" ]]; then
            stall_count[$iface]=$(( ${stall_count[$iface]:-0} + 1 ))
        else
            if [[ ${recover_streak[$iface]:-0} -gt 0 ]]; then
                log_info "${iface}: 送信が再開しました (復旧 ${recover_streak[$iface]} 回)"
                recover_streak[$iface]=0
            fi
            stall_count[$iface]=0
        fi
        last_tx[$iface]=$tx

        if [[ ${stall_count[$iface]} -ge $STALL_TICKS ]]; then
            recover "$iface"
        fi
    done
}

log_info "監視開始: ${IFACES[*]} (周期 ${INTERVAL}s / 滞留 ${STALL_TICKS} 周期で復旧)"

tick_no=0
while :; do
    tick
    tick_no=$(( tick_no + 1 ))
    if [[ $MAX_TICKS -gt 0 && $tick_no -ge $MAX_TICKS ]]; then
        break
    fi
    sleep "$INTERVAL"
done
