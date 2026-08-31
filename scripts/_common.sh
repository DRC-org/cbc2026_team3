# scripts/*.sh が共有する土台。`source` して使う（単体では何もしない）。
#
# 実行属性は要らない。systemd は unit の ExecStart を絶対パスで叩き、各スクリプトが
# 自分の隣からこれを読むだけなので、root の install.sh から読めれば足りる (0644)。
# インストール時にどこかへコピーされることもない —— unit はリポジトリ内の
# @PROJECT_DIR@/scripts/*.sh をその場で実行する。
#
# **ログの接頭辞はここで統一しない。** journal では [ OK ] / [ WD ] / [install] /
# [deploy] で発生元の unit を見分けている。各スクリプトが source 後に
# LOG_PREFIX などを上書きする (関数は呼び出し時に展開するので順序は問わない)。

# source 元のスクリプト。usage() が自分のヘッダコメントを読むために使う。
MAIN_SCRIPT="${BASH_SOURCE[1]}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# systemd から root で起動されるため venv ではなくシステム python を使う。
# venv のパスに依存すると、起動順やユーザ切り替えで壊れる。
PYTHON="/usr/bin/python3"

CAN_CONFIG="${SCRIPT_DIR}/can_config.py"

LOG_PREFIX="[ -- ]"
LOG_WARN_PREFIX="[WARN]"
LOG_ERR_PREFIX="[ERR ]"

log_info() { echo "${LOG_PREFIX} $*"; }
log_warn() { echo "${LOG_WARN_PREFIX} $*" >&2; }
log_err()  { echo "${LOG_ERR_PREFIX} $*" >&2; }

# root なら sudo を挟まない。開発中は一般ユーザーから直接叩くので sudo が要る。
if [[ $EUID -eq 0 ]]; then
    IP=(ip)
else
    IP=(sudo ip)
fi

# ヘッダコメントをそのまま --help として出す。
#
# **行番号をハードコードしてはならない。** かつては `sed -n '3,12p'` のように
# 自分の何行目から何行目かを書いており、コメントを 1 行足すたびに末尾が欠けるか
# 無関係な行が混ざった (can_watchdog.sh は実際に空行を 1 行余計に出していた)。
# 連続するコメント行という構造で切れば、ヘッダを編集しても追従する。
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

# **数値オプションは必ず検証する。** 検証しないと不正値は遠く離れた場所で
# 表面化する —— `--wait abc` は算術展開のエラー、`--interval abc` は sleep の
# 失敗になり、set -e で**常駐ごと落ちる**。どちらも症状は「起動しない」だけで、
# 原因が引数にあることは journal から読めない。
require_number() {
    local flag="$1" value="${2-}"
    if [[ ! "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        log_err "${flag} には数値が必要です: '${value}'"
        exit 2
    fi
}

# 回数や秒数のうち算術展開・整数比較に渡るもの。小数を通すと `[[ 3 -ge 2.5 ]]` が
# その場で構文エラーになる。
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

# udev ルールのパスと service 名は can_config.py が単一情報源。かつては
# install.sh と setup_can.sh が同じ文字列を手で書き写し、「install.sh の
# UDEV_RULE_PATH と一致させること」とコメントで運用を要求していた (= 仕組みでは
# 担保されていなかった)。ずれると setup_can.sh は存在しないファイルを見て
# 「udev ルールが未配置」と警告し続ける —— install.sh は正しく配置しているのに。
can_config_path() {
    local key="$1" out
    out=$("$PYTHON" "$CAN_CONFIG" paths) || return 1
    awk -F'\t' -v k="$key" '$1 == k { print $2; found = 1 } END { exit !found }' <<< "$out"
}

# **プロセス置換 (`while ... done < <(cmd)` / `mapfile < <(cmd)`) は cmd の終了
# コードをどこにも伝えない。** set -e でも pipefail でも捕まらないので、
# can_config.py が落ちるとループが空回りしたまま先へ進む。必ず変数へ受けて
# 終了コードを見るための入口をここに 1 つ置く。
can_config_list() {
    "$PYTHON" "$CAN_CONFIG" list "$@"
}
