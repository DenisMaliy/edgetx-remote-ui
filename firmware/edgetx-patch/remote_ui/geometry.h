/*
 * TX16S Remote UI — геометрія екрана й сітка плиток.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Єдине місце в нашому коді, яке знає розмір екрана, — і воно не знає його
 * саме: числа приходять від EdgeTX через `LCD_W` / `LCD_H` (`board.h`).
 * Жодної константи, прибитої до TX16S, тут немає — на іншій цілі сітка
 * плиток перерахується сама.
 *
 * Тести на ПК збираються без жодного заголовка EdgeTX, тому там роздільність
 * задається прапорцями (`-DREMOTE_UI_STANDALONE -DREMOTE_UI_LCD_W=480 ...`).
 * Це не «специфіка пульта в коді», а спосіб перевірити геометрію без пульта.
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

#include "protocol.h"
#include "tile.h"

#if defined(REMOTE_UI_STANDALONE)
#if !defined(REMOTE_UI_LCD_W) || !defined(REMOTE_UI_LCD_H)
#error "REMOTE_UI_STANDALONE вимагає -DREMOTE_UI_LCD_W і -DREMOTE_UI_LCD_H"
#endif
#else
#include "board.h"  // LCD_W, LCD_H
#define REMOTE_UI_LCD_W LCD_W
#define REMOTE_UI_LCD_H LCD_H

// Увесь шлях захоплення припускає 16 біт на піксель: тіньовий кадр —
// `uint16_t`, RLE16 працює парами «лічильник + піксель», HELLO віддає
// формат 1 (RGB565). На ЧБ-цілі це мовчки брехало б клієнту, тому збірка
// зупиняється. ЧБ — це `capture_bw.cpp` і етап 5.
//
// COLORLCD приходить не з board.h, а прапорцем компілятора:
// radio/src/gui/colorlcd/CMakeLists.txt -> add_definitions(-DCOLORLCD).
#if !defined(COLORLCD)
#error "Remote UI поки що вміє лише кольорові цілі (RGB565)"
#endif
#endif

namespace remote_ui {

constexpr int SCREEN_W = REMOTE_UI_LCD_W;
constexpr int SCREEN_H = REMOTE_UI_LCD_H;

// Сторона плитки. Плитка — одиниця стиснення й передачі, а не спосіб пошуку
// змін (їх приносить гачок). Розмір обрано так, щоб **сира** плитка разом із
// заголовком гарантовано влазила в один PAYLOAD: 32*32*2 + 9 = 2057 байт при
// стелі 4096. Це важливо, бо на однорідному шумі RLE не стискає, і плитка
// піде сирою — розрізати її в цей момент нема куди.
constexpr int TILE_SIDE = 32;

// Крайні плитки праворуч і знизу можуть бути вужчими або нижчими за TILE_SIDE:
// 272 не ділиться на 32 націло, і це нормальний випадок, а не помилка.
constexpr int TILES_X = (SCREEN_W + TILE_SIDE - 1) / TILE_SIDE;
constexpr int TILES_Y = (SCREEN_H + TILE_SIDE - 1) / TILE_SIDE;
constexpr int TILE_COUNT = TILES_X * TILES_Y;

constexpr size_t TILE_MAX_PIXELS = static_cast<size_t>(TILE_SIDE) * TILE_SIDE;

static_assert(TILE_HEADER_SIZE + TILE_MAX_PIXELS * 2 <= MAX_PAYLOAD_SIZE,
              "сира плитка має вміщатись у PAYLOAD разом із заголовком");

static_assert(SCREEN_W > 0 && SCREEN_H > 0, "роздільність не прочиталась");

}  // namespace remote_ui
