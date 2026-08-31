#!/usr/bin/env bash
#
# udev ルールと systemd unit を配置する。
#   cbc-can.service          … CAN バス初期化。enable する (電源投入で up)
#   cbc-can-watchdog.service … bus-off 復旧ウォッチドッグ。enable する
#   cbc-control.service      … 中央制御プログラム + Web UI。enable しない (手動 start)
#
# 制御プログラムを自動起動しないのは、電源投入だけで機体が通電・待機状態になる
# のを避けるため。起動タイミングは操縦者が握る。
#
# 使い方:
#   sudo scripts/install.sh              # インストール / 設定変更の反映
#   sudo scripts/install.sh --uninstall  # 配置したファイルを撤去

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# journal ではこの接頭辞で発生元を見分ける。install.sh は通常ログも警告も同じ
LOG_PREFIX="[install]"
LOG_WARN_PREFIX="[install]"
LOG_ERR_PREFIX="[install]"

ORIGINAL_ARGS="$*"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage ;;
        # 黙って無視すると `--uninstal` (typo) が**フルインストールを実行する**
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

require_can_config

# udev ルールのパスと、そのルールの RUN+= が restart する unit 名は
# can_config.py が持つ。ここに書き写すと、ルールだけが古い名前を指す形が作れる。
# **設定が壊れていても答えられる必要がある** (--uninstall が撤去先を知るため)
UDEV_RULE_PATH="$(can_config_path udev_rule_path)"
CAN_SERVICE_NAME="$(can_config_path service_name)"

# **配置する unit は 1 つの表でしか列挙しない。** 配置・enable・撤去を別々の場所に
# 書き並べると unit を 1 つ足すたびに 3 箇所へ手で書き足すことになり、実際に
# --uninstall だけ cbc-can-watchdog.service を書き忘れていた。撤去したつもりで
# ウォッチドッグが enable されたまま残り、Requires の相手 (cbc-can.service) が
# 消えているので起動不能な unit が居座る (しかも生き残った常駐は ip link を
# down/up し続ける)。
UNITS=(
    "$CAN_SERVICE_NAME"
    "cbc-can-watchdog.service"
    "cbc-control.service"
)

# 電源投入で自動起動してよい unit。どちらも機体を動かさない (up と down/up しか
# しない)。**cbc-control.service をここへ入れてはならない** —— 通電しただけで
# 機体が待機状態になる。
AUTOSTART_UNITS=(
    "$CAN_SERVICE_NAME"
    "cbc-can-watchdog.service"
)

CONTROL_SERVICE_NAME="cbc-control.service"

# 制御プログラムを走らせるユーザー。root で走らせると venv も CAN ソケットも
# root 所有になり、開発時の手動起動と実行条件が食い違う。SocketCAN の bind に
# 特権は要らない (up は root の cbc-can.service が済ませている)
RUN_USER="${SUDO_USER:-}"

# **生成物は一時ファイルへ書いてから mv する。** `cmd > "$dest"` はシェルが先に
# dest を truncate するので、生成が途中で失敗すると空 / 欠けたファイルが残る。
# udev ルールがそうなるとバス名は can0/can1/... の USB 列挙順へ戻り、C620 に
# EDULITE 用のコマンドが飛んでモータを壊す。同一 FS への mv は atomic なので、
# 「古いルールのまま」か「新しいルール」のどちらかにしかならない。
generate_to() {
    local dest="$1"; shift
    local tmp
    tmp="$(mktemp "${dest}.XXXXXX")"
    if ! "$@" >"$tmp"; then
        rm -f "$tmp"
        log_err "生成に失敗しました: ${dest}"
        return 1
    fi
    chmod 0644 "$tmp"
    mv -f "$tmp" "$dest"
}

if [[ $EUID -ne 0 ]]; then
    log_err "root 権限が必要です: sudo $0 ${ORIGINAL_ARGS}"
    exit 1
fi

if [[ $UNINSTALL -eq 1 ]]; then
    for unit in "${UNITS[@]}"; do
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/${unit}"
    done
    rm -f "$UDEV_RULE_PATH"
    systemctl daemon-reload
    udevadm control --reload-rules
    log_info "撤去しました。CAN インターフェース名は再起動後に can0 等へ戻ります。"
    exit 0
fi

if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    log_err "制御プログラムの実行ユーザーを特定できません。"
    log_err "一般ユーザーから sudo で実行してください: sudo $0 ${ORIGINAL_ARGS}"
    exit 1
fi

# 設定不正なら何も配置せずに止める
can_config_list >/dev/null

log_info "udev ルールを生成: ${UDEV_RULE_PATH}"
generate_to "$UDEV_RULE_PATH" "$PYTHON" "$CAN_CONFIG" udev

for unit in "${UNITS[@]}"; do
    log_info "systemd unit を配置: /etc/systemd/system/${unit}"
    generate_to "/etc/systemd/system/${unit}" sed \
        -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" \
        -e "s|@RUN_USER@|${RUN_USER}|g" \
        "${SCRIPT_DIR}/${unit}"
done
log_info "  (${CONTROL_SERVICE_NAME} の実行ユーザー: ${RUN_USER})"

# rename は down 状態でしか通らない。既存の can* を落としてから udev を再適用する。
for iface in /sys/class/net/can*; do
    [[ -e "$iface" ]] || continue
    name="$(basename "$iface")"
    log_info "既存インターフェースを down: ${name}"
    ip link set "$name" down 2>/dev/null || true
done

log_info "udev ルールを再読み込みして適用"
udevadm control --reload-rules
udevadm trigger --subsystem-match=net --action=add
udevadm settle

log_info "systemd を再読み込みして有効化"
systemctl daemon-reload
for unit in "${AUTOSTART_UNITS[@]}"; do
    systemctl enable "$unit"
    systemctl restart "$unit"
done

# cbc-control は enable も restart もしない。試合中に unit を入れ直しただけで
# 制御プログラムが落ちる (= 機体が止まる) のを避けるため、反映は次の start から
log_info "${CONTROL_SERVICE_NAME} を配置しました (自動起動は無効。手動 start で使う)"

log_info "完了。状態:"
for unit in "${AUTOSTART_UNITS[@]}"; do
    systemctl --no-pager --lines=5 status "$unit" || true
done

cat <<EOS

次の手順:
  scripts/deploy.sh              # 依存導入 + Web UI ビルド + サービス再起動
  sudo systemctl start ${CONTROL_SERVICE_NAME}    # 制御プログラム起動
  journalctl -u ${CONTROL_SERVICE_NAME} -f        # ログ追跡
EOS
