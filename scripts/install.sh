#!/usr/bin/env bash
#
# udev ルールと systemd unit を配置して CAN バスの自動起動を有効にする。
#
# 使い方:
#   sudo scripts/install.sh              # インストール / 設定変更の反映
#   sudo scripts/install.sh --uninstall  # 配置したファイルを撤去

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

UDEV_RULE_PATH="/etc/udev/rules.d/99-canable.rules"
SERVICE_NAME="cbc-can.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
PYTHON="/usr/bin/python3"

log() { echo "[install] $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "root 権限が必要です: sudo $0 $*" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_PATH" "$UDEV_RULE_PATH"
    systemctl daemon-reload
    udevadm control --reload-rules
    log "撤去しました。CAN インターフェース名は再起動後に can0 等へ戻ります。"
    exit 0
fi

# 設定不正なら何も配置せずに止める
"$PYTHON" "${SCRIPT_DIR}/can_config.py" list >/dev/null

log "udev ルールを生成: ${UDEV_RULE_PATH}"
"$PYTHON" "${SCRIPT_DIR}/can_config.py" udev > "$UDEV_RULE_PATH"

log "systemd unit を配置: ${SERVICE_PATH}"
sed "s|@PROJECT_DIR@|${PROJECT_DIR}|g" "${SCRIPT_DIR}/${SERVICE_NAME}" > "$SERVICE_PATH"

# rename は down 状態でしか通らない。既存の can* を落としてから udev を再適用する。
for iface in /sys/class/net/can*; do
    [[ -e "$iface" ]] || continue
    name="$(basename "$iface")"
    log "既存インターフェースを down: ${name}"
    ip link set "$name" down 2>/dev/null || true
done

log "udev ルールを再読み込みして適用"
udevadm control --reload-rules
udevadm trigger --subsystem-match=net --action=add
udevadm settle

log "systemd を再読み込みして有効化"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

log "完了。状態:"
systemctl --no-pager --lines=20 status "$SERVICE_NAME" || true
