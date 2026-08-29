// STM32CubeMX が生成する Core/Src/main.c から呼ばれる入口。
// main.c の USER CODE 領域には setup() / loop() の呼び出ししか置かないこと
// （再生成のたびに消える場所にロジックを書くと、CubeMX を触った人が黙って壊す）。

#ifdef __cplusplus
extern "C" {
#endif

void setup();
void loop();

#ifdef __cplusplus
}
#endif
