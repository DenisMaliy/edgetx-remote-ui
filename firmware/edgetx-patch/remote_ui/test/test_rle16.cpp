/*
 * Тести стиснення RLE16 і збирання пакета TILE.
 *
 * Головне, що тут доводиться, — правило «стиснуте більше за сире → шлемо
 * сире». Без нього однорідний шум роздуває трафік у півтора раза замість
 * того, щоб стискатись.
 */

#include <stdint.h>
#include <string.h>

#include "../rle16.h"
#include "../tile.h"
#include "test_harness.h"

using remote_ui::encodeTilePayload;
using remote_ui::rle16Decode;
using remote_ui::rle16Encode;
using remote_ui::TILE_HEADER_SIZE;
using remote_ui::TILE_METHOD_RAW;
using remote_ui::TILE_METHOD_RLE16;
using remote_ui::TileRef;

namespace {

// Робочі буфери статичні: каркас провалює тест за будь-яке виділення
// динамічної пам'яті, а 32x32 пікселі на стеку — це вже 2 КБ.
constexpr size_t MAX_PIXELS = 1024;

uint16_t g_pixels[MAX_PIXELS];
uint16_t g_decoded[MAX_PIXELS];
uint8_t g_out[8192];

// Псевдовипадкові пікселі з нерухомим зерном: тест має бути повторюваним.
uint16_t nextNoise(uint32_t& state)
{
  state = state * 1103515245u + 12345u;
  return static_cast<uint16_t>(state >> 16);
}

}  // namespace

TEST(RleSolidBlockCollapses)
{
  for (size_t i = 0; i < MAX_PIXELS; ++i) {
    g_pixels[i] = 0xF81F;
  }

  const size_t len = rle16Encode(g_pixels, MAX_PIXELS, g_out, sizeof(g_out));

  // 1024 = 4 серії по 255 + одна на 4 пікселі -> 5 пар по 3 байти.
  CHECK_EQ(len, 15);
  CHECK_EQ(g_out[0], 255);
  CHECK_EQ(g_out[1], 0x1F);
  CHECK_EQ(g_out[2], 0xF8);
  CHECK_EQ(g_out[12], 4);

  const size_t back = rle16Decode(g_out, len, g_decoded, MAX_PIXELS);
  CHECK_EQ(back, MAX_PIXELS);
  CHECK_BYTES_EQ(reinterpret_cast<const uint8_t*>(g_decoded),
                 reinterpret_cast<const uint8_t*>(g_pixels), MAX_PIXELS * 2);
}

TEST(RleCounterStopsAt255)
{
  for (size_t i = 0; i < 300; ++i) {
    g_pixels[i] = 0x1234;
  }

  const size_t len = rle16Encode(g_pixels, 300, g_out, sizeof(g_out));

  CHECK_EQ(len, 6);
  CHECK_EQ(g_out[0], 255);
  CHECK_EQ(g_out[3], 45);
}

TEST(RleWithoutRepeatsGrows)
{
  for (size_t i = 0; i < MAX_PIXELS; ++i) {
    g_pixels[i] = static_cast<uint16_t>(i);  // усі різні
  }

  // З великим буфером стиснуте виходить у півтора раза більшим за сире.
  const size_t len = rle16Encode(g_pixels, MAX_PIXELS, g_out, sizeof(g_out));
  CHECK_EQ(len, MAX_PIXELS * 3);

  // А зі стелею «на байт менше за сире» кодувальник чесно каже «не вийшло».
  CHECK_EQ(rle16Encode(g_pixels, MAX_PIXELS, g_out, MAX_PIXELS * 2 - 1), 0);
}

TEST(RleEmptyAndBadArguments)
{
  CHECK_EQ(rle16Encode(g_pixels, 0, g_out, sizeof(g_out)), 0);
  CHECK_EQ(rle16Encode(nullptr, 4, g_out, sizeof(g_out)), 0);
  CHECK_EQ(rle16Encode(g_pixels, 4, nullptr, sizeof(g_out)), 0);
}

TEST(RleRoundTripMixedRuns)
{
  uint32_t state = 777;
  size_t written = 0;

  // Суміш: довгі однотонні смуги впереміш із шумом — так виглядає справжній
  // інтерфейс, де є фон і текст.
  while (written < MAX_PIXELS) {
    const uint16_t value = nextNoise(state);
    size_t run = (value & 1) ? 1 : (value % 40) + 1;
    if (written + run > MAX_PIXELS) {
      run = MAX_PIXELS - written;
    }
    for (size_t i = 0; i < run; ++i) {
      g_pixels[written++] = value;
    }
  }

  const size_t len = rle16Encode(g_pixels, MAX_PIXELS, g_out, sizeof(g_out));
  CHECK(len > 0);

  const size_t back = rle16Decode(g_out, len, g_decoded, MAX_PIXELS);
  CHECK_EQ(back, MAX_PIXELS);
  CHECK_BYTES_EQ(reinterpret_cast<const uint8_t*>(g_decoded),
                 reinterpret_cast<const uint8_t*>(g_pixels), MAX_PIXELS * 2);
}

TEST(RleDecodeRejectsBrokenStream)
{
  const uint8_t notMultipleOfThree[] = {1, 0x00, 0x00, 1};
  CHECK_EQ(rle16Decode(notMultipleOfThree, sizeof(notMultipleOfThree), g_decoded,
                       MAX_PIXELS),
           0);

  const uint8_t zeroCount[] = {0, 0x11, 0x22};
  CHECK_EQ(rle16Decode(zeroCount, sizeof(zeroCount), g_decoded, MAX_PIXELS), 0);

  const uint8_t tooManyPixels[] = {200, 0x11, 0x22};
  CHECK_EQ(rle16Decode(tooManyPixels, sizeof(tooManyPixels), g_decoded, 10), 0);

  CHECK_EQ(rle16Decode(zeroCount, 0, g_decoded, MAX_PIXELS), 0);
}

TEST(TileChoosesRleForFlatArea)
{
  for (size_t i = 0; i < MAX_PIXELS; ++i) {
    g_pixels[i] = 0x07E0;
  }

  const TileRef tile = {32, 64, 32, 32};
  const size_t len = encodeTilePayload(tile, g_pixels, g_out, sizeof(g_out));

  CHECK_EQ(len, TILE_HEADER_SIZE + 15);

  // Заголовок — little-endian, поле за полем.
  CHECK_EQ(g_out[0], 32);
  CHECK_EQ(g_out[1], 0);
  CHECK_EQ(g_out[2], 64);
  CHECK_EQ(g_out[3], 0);
  CHECK_EQ(g_out[4], 32);
  CHECK_EQ(g_out[6], 32);
  CHECK_EQ(g_out[8], TILE_METHOD_RLE16);
}

TEST(TileFallsBackToRawOnNoise)
{
  uint32_t state = 4242;
  for (size_t i = 0; i < MAX_PIXELS; ++i) {
    g_pixels[i] = nextNoise(state);
  }

  const TileRef tile = {0, 0, 32, 32};
  const size_t len = encodeTilePayload(tile, g_pixels, g_out, sizeof(g_out));

  // Саме те, заради чого правило й існує: на шумі шлемо сире, а не роздуте.
  CHECK_EQ(g_out[8], TILE_METHOD_RAW);
  CHECK_EQ(len, TILE_HEADER_SIZE + MAX_PIXELS * 2);

  // Сирі дані — теж little-endian.
  CHECK_EQ(g_out[TILE_HEADER_SIZE], g_pixels[0] & 0xFF);
  CHECK_EQ(g_out[TILE_HEADER_SIZE + 1], (g_pixels[0] >> 8) & 0xFF);
}

TEST(TileRejectsTooSmallBuffer)
{
  const TileRef tile = {0, 0, 32, 32};

  // Місця має вистачати на **сиру** плитку, навіть якщо стиснута влізла б:
  // інакше запасного шляху немає.
  CHECK_EQ(encodeTilePayload(tile, g_pixels, g_out, TILE_HEADER_SIZE + 100), 0);

  const TileRef empty = {0, 0, 0, 32};
  CHECK_EQ(encodeTilePayload(empty, g_pixels, g_out, sizeof(g_out)), 0);
  CHECK_EQ(encodeTilePayload(tile, nullptr, g_out, sizeof(g_out)), 0);
}

TEST(TileEdgeSizeIsHandled)
{
  for (size_t i = 0; i < MAX_PIXELS; ++i) {
    g_pixels[i] = 0x0000;
  }

  // Нижній ряд екрана 480x272: висота плитки 16, а не 32.
  const TileRef tile = {448, 256, 32, 16};
  const size_t len = encodeTilePayload(tile, g_pixels, g_out, sizeof(g_out));

  CHECK(len > 0);
  CHECK_EQ(g_out[6], 16);
  CHECK_EQ(g_out[7], 0);

  const size_t pixels = 32 * 16;
  const size_t back = rle16Decode(&g_out[TILE_HEADER_SIZE],
                                  len - TILE_HEADER_SIZE, g_decoded, MAX_PIXELS);
  CHECK_EQ(back, pixels);
}
