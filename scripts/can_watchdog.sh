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
#
# **なぜ要るか。** CANable2 (gs_usb) は bus-off から自動復帰しない。カーネルの
# restart-ms はドライバが do_set_mode を持たないため設定できず、ファームが持つ
# GS_CAN_FEATURE_BUS_OFF_RECOVERY もカーネル 7.0 の gs_usb は知らない。実測では
# 30 秒待っても 1 通も通らず、down/up でのみ復旧した。バス上に相手が 1 台でも
# 生きていれば ACK は返るので、実際に落ちうるのは相手が 2 台しか居ない
# can_dm3520 だけだが、そこは物理緊急停止で電源が落ちる経路そのものである。
#
# **なぜ `ip link` の state を見ないか。** 同じ実測で、落ちている間も can state は
# ERROR-ACTIVE のまま、bus-off / error-warn / error-pass のカウンタも 0 のまま、
# エラーフレームも 1 通も来なかった (berr-reporting も GET_STATE も未対応)。
# カーネルから見える範囲に異常が現れないので、送信の滞留でしか判定できない。
# 経緯と実測値は docs/checks_and_health.md にある。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAN_CONFIG="${SCRIPT_DIR}/can_config.py"

# systemd から root で起動されるため venv ではなくシステム python を使う
PYTHON="/usr/bin/python3"

INTERVAL=1
STALL_TICKS=3
MIN_RECOVER_INTERVAL=5
MAX_TICKS=0

# 連続復旧が続くときにログを出す間隔 [回]。相手が最初から居ないバス (試合前で
# 機体の電源が入っていない) では復旧しても滞留は解消しないため、毎回出すと
# journal がそれで埋まって本物の異常が埋もれる。
LOG_EVERY=12

log_info() { echo "[ WD ] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_err()  { echo "[ERR ] $*" >&2; }

usage() {
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)             INTERVAL="${2:?--interval には秒数が必要です}"; shift 2 ;;
        --stall-ticks)          STALL_TICKS="${2:?--stall-ticks には回数が必要です}"; shift 2 ;;
        --min-recover-interval) MIN_RECOVER_INTERVAL="${2:?--min-recover-interval には秒数が必要です}"; shift 2 ;;
        --max-ticks)            MAX_TICKS="${2:?--max-ticks には回数が必要です}"; shift 2 ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    IP=(ip)
else
    IP=(sudo ip)
fi

if [[ ! -f "$CAN_CONFIG" ]]; then
    log_err "can_config.py が見つかりません: ${CAN_CONFIG}"
    exit 1
fi

# バス一覧は can_config.py が単一情報源。ここに名前を書き写さない。
mapfile -t IFACES < <("$PYTHON" "$CAN_CONFIG" list --assigned-only | cut -f1)

declare -A stall_count=() last_tx=() last_recover=() recover_streak=()

# 送信できた累計フレーム数。デバイスが無ければ空を返す (呼び出し側が周期を捨てる)。
# `|| true` は必須 —— pipefail の下では `ip` の失敗がそのまま代入の失敗になり、
# set -e で常駐ごと落ちる。バスが 1 本抜けただけで監視全体が死ぬのは割に合わない。
tx_packets() {
    ip -s link show "$1" 2>/dev/null | awk '/TX:/{getline; print $2; exit}' || true
}

# qdisc に滞留しているパケット数。読めなければ 0 に倒す
# (デバイスが抜けた直後などに一瞬読めなくなる。それを滞留と数えない)。
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

    # 復旧しても直らないバス (相手が最初から居ない) では滞留が解消しないので、
    # 制限しないと down/up を回し続ける。害はログ量だけだが、その害が大きい。
    if [[ $(( now - ${last_recover[$iface]:-0} )) -lt $MIN_RECOVER_INTERVAL ]]; then
        return
    fi
    last_recover[$iface]=$now
    recover_streak[$iface]=$(( ${recover_streak[$iface]:-0} + 1 ))
    n=${recover_streak[$iface]}

    if [[ $n -eq 1 || $(( n % LOG_EVERY )) -eq 0 ]]; then
        log_warn "${iface}: 送信が ${STALL_TICKS} 周期進んでいません。down/up で復旧を試みます (${n} 回目)"
    fi

    # **bitrate と txqueuelen は down/up をまたいで保たれるので入れ直さない。**
    # 入れ直すと、その途中で失敗したときに元より悪い状態で残る。復旧は
    # 触る対象が少ないほどよい。
    "${IP[@]}" link set "$iface" down 2>/dev/null || true
    "${IP[@]}" link set "$iface" up 2>/dev/null || true

    # 復旧直後から数え直す。残したままだと次の周期で即座に再判定へ入り、
    # 実際に効いたかどうかを見る猶予が無くなる。
    stall_count[$iface]=0
}

tick() {
    local iface tx backlog
    for iface in "${IFACES[@]}"; do
        # 欠けているバスは復旧対象ではない (挿さっていないものは down/up できない)。
        # 挿さっていなければ tx_packets が空を返すので、それが唯一の判定になる。
        # 後から挿されれば次の周期から拾う。
        tx=$(tx_packets "$iface")
        [[ -n "$tx" ]] || continue
        backlog=$(qdisc_backlog "$iface")

        # 滞留 = キューに残っているのに tx_packets が 1 つも進まない。backlog だけで
        # 判定すると正常な連続送信の一瞬を拾い、tx_packets だけで判定すると
        # 「送るものが無いだけ」の平常時を異常と読む。両方が要る。
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
