#!/usr/bin/env bash
#
# CAN バスを config/can_buses.yaml の定義どおりに立ち上げる。
#
# 冪等: 何度実行しても同じ状態に収束する。既に up 済みなら一度 down してから
# 設定を適用する（ip link set type can は down 中しか受け付けないため）。
#
# 使い方:
#   scripts/setup_can.sh                 # 見つかったバスだけ up (開発用)
#   scripts/setup_can.sh --strict        # 全バス必須。欠けたら異常終了 (試合前点検)
#   scripts/setup_can.sh --wait 15       # デバイス出現を最大 15 秒待つ (systemd 用)
#   scripts/setup_can.sh --only can_dc   # そのバスだけ扱う
#
# slcan のバス (can_dc) の slcand を起こすのは cbc-slcand@<バス名>.service だけで、
# ここは slcand が作った netdev を up するところまでしか持たない。

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG_PREFIX="[ OK ]"

STRICT=0
WAIT_SEC=0
ONLY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict) STRICT=1; shift ;;
        --wait)   require_integer --wait "${2-}"; WAIT_SEC="$2"; shift 2 ;;
        --only)
            if [[ -z "${2-}" ]]; then log_err "--only にはバス名が必要です"; exit 2; fi
            ONLY="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

# デッドラインは全バスで共有する (バスごとに待つと実測で PC 起動が 31 秒まで伸びた)。
wait_for_iface() {
    local iface="$1"
    while :; do
        if ip link show "$iface" &>/dev/null; then
            return 0
        fi
        if [[ $(date +%s) -ge $WAIT_DEADLINE ]]; then
            return 1
        fi
        sleep 0.5
    done
}

LINK_STATE=""

# txqueuelen → up → 状態確認。gs_usb と slcand で共通。
bring_up() {
    local iface="$1" txqueuelen="$2" allow_unknown_state="$3"

    if ! "${IP[@]}" link set "$iface" txqueuelen "$txqueuelen"; then
        log_err "${iface}: txqueuelen ${txqueuelen} の設定に失敗"
        return 1
    fi

    if ! "${IP[@]}" link set "$iface" up; then
        log_err "${iface}: up に失敗"
        return 1
    fi

    LINK_STATE=$(ip -details link show "$iface" | grep -oE '(ERROR-ACTIVE|ERROR-WARNING|ERROR-PASSIVE|BUS-OFF|STOPPED)' | head -1)
    if [[ "$LINK_STATE" == "ERROR-ACTIVE" ]]; then
        return 0
    fi
    # slcan の netdev は CAN の状態を持たない実装があり、空欄が正常。
    if [[ -z "$LINK_STATE" && "$allow_unknown_state" == "1" ]]; then
        return 0
    fi

    log_err "${iface}: up 後の状態が異常です (${LINK_STATE:-不明})"
    return 1
}

setup_one_slcan() {
    local iface="$1" bitrate="$2" txqueuelen="$3"

    # netdev を作るのは cbc-slcand@<バス名>.service の slcand だけ。ここでは起こさない。
    if ! wait_for_iface "$iface"; then
        return 2
    fi

    if ! bring_up "$iface" "$txqueuelen" 1; then
        return 1
    fi

    log_info "${iface}: slcand ${SLCAN_DEV[$iface]} bitrate=${bitrate} txqueuelen=${txqueuelen} state=${LINK_STATE:-不明}"
    return 0
}

setup_one() {
    local iface="$1" bitrate="$2" txqueuelen="$3" restart_ms="$4"

    if ! wait_for_iface "$iface"; then
        return 2
    fi

    "${IP[@]}" link set "$iface" down 2>/dev/null || true

    if ! "${IP[@]}" link set "$iface" type can bitrate "$bitrate"; then
        log_err "${iface}: bitrate ${bitrate} の設定に失敗"
        return 1
    fi

    # do_set_mode を持たないドライバ (CANable2 の gs_usb) は restart-ms に
    # EOPNOTSUPP ("Device doesn't support restart from Bus Off") を返す。
    local restart_err
    if restart_err=$("${IP[@]}" link set "$iface" type can restart-ms "$restart_ms" 2>&1); then
        :
    elif [[ "$restart_err" == *"restart from Bus Off"* ]]; then
        restart_unsupported+=("$iface")
    else
        log_err "${iface}: restart-ms ${restart_ms} の設定に失敗: ${restart_err}"
        return 1
    fi

    if ! bring_up "$iface" "$txqueuelen" 0; then
        return 1
    fi

    local effective_restart
    effective_restart=$(ip -details link show "$iface" | grep -oE 'restart-ms [0-9]+' | head -1 | awk '{print $2}')
    log_info "${iface}: bitrate=${bitrate} txqueuelen=${txqueuelen} restart-ms=${effective_restart:-不明} state=${LINK_STATE}"
    return 0
}

require_can_config

if ! UDEV_RULE_PATH="$(can_config_path udev_rule_path)"; then
    log_err "udev ルールの配置先を取得できません: ${CAN_CONFIG}"
    exit 1
fi
udev_stale=0

check_udev_sync() {
    if [[ ! -f "$UDEV_RULE_PATH" ]]; then
        log_warn "udev ルールが未配置です: ${UDEV_RULE_PATH}"
        log_warn "  -> sudo scripts/install.sh を実行してください"
        udev_stale=1
        return
    fi
    if ! "$PYTHON" "$CAN_CONFIG" udev | diff -q - "$UDEV_RULE_PATH" &>/dev/null; then
        log_warn "config/can_buses.yaml と配置済み udev ルールが一致しません"
        log_warn "  -> sudo scripts/install.sh を再実行してください"
        udev_stale=1
    fi
}

check_udev_sync

WAIT_DEADLINE=$(( $(date +%s) + WAIT_SEC ))

configured=0
up_count=0
missing=()
unassigned=()
failed=()
restart_unsupported=()

if ! bus_list=$(can_config_list); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi
if ! assigned_list=$(can_config_list --assigned-only); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi
if ! slcan_list=$(can_config_slcan); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi

declare -A SLCAN_DEV=() SLCAN_UNIT=()
while IFS=$'\t' read -r name dev _speed unit; do
    [[ -z "${name:-}" ]] && continue
    SLCAN_DEV[$name]="$dev"
    SLCAN_UNIT[$name]="$unit"
done <<< "$slcan_list"

if [[ -n "$ONLY" ]]; then
    if ! awk -F'\t' -v k="$ONLY" '$1 == k { found = 1 } END { exit !found }' <<< "$bus_list"; then
        log_err "--only: そんな名前の CAN バスはありません: ${ONLY}"
        exit 2
    fi
    bus_list=$(awk -F'\t' -v k="$ONLY" '$1 == k' <<< "$bus_list")
    assigned_list=$(awk -F'\t' -v k="$ONLY" '$1 == k' <<< "$assigned_list")
fi

# 「採取済みか」の判定は can_config.py が単一情報源。ここへ条件を書き写さない。
assigned_names=" "
while IFS=$'\t' read -r name _rest; do
    [[ -z "${name:-}" ]] && continue
    assigned_names+="${name} "
done <<< "$assigned_list"

while IFS=$'\t' read -r name _rest; do
    [[ -z "${name:-}" ]] && continue
    if [[ "$assigned_names" != *" ${name} "* ]]; then
        unassigned+=("$name")
    fi
done <<< "$bus_list"

while IFS=$'\t' read -r name _serial bitrate txqueuelen restart_ms; do
    [[ -z "${name:-}" ]] && continue
    configured=$(( configured + 1 ))

    set +e
    if [[ -n "${SLCAN_DEV[$name]:-}" ]]; then
        setup_one_slcan "$name" "$bitrate" "$txqueuelen"
    else
        setup_one "$name" "$bitrate" "$txqueuelen" "$restart_ms"
    fi
    rc=$?
    set -e

    case $rc in
        0) up_count=$(( up_count + 1 )) ;;
        2) missing+=("$name") ;;
        *) failed+=("$name") ;;
    esac
done <<< "$assigned_list"

for name in "${unassigned[@]}"; do
    log_warn "${name}: 個体識別情報が未採取 (config/can_buses.yaml が TBD)"
    if [[ -n "${SLCAN_DEV[$name]:-}" ]]; then
        log_warn "  -> 基板を挿して 'lsusb' で VID:PID を、"
        log_warn "     'udevadm info -a -n /dev/ttyACM* | grep -m3 -E \"idVendor|idProduct|serial\"' で serial を採取"
    else
        log_warn "  -> 'udevadm info -a -n /sys/class/net/can* | grep -m1 ATTRS{serial}' で serial を採取"
    fi
done

for name in "${missing[@]}"; do
    if [[ -n "${SLCAN_DEV[$name]:-}" ]]; then
        log_warn "${name}: netdev がありません (slcand が繋がっていない / 基板が挿さっていない)"
        log_warn "  -> systemctl status ${SLCAN_UNIT[$name]}   (${SLCAN_DEV[$name]} の有無も見る)"
    else
        log_warn "${name}: デバイスが見つかりません"
    fi
done

for name in "${failed[@]}"; do
    log_err "${name}: デバイスはあるのに設定に失敗しました"
done

for name in "${restart_unsupported[@]}"; do
    log_warn "${name}: ドライバが bus-off からの自動復帰に非対応 (restart-ms=0 のまま)"
done
if [[ ${#restart_unsupported[@]} -gt 0 ]]; then
    log_warn "  -> bus-off へ落ちた場合は scripts/setup_can.sh の再実行 (down/up) でしか戻せません"
fi

echo "--- ${up_count}/${configured} バス起動 (未採取 ${#unassigned[@]} / 欠け ${#missing[@]} / 失敗 ${#failed[@]}) ---"

if [[ ${#failed[@]} -gt 0 ]]; then
    exit 1
fi

if [[ $STRICT -eq 1 ]]; then
    if [[ $configured -eq 0 ]]; then
        log_err "strict モード: 起動対象の CAN バスが 1 本もありません"
        exit 1
    fi
    if [[ ${#unassigned[@]} -gt 0 || ${#missing[@]} -gt 0 ]]; then
        log_err "strict モード: 全 CAN バスが揃っていません"
        exit 1
    fi
    if [[ $up_count -ne $configured ]]; then
        log_err "strict モード: ${up_count}/${configured} 本しか up していません"
        exit 1
    fi
    if [[ $udev_stale -eq 1 ]]; then
        log_err "strict モード: udev ルールが config/can_buses.yaml と同期していません"
        exit 1
    fi
fi

exit 0
