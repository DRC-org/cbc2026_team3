#include <unity.h>

#include "SlcanCodec.h"

using namespace motorcan;

void setUp() {}
void tearDown() {}

namespace {

SlcanRequest feed(SlcanCodec &codec, const char *text) {
    SlcanRequest last{SlcanResult::None, nullptr, {0, 0, {0}}};
    for (const char *p = text; *p != '\0'; ++p) {
        last = codec.push(*p);
    }
    return last;
}

SlcanCodec openedCodec() {
    SlcanCodec codec;
    feed(codec, "S8\r");
    feed(codec, "O\r");
    return codec;
}

}

static void test_frame_round_trips_through_encode_and_decode() {
    const uint8_t data[8] = {0x00, 0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xFF};
    char text[kSlcanFrameTextCapacity] = {0};
    const size_t written = encodeFrame(text, sizeof(text), 0x123, 8, data);
    TEST_ASSERT_EQUAL_UINT32(22u, (uint32_t)written);

    SlcanCodec codec = openedCodec();
    const SlcanRequest request = feed(codec, text);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(request.result));
    TEST_ASSERT_EQUAL_UINT16(0x123, request.frame.canId);
    TEST_ASSERT_EQUAL_UINT8(8, request.frame.len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(data, request.frame.data, 8);
}

static void test_zero_length_frame_round_trips() {
    char text[kSlcanFrameTextCapacity] = {0};
    const size_t written = encodeFrame(text, sizeof(text), 0x7FF, 0, nullptr);
    TEST_ASSERT_EQUAL_UINT32(6u, (uint32_t)written);
    TEST_ASSERT_EQUAL_STRING("t7FF0\r", text);

    SlcanCodec codec = openedCodec();
    const SlcanRequest request = feed(codec, text);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(request.result));
    TEST_ASSERT_EQUAL_UINT16(0x7FF, request.frame.canId);
    TEST_ASSERT_EQUAL_UINT8(0, request.frame.len);
}

static void test_encoded_hex_is_uppercase() {
    const uint8_t data[2] = {0xAB, 0xCD};
    char text[kSlcanFrameTextCapacity] = {0};
    TEST_ASSERT_EQUAL_UINT32(10u, (uint32_t)encodeFrame(text, sizeof(text), 0x1AB, 2, data));
    TEST_ASSERT_EQUAL_STRING("t1AB2ABCD\r", text);
}

static void test_encode_rejects_out_of_range_and_short_buffers() {
    const uint8_t data[8] = {0};
    char text[kSlcanFrameTextCapacity] = {0};
    TEST_ASSERT_EQUAL_UINT32(0u, (uint32_t)encodeFrame(text, sizeof(text), 0x800, 0, nullptr));
    TEST_ASSERT_EQUAL_UINT32(0u, (uint32_t)encodeFrame(text, sizeof(text), 0x100, 9, data));
    TEST_ASSERT_EQUAL_UINT32(0u, (uint32_t)encodeFrame(text, sizeof(text), 0x100, 1, nullptr));
    TEST_ASSERT_EQUAL_UINT32(0u, (uint32_t)encodeFrame(text, 22, 0x100, 8, data));
    TEST_ASSERT_EQUAL_UINT32(0u, (uint32_t)encodeFrame(nullptr, 32, 0x100, 0, nullptr));
}

static void test_lowercase_and_uppercase_hex_decode_alike() {
    SlcanCodec lower = openedCodec();
    SlcanCodec upper = openedCodec();
    const SlcanRequest a = feed(lower, "t1ab2abcd\r");
    const SlcanRequest b = feed(upper, "t1AB2ABCD\r");

    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(a.result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(b.result));
    TEST_ASSERT_EQUAL_UINT16(b.frame.canId, a.frame.canId);
    TEST_ASSERT_EQUAL_UINT8(b.frame.len, a.frame.len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(b.frame.data, a.frame.data, 2);
}

static void test_transmit_is_refused_until_the_channel_is_open() {
    SlcanCodec codec;
    TEST_ASSERT_FALSE(codec.isOpen());
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1230\r").result));

    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Ok),
                            static_cast<uint8_t>(feed(codec, "O\r").result));
    TEST_ASSERT_TRUE(codec.isOpen());
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(feed(codec, "t1230\r").result));

    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Ok),
                            static_cast<uint8_t>(feed(codec, "C\r").result));
    TEST_ASSERT_FALSE(codec.isOpen());
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1230\r").result));
}

static void test_extended_and_remote_frames_are_refused() {
    SlcanCodec codec = openedCodec();
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "T000001230\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "r1230\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "R000001230\r").result));
}

static void test_malformed_transmit_lines_are_refused() {
    SlcanCodec codec = openedCodec();
    // ID が 11bit に収まらない
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t8000\r").result));
    // DLC > 8
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1009\r").result));
    // データが DLC より短い / 長い
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1002AB\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1001ABCD\r").result));
    // 16 進以外の文字
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1g00\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t1001XY\r").result));
    // ヘッダが足りない
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "t123\r").result));
    // 直後の正常な行は影響を受けない
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(feed(codec, "t1001AB\r").result));
}

static void test_overflowed_line_is_discarded_without_breaking_the_next() {
    SlcanCodec codec = openedCodec();
    TEST_ASSERT_EQUAL_UINT8(
        static_cast<uint8_t>(SlcanResult::Error),
        static_cast<uint8_t>(feed(codec, "t1238AABBCCDDEEFF0011223344556677\r").result));

    const SlcanRequest request = feed(codec, "t1001AB\r");
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Frame),
                            static_cast<uint8_t>(request.result));
    TEST_ASSERT_EQUAL_UINT16(0x100, request.frame.canId);
    TEST_ASSERT_EQUAL_UINT8(1, request.frame.len);
    TEST_ASSERT_EQUAL_UINT8(0xAB, request.frame.data[0]);
}

static void test_empty_lines_produce_no_response() {
    SlcanCodec codec = openedCodec();
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::None),
                            static_cast<uint8_t>(feed(codec, "\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::None),
                            static_cast<uint8_t>(feed(codec, "\r\n\r").result));
    TEST_ASSERT_TRUE(codec.isOpen());
}

static void test_bitrate_command_accepts_only_zero_to_eight() {
    SlcanCodec codec;
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Ok),
                            static_cast<uint8_t>(feed(codec, "S8\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Ok),
                            static_cast<uint8_t>(feed(codec, "S0\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "S9\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "S\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "S88\r").result));
}

static void test_unknown_commands_are_refused() {
    SlcanCodec codec = openedCodec();
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "X\r").result));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Error),
                            static_cast<uint8_t>(feed(codec, "OO\r").result));
    TEST_ASSERT_TRUE(codec.isOpen());
}

static void test_info_queries_answer_with_fixed_replies() {
    SlcanCodec codec;
    const SlcanRequest version = feed(codec, "V\r");
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Reply),
                            static_cast<uint8_t>(version.result));
    TEST_ASSERT_EQUAL_STRING("V1013\r", version.reply);

    const SlcanRequest serial = feed(codec, "N\r");
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Reply),
                            static_cast<uint8_t>(serial.result));
    TEST_ASSERT_EQUAL_STRING("N0001\r", serial.reply);

    const SlcanRequest status = feed(codec, "F\r");
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlcanResult::Reply),
                            static_cast<uint8_t>(status.result));
    TEST_ASSERT_EQUAL_STRING("F00\r", status.reply);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_frame_round_trips_through_encode_and_decode);
    RUN_TEST(test_zero_length_frame_round_trips);
    RUN_TEST(test_encoded_hex_is_uppercase);
    RUN_TEST(test_encode_rejects_out_of_range_and_short_buffers);
    RUN_TEST(test_lowercase_and_uppercase_hex_decode_alike);
    RUN_TEST(test_transmit_is_refused_until_the_channel_is_open);
    RUN_TEST(test_extended_and_remote_frames_are_refused);
    RUN_TEST(test_malformed_transmit_lines_are_refused);
    RUN_TEST(test_overflowed_line_is_discarded_without_breaking_the_next);
    RUN_TEST(test_empty_lines_produce_no_response);
    RUN_TEST(test_bitrate_command_accepts_only_zero_to_eight);
    RUN_TEST(test_unknown_commands_are_refused);
    RUN_TEST(test_info_queries_answer_with_fixed_replies);
    return UNITY_END();
}
