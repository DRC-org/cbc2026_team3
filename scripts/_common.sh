MAIN_SCRIPT="${BASH_SOURCE[1]}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PYTHON="/usr/bin/python3"

CAN_CONFIG="${SCRIPT_DIR}/can_config.py"

LOG_PREFIX="[ -- ]"
LOG_WARN_PREFIX="[WARN]"
LOG_ERR_PREFIX="[ERR ]"

log_info() { echo "${LOG_PREFIX} $*"; }
log_warn() { echo "${LOG_WARN_PREFIX} $*" >&2; }
log_err()  { echo "${LOG_ERR_PREFIX} $*" >&2; }

if [[ $EUID -eq 0 ]]; then
    IP=(ip)
else
    IP=(sudo ip)
fi

# 各スクリプトのヘッダコメントをそのまま --help として出す (行番号は持たない)。
usage() {
    awk '
        /^#!/ { next }
        /^#/  {
            sub(/^#[ ]?/, "")
            if (!started && $0 == "") next   # 先頭の空コメント行は落とす
            started = 1
            print
            next
        }
        { exit }                             # 最初の非コメント行で打ち切る
    ' "$MAIN_SCRIPT"
    exit 0
}

require_number() {
    local flag="$1" value="${2-}"
    if [[ ! "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        log_err "${flag} には数値が必要です: '${value}'"
        exit 2
    fi
}

require_integer() {
    local flag="$1" value="${2-}"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        log_err "${flag} には整数が必要です: '${value}'"
        exit 2
    fi
}

require_can_config() {
    if [[ ! -f "$CAN_CONFIG" ]]; then
        log_err "can_config.py が見つかりません: ${CAN_CONFIG}"
        exit 1
    fi
}

can_config_path() {
    local key="$1" out
    out=$("$PYTHON" "$CAN_CONFIG" paths) || return 1
    awk -F'\t' -v k="$key" '$1 == k { print $2; found = 1 } END { exit !found }' <<< "$out"
}

# bash のプロセス置換 (`while ... done < <(cmd)` / `mapfile`) は cmd の終了コードを
# どこにも伝えず set -e / pipefail でも捕まらないので、必ず変数へ受けて判定する。
can_config_list() {
    "$PYTHON" "$CAN_CONFIG" list "$@"
}
