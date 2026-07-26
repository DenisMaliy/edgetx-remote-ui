/*
 * TX16S Remote UI — збирання корисних даних пакета TILE, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 */

#if defined(REMOTE_UI)

#include "tile.h"

#include "rle16.h"

namespace remote_ui {

namespace {

void writeU16(uint8_t* out, uint16_t value)
{
  out[0] = static_cast<uint8_t>(value & 0xFF);
  out[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
}

}  // namespace

size_t encodeTilePayload(const TileRef& tile, const uint16_t* pixels,
                         uint8_t* out, size_t outSize)
{
  if (pixels == nullptr || out == nullptr) {
    return 0;
  }

  if (tile.w == 0 || tile.h == 0) {
    return 0;
  }

  const size_t pixelCount = static_cast<size_t>(tile.w) * tile.h;
  const size_t rawBytes = pixelCount * 2;

  // Сирий варіант має вміститись завжди — це наш запасний шлях, і якщо він не
  // влазить, стискати нема сенсу: результат нікуди буде подіти.
  if (outSize < TILE_HEADER_SIZE + rawBytes) {
    return 0;
  }

  writeU16(&out[0], tile.x);
  writeU16(&out[2], tile.y);
  writeU16(&out[4], tile.w);
  writeU16(&out[6], tile.h);

  uint8_t* const data = &out[TILE_HEADER_SIZE];

  // Стеля для RLE — на байт менша за сирі дані. Кодувальник зупиняється сам,
  // щойно перестає вигравати, і повертає 0. Так однорідний шум не роздуває
  // трафік удвічі, а просто йде сирим.
  const size_t rleBytes = rle16Encode(pixels, pixelCount, data, rawBytes - 1);

  if (rleBytes > 0) {
    out[8] = TILE_METHOD_RLE16;
    return TILE_HEADER_SIZE + rleBytes;
  }

  out[8] = TILE_METHOD_RAW;
  for (size_t i = 0; i < pixelCount; ++i) {
    data[i * 2] = static_cast<uint8_t>(pixels[i] & 0xFF);
    data[i * 2 + 1] = static_cast<uint8_t>((pixels[i] >> 8) & 0xFF);
  }

  return TILE_HEADER_SIZE + rawBytes;
}

}  // namespace remote_ui

#endif  // REMOTE_UI
