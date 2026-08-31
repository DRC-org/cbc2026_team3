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
#
# デッドラインは全バスで共有する。バスごとに待つと、欠けが N 本あるとき
# N x WAIT_SEC 秒かかり PC 起動が実測で 31 秒まで伸びた。全 CANable は同じ
# USB 列挙で現れるため、待ち時間を分ける意味はない。
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

setup_one() {
    local iface="$1" bitrate="$2" txqueuelen="$3" restart_ms="$4"

    if ! wait_for_iface "$iface"; then
        return 2   # デバイス欠け。strict かどうかは呼び出し元が判断する
    fi

    # 設定変更は down 状態でしか通らない。up 済みでも失敗しないよう || true。
    "${IP[@]}" link set "$iface" down 2>/dev/null || true

    if ! "${IP[@]}" link set "$iface" type can bitrate "$bitrate"; then
        log_err "${iface}: bitrate ${bitrate} の設定に失敗"
        return 1
    fi

    # **restart-ms はドライバが対応していなければ設定できない。** カーネルは
    # do_set_mode を持たないドライバに対して EOPNOTSUPP
    # ("Device doesn't support restart from Bus Off") を返し、CANable2 が使う
    # gs_usb がこれに当たる (手動の `type can restart` も同じ理由で通らない)。
    # bitrate と同じ 1 コマンドに束ねると、対応していない環境では **1 本も
    # up できない** —— しかも bitrate 側は先に適用されるので、症状は
    # 「設定に失敗したのに bitrate だけ入っている」形になる。
    # 非対応なら restart-ms 0 のまま続行し、後段でまとめて警告する
    # (復旧手段が無いことは隠さないが、全バスを落とす理由にはしない)。
    local restart_err
    if restart_err=$("${IP[@]}" link set "$iface" type can restart-ms "$restart_ms" 2>&1); then
        :
    elif [[ "$restart_err" == *"restart from Bus Off"* ]]; then
        restart_unsupported+=("$iface")
    else
        log_err "${iface}: restart-ms ${restart_ms} の設定に失敗: ${restart_err}"
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

    # restart-ms は要求値ではなく実効値を読み戻して出す。要求値を出すと、
    # ドライバが受け付けなかった環境で「設定したつもり」のログだけが残る。
    local effective_restart
    effective_restart=$(ip -details link show "$iface" | grep -oE 'restart-ms [0-9]+' | head -1 | awk '{print $2}')
    log_ok "${iface}: bitrate=${bitrate} txqueuelen=${txqueuelen} restart-ms=${effective_restart:-不明} state=${state}"
    return 0
}

if [[ ! -f "$CAN_CONFIG" ]]; then
    log_err "can_config.py が見つかりません: ${CAN_CONFIG}"
    exit 1
fi

# can_buses.yaml を編集しても install.sh を再実行しなければ udev には反映されない。
# 反映漏れは「serial を書いたのに固定名にならない」形で現れ、原因が分かりにくいため
# ここで検出する。パスは install.sh の UDEV_RULE_PATH と一致させること。
UDEV_RULE_PATH="/etc/udev/rules.d/99-canable.rules"
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
restart_unsupported=()

# **プロセス置換 (`done < <(cmd)`) は cmd の終了コードをどこにも伝えない。**
# set -e でも pipefail でも捕まらないので、can_config.py が落ちるとループは
# 空回りし、configured=0 / unassigned=0 / missing=0 のまま先へ進む。その状態は
# strict の条件を 1 つも満たさないので、**1 本も up していないのに試合前点検が
# 成功終了する**。一度変数へ受けて終了コードを見るのが唯一の歯止めになる
# (今まではたまたま check_udev_sync の diff が同じ不正を拾っていただけ)。
if ! bus_list=$("$PYTHON" "$CAN_CONFIG" list); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi
if ! assigned_list=$("$PYTHON" "$CAN_CONFIG" list --assigned-only); then
    log_err "CAN バス定義を読めません: ${CAN_CONFIG}"
    exit 1
fi

# serial 未採取のバスを控えておく（strict では未完了として扱う）
while IFS=$'\t' read -r name serial _bitrate _txq _restart; do
    [[ -z "${name:-}" ]] && continue
    if [[ "$serial" == "TBD" ]]; then
        unassigned+=("$name")
    fi
done <<< "$bus_list"

while IFS=$'\t' read -r name _serial bitrate txqueuelen restart_ms; do
    [[ -z "${name:-}" ]] && continue
    configured=$(( configured + 1 ))

    set +e
    setup_one "$name" "$bitrate" "$txqueuelen" "$restart_ms"
    rc=$?
    set -e

    case $rc in
        0) up_count=$(( up_count + 1 )) ;;
        2) missing+=("$name") ;;
        *) exit 1 ;;   # デバイスはあるのに設定に失敗 → 常に異常
    esac
done <<< "$assigned_list"

for name in "${unassigned[@]}"; do
    log_warn "${name}: serial 未採取 (config/can_buses.yaml が TBD)"
done

for name in "${missing[@]}"; do
    log_warn "${name}: デバイスが見つかりません"
done

# 非対応は恒久的なドライバの性質であって、点検で直せる不備ではない。strict でも
# 失敗にしないのは、毎回必ず落ちる点検は点検として機能しないため。ただし
# 「bus-off に落ちたら自力では戻らない」ことは毎回目に入る場所に出す。
for name in "${restart_unsupported[@]}"; do
    log_warn "${name}: ドライバが bus-off からの自動復帰に非対応 (restart-ms=0 のまま)"
done
if [[ ${#restart_unsupported[@]} -gt 0 ]]; then
    log_warn "  -> bus-off へ落ちた場合は scripts/setup_can.sh の再実行 (down/up) でしか戻せません"
fi

echo "--- ${up_count}/${configured} バス起動 (未採取 ${#unassigned[@]} / 欠け ${#missing[@]}) ---"

if [[ $STRICT -eq 1 ]]; then
    # 「立ち上げるものが 1 本も無かった」は成功ではない。試合前点検が答えるのは
    # 「定義したバスが全部使えるか」なので、対象 0 本は必ず異常として扱う
    if [[ $configured -eq 0 ]]; then
        log_err "strict モード: 起動対象の CAN バスが 1 本もありません"
        exit 1
    fi
    if [[ ${#unassigned[@]} -gt 0 || ${#missing[@]} -gt 0 ]]; then
        log_err "strict モード: 全 CAN バスが揃っていません"
        exit 1
    fi
    # 欠けも未採取も無いのに up が足りないなら、途中で設定に失敗している
    if [[ $up_count -ne $configured ]]; then
        log_err "strict モード: ${up_count}/${configured} 本しか up していません"
        exit 1
    fi
    # バスが揃っていても定義と実態がズレていれば、意図しない個体に
    # 繋がっている可能性がある。試合前点検では失敗として扱う。
    if [[ $udev_stale -eq 1 ]]; then
        log_err "strict モード: udev ルールが config/can_buses.yaml と同期していません"
        exit 1
    fi
fi

exit 0
