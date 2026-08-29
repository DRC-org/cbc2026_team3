cmake_minimum_required(VERSION 3.22)

#
# STM32CubeMX が最初の 1 回だけ生成し、以降は再生成しないファイル。
# 追記した箇所には理由をコメントで残すこと（次に CubeMX を触る人が消さないように）。
#

# Setup compiler settings
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

# C++ を有効化・設定。MotorCan（3 枚の基板で共有するプロトコル層）が C++17 を使う。
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Define the build type
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

# Set the project name
set(CMAKE_PROJECT_NAME solenoid)

# Enable compile command to ease indexing with e.g. clangd
set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

# Core project settings
project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

enable_language(C CXX ASM)

# Create an executable object type
add_executable(${CMAKE_PROJECT_NAME})

# Add STM32CubeMX generated sources
add_subdirectory(cmake/stm32cubemx)

# ---------------------------------------------------------------------------
# 共有プロトコル層（firmware/lib/MotorCan/）
#
# **DC 用・サーボ用と同じソースを参照する。** ここでコピーを持つと、プロトコルを
# 変えたときに 3 枚のうち 1 枚だけが古いまま焼かれ、症状は「そのモータが応答しない」
# だけで CAN 越しには見えない（docs/motor_driver_can_protocol.md §3.4 の INFO は
# そのために存在する）。
#
# MotorCan は Arduino.h も stm32f3xx_hal.h も include しないので、ビルド系が
# PlatformIO と CMake に分かれていてもそのまま共有できる。native テスト
# （pio test -e native）が掛かるのも同じ性質による。
# ---------------------------------------------------------------------------
set(MOTORCAN_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../lib/MotorCan/src)

# Link directories setup
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined library search paths
)

# Add sources to executable
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src/app.cpp
    ${MOTORCAN_DIR}/MotorCanProtocol.cpp
    ${MOTORCAN_DIR}/MotorCanRouter.cpp
    ${MOTORCAN_DIR}/MotorSafety.cpp
    ${MOTORCAN_DIR}/SolenoidChannel.cpp
    ${MOTORCAN_DIR}/SerialLineBuffer.cpp
)

# Add include paths
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${MOTORCAN_DIR}
)

# Add project symbols (macros)
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined symbols
)

# Remove wrong libob.a library dependency when using cpp files
list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx

    # Add user defined libraries
)
