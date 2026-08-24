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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAN_CONFIG="${SCRIPT_DIR}/can_config.py"

# systemd から root で起動されるため venv ではなくシステム python を使う
PYTHON="/usr/bin/python3"

STRICT=0
WAIT_SEC=0

log_ok()   { echo "[ OK ] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_err()  { echo "[ERR ] $*" >&2; }

usage() {
    sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict) STRICT=1; shift ;;
        --wait)   WAIT_SEC="${2:?--wait には秒数が必要です}"; shift 2 ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    IP=(ip)
else
    IP=(sudo ip)
fi

# デバイスが現れるまで待つ。USB の列挙は起動直後だと間に合わないことがあるため、
# systemd から呼ぶ際は --wait を指定する。
wait_for_iface() {
    local iface="$1" deadline
    deadline=$(( $(date +%s) + WAIT_SEC ))
    while :; do
        if ip link show "$iface" &>/dev/null; then
            return 0
        fi
        if [[ $(date +%s) -ge $deadline ]]; then
            return 1
        fi
        sleep 0.5
    done
}

setup_one() {
    local iface="$1" bitrate="$2" txqueuelen="$3"

    if ! wait_for_iface "$iface"; then
        return 2   # デバイス欠け。strict かどうかは呼び出し元が判断する
    fi

    # 設定変更は down 状態でしか通らない。up 済みでも失敗しないよう || true。
    "${IP[@]}" link set "$iface" down 2>/dev/null || true

    if ! "${IP[@]}" link set "$iface" type can bitrate "$bitrate"; then
        log_err "${iface}: bitrate ${bitrate} の設定に失敗"
        return 1
    fi

    if ! "${IP[@]}" link set "$iface" txqueuelen "$txqueuelen"; then
        log_err "${iface}: txqueuelen ${txqueuelen} の設定に失敗"
        return 1
    fi

    if ! "${IP[@]}" link set "$iface" up; then
        log_err "${iface}: up に失敗"
        return 1
    fi

    # up の成功は通信可能を意味しない。コントローラの状態まで確認する。
    local state
    state=$(ip -details link show "$iface" | grep -oE '(ERROR-ACTIVE|ERROR-WARNING|ERROR-PASSIVE|BUS-OFF|STOPPED)' | head -1)
    if [[ "$state" != "ERROR-ACTIVE" ]]; then
        log_err "${iface}: up 後の状態が異常です (${state:-不明})"
        return 1
    fi

    log_ok "${iface}: bitrate=${bitrate} txqueuelen=${txqueuelen} state=${state}"
    return 0
}

if [[ ! -f "$CAN_CONFIG" ]]; then
    log_err "can_config.py が見つかりません: ${CAN_CONFIG}"
    exit 1
fi

configured=0
up_count=0
missing=()
unassigned=()

# serial 未採取のバスを控えておく（strict では未完了として扱う）
while IFS=$'\t' read -r name serial _bitrate _txq; do
    [[ -z "${name:-}" ]] && continue
    if [[ "$serial" == "TBD" ]]; then
        unassigned+=("$name")
    fi
done < <("$PYTHON" "$CAN_CONFIG" list)

while IFS=$'\t' read -r name _serial bitrate txqueuelen; do
    [[ -z "${name:-}" ]] && continue
    configured=$(( configured + 1 ))

    set +e
    setup_one "$name" "$bitrate" "$txqueuelen"
    rc=$?
    set -e

    case $rc in
        0) up_count=$(( up_count + 1 )) ;;
        2) missing+=("$name") ;;
        *) exit 1 ;;   # デバイスはあるのに設定に失敗 → 常に異常
    esac
done < <("$PYTHON" "$CAN_CONFIG" list --assigned-only)

for name in "${unassigned[@]}"; do
    log_warn "${name}: serial 未採取 (config/can_buses.yaml が TBD)"
done

for name in "${missing[@]}"; do
    log_warn "${name}: デバイスが見つかりません"
done

echo "--- ${up_count}/${configured} バス起動 (未採取 ${#unassigned[@]} / 欠け ${#missing[@]}) ---"

if [[ $STRICT -eq 1 ]]; then
    if [[ ${#unassigned[@]} -gt 0 || ${#missing[@]} -gt 0 ]]; then
        log_err "strict モード: 全 CAN バスが揃っていません"
        exit 1
    fi
fi

exit 0
