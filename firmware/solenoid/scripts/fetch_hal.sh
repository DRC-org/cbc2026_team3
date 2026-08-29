#!/usr/bin/env bash
#
# 電磁弁基板ファームの依存（STM32F3 の HAL ドライバと CMSIS）を Drivers/ へ取得する。
#
# **STM32CubeMX が無くてもビルドできるようにするためのスクリプト。** CubeMX の
# GENERATE CODE が Drivers/ に置くものと同じ内容を、ST の公式リポジトリから直接持ってくる。
# `Drivers/` は生成物なのでリポジトリにはコミットしない（サンプル元と同じ運用）が、
# **clone 直後にビルドできない状態を残さない**ためにここで手順を固定してある。
#
# リビジョンは STM32CubeF3 パッケージがサブモジュールとして指しているコミットに固定する。
# master を追うと、Cube パッケージとして検証されていない HAL と CMSIS の組み合わせで
# 焼くことになり、症状が出たときに「自分のコードが悪いのか HAL が変わったのか」を
# 切り分けられなくなる。
#
# 使い方:
#   firmware/solenoid/scripts/fetch_hal.sh
#
# CubeMX を使う場合はこのスクリプトは不要（GENERATE CODE が Drivers/ を作る）。
# ただし .ioc を変更したときは CubeMX 側で再生成すること —— このスクリプトは
# Drivers/ を取ってくるだけで、Core/ や cmake/ には触らない。

set -euo pipefail

# STM32CubeF3 の master（2026-08 時点）と、それがサブモジュールとして指すコミット。
readonly CUBE_F3_REF=52c94d2431c0f6337944ed1a34577a19d668a691
readonly HAL_REF=d1ff4171c72e4ee4734d6a3f9ad80d4c3ad580e7
readonly CMSIS_DEVICE_REF=5558e64e3675a1e1fcb1c71f468c7c407c1b1134

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROJECT_DIR
readonly DRIVERS_DIR="${PROJECT_DIR}/Drivers"

if [[ -d "${DRIVERS_DIR}" ]]; then
    echo "既に ${DRIVERS_DIR} があります。取り直すなら削除してから実行してください。" >&2
    exit 1
fi

WORK_DIR="$(mktemp -d)"
readonly WORK_DIR
trap 'rm -rf "${WORK_DIR}"' EXIT

# 単一コミットだけを取る（--depth 1 の clone ではタグの無いコミットを指定できない）。
# sparse_path を渡すと、そのパスだけをチェックアウトする（STM32CubeF3 は数百 MB あり、
# 必要なのは CMSIS/Include だけなので全部は取らない）。
fetch() {
    local repo="$1" ref="$2" dest="$3" sparse_path="${4:-}"
    git -C "${WORK_DIR}" init -q "${dest}"
    git -C "${WORK_DIR}/${dest}" remote add origin "${repo}"

    local filter=()
    if [[ -n "${sparse_path}" ]]; then
        git -C "${WORK_DIR}/${dest}" sparse-checkout set --no-cone "${sparse_path}"
        filter=(--filter=blob:none)
    fi

    git -C "${WORK_DIR}/${dest}" fetch -q --depth 1 "${filter[@]}" origin "${ref}"
    git -C "${WORK_DIR}/${dest}" checkout -q FETCH_HEAD
}

echo "HAL ドライバを取得中..."
fetch https://github.com/STMicroelectronics/stm32f3xx_hal_driver.git "${HAL_REF}" hal

echo "CMSIS デバイスヘッダを取得中..."
fetch https://github.com/STMicroelectronics/cmsis_device_f3.git "${CMSIS_DEVICE_REF}" cmsis_device

# CMSIS Core（core_cm4.h 等）は STM32CubeF3 本体に入っており、サブモジュールではない。
echo "CMSIS Core を取得中..."
fetch https://github.com/STMicroelectronics/STM32CubeF3.git "${CUBE_F3_REF}" cube "Drivers/CMSIS/Include/*"

mkdir -p "${DRIVERS_DIR}/STM32F3xx_HAL_Driver"
mkdir -p "${DRIVERS_DIR}/CMSIS/Device/ST/STM32F3xx"
mkdir -p "${DRIVERS_DIR}/CMSIS/Include"

cp -r "${WORK_DIR}/hal/Inc" "${DRIVERS_DIR}/STM32F3xx_HAL_Driver/Inc"
cp -r "${WORK_DIR}/hal/Src" "${DRIVERS_DIR}/STM32F3xx_HAL_Driver/Src"
cp -r "${WORK_DIR}/cmsis_device/Include" "${DRIVERS_DIR}/CMSIS/Device/ST/STM32F3xx/Include"
cp -r "${WORK_DIR}/cube/Drivers/CMSIS/Include/." "${DRIVERS_DIR}/CMSIS/Include/"

echo "完了: ${DRIVERS_DIR}"
echo
echo "次のコマンドでビルドできます（arm-none-eabi-gcc 11 以降が PATH に要る）:"
echo "  cmake --preset Debug -S firmware/solenoid"
echo "  cmake --build firmware/solenoid/build/Debug"
