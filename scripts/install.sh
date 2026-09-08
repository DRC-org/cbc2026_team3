#!/usr/bin/env bash
#
# udev ルールと systemd unit を配置する。
#   cbc-can.service          … CAN バス初期化。enable する (電源投入で up)
#   cbc-can-watchdog.service … bus-off 復旧ウォッチドッグ。enable する
#   cbc-control.service      … 中央制御プログラム + Web UI。enable しない (手動 start)
#
# 使い方:
#   sudo scripts/install.sh              # インストール / 設定変更の反映
#   sudo scripts/install.sh --uninstall  # 配置したファイルを撤去

set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG_PREFIX="[install]"
LOG_WARN_PREFIX="[install]"
LOG_ERR_PREFIX="[install]"

ORIGINAL_ARGS="$*"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage ;;
        *) log_err "不明な引数: $1"; exit 2 ;;
    esac
done

require_can_config

UDEV_RULE_PATH="$(can_config_path udev_rule_path)"
CAN_SERVICE_NAME="$(can_config_path service_name)"

UNITS=(
    "$CAN_SERVICE_NAME"
    "cbc-can-watchdog.service"
    "cbc-control.service"
)

AUTOSTART_UNITS=(
    "$CAN_SERVICE_NAME"
    "cbc-can-watchdog.service"
)

CONTROL_SERVICE_NAME="cbc-control.service"

# SocketCAN の bind に特権は要らない (up は root の cbc-can.service が済ませている)。
RUN_USER="${SUDO_USER:-}"

# `cmd > "$dest"` はシェルが先に dest を truncate するので、一時ファイルへ書いて
# から mv する (同一 FS の mv は atomic)。
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

# rename は down 状態でしか通らない。
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
