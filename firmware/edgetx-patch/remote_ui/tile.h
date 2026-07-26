/*
 * TX16S Remote UI — збирання корисних даних пакета TILE.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Формат PAYLOAD (docs/03-protocol.md, пакет 0x02):
 *
 *   [x:2][y:2][ширина:2][висота:2][метод:1][дані]
 *
 * усе little-endian, метод 0 = сирі пікселі RGB565, 1 = RLE16.
 *
 * Шар не знає ані про EdgeTX, ані про транспорт: на вході прямокутник і
 * пікселі, на виході — готовий PAYLOAD.
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

namespace remote_ui {

// Заголовок PAYLOAD: x, y, ширина, висота (по 2 байти) + метод (1 байт).
constexpr size_t TILE_HEADER_SIZE = 9;

// Методи стиснення в полі «метод».
constexpr uint8_t TILE_METHOD_RAW = 0;
constexpr uint8_t TILE_METHOD_RLE16 = 1;

// Положення й розмір плитки в логічних (неповернутих) координатах екрана.
struct TileRef {
  uint16_t x;
  uint16_t y;
  uint16_t w;
  uint16_t h;
};

// Складає PAYLOAD пакета TILE у `out`.
//
// Пікселі беруться суцільним масивом w*h — так, як їх віддає captureTakeTile().
// Метод обирається сам: RLE береться, лише якщо він **строго менший** за сирі
// дані. Рівність теж дає сире — розпакування коштує клієнту процесора, і
// платити за нього без виграшу в байтах нема сенсу.
//
// Повертає довжину PAYLOAD або 0, якщо аргументи хибні чи в `out` не
// вистачило місця навіть на сиру плитку.
size_t encodeTilePayload(const TileRef& tile, const uint16_t* pixels,
                         uint8_t* out, size_t outSize);

}  // namespace remote_ui
