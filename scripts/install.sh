#!/usr/bin/env bash
#
# udev ルールと systemd unit を配置する。
#   cbc-can.service     … CAN バス初期化。enable する (電源投入で up)
#   cbc-control.service … 中央制御プログラム + Web UI。enable しない (手動 start)
#
# 制御プログラムを自動起動しないのは、電源投入だけで機体が通電・待機状態になる
# のを避けるため。起動タイミングは操縦者が握る。
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
CONTROL_SERVICE_NAME="cbc-control.service"
CONTROL_SERVICE_PATH="/etc/systemd/system/${CONTROL_SERVICE_NAME}"
PYTHON="/usr/bin/python3"

# 制御プログラムを走らせるユーザー。root で走らせると venv も CAN ソケットも
# root 所有になり、開発時の手動起動と実行条件が食い違う。SocketCAN の bind に
# 特権は要らない (up は root の cbc-can.service が済ませている)
RUN_USER="${SUDO_USER:-}"

log() { echo "[install] $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "root 権限が必要です: sudo $0 $*" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$CONTROL_SERVICE_NAME" 2>/dev/null || true
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$CONTROL_SERVICE_PATH" "$SERVICE_PATH" "$UDEV_RULE_PATH"
    systemctl daemon-reload
    udevadm control --reload-rules
    log "撤去しました。CAN インターフェース名は再起動後に can0 等へ戻ります。"
    exit 0
fi

if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    echo "制御プログラムの実行ユーザーを特定できません。" >&2
    echo "一般ユーザーから sudo で実行してください: sudo $0 $*" >&2
    exit 1
fi

# 設定不正なら何も配置せずに止める
"$PYTHON" "${SCRIPT_DIR}/can_config.py" list >/dev/null

log "udev ルールを生成: ${UDEV_RULE_PATH}"
"$PYTHON" "${SCRIPT_DIR}/can_config.py" udev > "$UDEV_RULE_PATH"

log "systemd unit を配置: ${SERVICE_PATH}"
sed "s|@PROJECT_DIR@|${PROJECT_DIR}|g" "${SCRIPT_DIR}/${SERVICE_NAME}" > "$SERVICE_PATH"

log "systemd unit を配置: ${CONTROL_SERVICE_PATH} (実行ユーザー: ${RUN_USER})"
sed -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" -e "s|@RUN_USER@|${RUN_USER}|g" \
    "${SCRIPT_DIR}/${CONTROL_SERVICE_NAME}" > "$CONTROL_SERVICE_PATH"

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

# cbc-control は enable も restart もしない。試合中に unit を入れ直しただけで
# 制御プログラムが落ちる (= 機体が止まる) のを避けるため、反映は次の start から
log "${CONTROL_SERVICE_NAME} を配置しました (自動起動は無効。手動 start で使う)"

log "完了。状態:"
systemctl --no-pager --lines=20 status "$SERVICE_NAME" || true

cat <<EOS

次の手順:
  scripts/deploy.sh              # 依存導入 + Web UI ビルド + サービス再起動
  sudo systemctl start ${CONTROL_SERVICE_NAME}    # 制御プログラム起動
  journalctl -u ${CONTROL_SERVICE_NAME} -f        # ログ追跡
EOS
