/*
 * Тести захоплення екрана: сітка плиток, обрізання, і головне — що повільний
 * транспорт не губить змін.
 *
 * Роздільність тут не «зашита під TX16S»: збірка тестів іде з
 * -DREMOTE_UI_STANDALONE, і числа приходять прапорцями компілятора. У
 * прошивці на їх місці стоять LCD_W/LCD_H від EdgeTX.
 */

#include <stdint.h>
#include <string.h>

#include "../capture.h"
#include "test_harness.h"

using remote_ui::buildFrameEnd;
using remote_ui::captureDirtyCount;
using remote_ui::captureFrameCount;
using remote_ui::FRAME_END_PAYLOAD_SIZE;
using remote_ui::captureMarkAllDirty;
using remote_ui::captureOnFlush;
using remote_ui::captureReset;
using remote_ui::captureTakeTile;
using remote_ui::SCREEN_H;
using remote_ui::SCREEN_W;
using remote_ui::TILE_COUNT;
using remote_ui::TILE_MAX_PIXELS;
using remote_ui::TILE_SIDE;
using remote_ui::TILES_X;
using remote_ui::TILES_Y;
using remote_ui::TileRef;

namespace {

uint16_t g_tileBuf[TILE_MAX_PIXELS];

// Прямокутник пікселів для подачі в гачок. Найбільший, що знадобиться
// тестам, — половина екрана.
constexpr size_t MAX_AREA_PIXELS = 240 * 136;
uint16_t g_area[MAX_AREA_PIXELS];

void fillArea(size_t count, uint16_t value)
{
  for (size_t i = 0; i < count; ++i) {
    g_area[i] = value;
  }
}

// Скільки плиток вдалось забрати, поки вони не скінчились.
int drainAll()
{
  TileRef tile;
  int taken = 0;
  while (captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS)) {
    ++taken;
  }
  return taken;
}

}  // namespace

TEST(CaptureGridMatchesScreen)
{
  // 480x272 при плитці 32: 15 стовпців, 9 рядів, останній ряд заввишки 16.
  CHECK_EQ(TILES_X, (SCREEN_W + TILE_SIDE - 1) / TILE_SIDE);
  CHECK_EQ(TILES_Y, (SCREEN_H + TILE_SIDE - 1) / TILE_SIDE);
  CHECK_EQ(TILE_COUNT, TILES_X * TILES_Y);
  CHECK(TILE_COUNT > 0);
}

TEST(CaptureMarksOnlyTouchedTiles)
{
  captureReset();
  CHECK_EQ(captureDirtyCount(), 0);

  // Область 40x40 з (16,16) накриває плитки (0,0), (1,0), (0,1), (1,1).
  fillArea(40 * 40, 0xABCD);
  captureOnFlush(16, 16, 55, 55, g_area, false);

  CHECK_EQ(captureDirtyCount(), 4);
}

TEST(CaptureDeliversWhatWasFlushed)
{
  captureReset();

  // Рівно одна плитка (0,0), заповнена значенням, яке ні з чим не сплутати.
  fillArea(TILE_SIDE * TILE_SIDE, 0x5AA5);
  captureOnFlush(0, 0, TILE_SIDE - 1, TILE_SIDE - 1, g_area, false);

  TileRef tile;
  CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  CHECK_EQ(tile.x, 0);
  CHECK_EQ(tile.y, 0);
  CHECK_EQ(tile.w, TILE_SIDE);
  CHECK_EQ(tile.h, TILE_SIDE);

  for (size_t i = 0; i < TILE_MAX_PIXELS; ++i) {
    if (g_tileBuf[i] != 0x5AA5) {
      CHECK_EQ(g_tileBuf[i], 0x5AA5);
      break;
    }
  }

  // Забрана плитка більше не брудна.
  CHECK_EQ(captureDirtyCount(), 0);
}

TEST(CapturePlacesPixelsAtRightOffset)
{
  captureReset();

  // Вузька смуга, що перетинає межу плиток по горизонталі: перевіряємо, що
  // крок рядка джерела рахується від області, а не від екрана.
  const int x1 = 30;
  const int x2 = 35;
  const int y1 = 10;
  const int y2 = 11;
  const int w = x2 - x1 + 1;
  const int h = y2 - y1 + 1;

  for (int row = 0; row < h; ++row) {
    for (int col = 0; col < w; ++col) {
      g_area[row * w + col] = static_cast<uint16_t>(0x1000 + row * 16 + col);
    }
  }
  captureOnFlush(x1, y1, x2, y2, g_area, false);

  CHECK_EQ(captureDirtyCount(), 2);  // плитки (0,0) і (1,0)

  TileRef tile;
  CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  CHECK_EQ(tile.x, 0);

  // У плитці (0,0) мають лежати стовпці 30 і 31 рядків 10 і 11.
  CHECK_EQ(g_tileBuf[10 * TILE_SIDE + 30], 0x1000);
  CHECK_EQ(g_tileBuf[10 * TILE_SIDE + 31], 0x1001);
  CHECK_EQ(g_tileBuf[11 * TILE_SIDE + 30], 0x1010);

  CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  CHECK_EQ(tile.x, TILE_SIDE);

  // А в плитці (1,0) — стовпці 32..35 тих самих рядків.
  CHECK_EQ(g_tileBuf[10 * TILE_SIDE + 0], 0x1002);
  CHECK_EQ(g_tileBuf[10 * TILE_SIDE + 3], 0x1005);
}

TEST(CaptureEdgeTilesAreCropped)
{
  captureReset();
  captureMarkAllDirty();

  TileRef tile;
  int shortTiles = 0;
  int narrowTiles = 0;

  while (captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS)) {
    CHECK(tile.x + tile.w <= SCREEN_W);
    CHECK(tile.y + tile.h <= SCREEN_H);
    if (tile.h != TILE_SIDE) {
      ++shortTiles;
    }
    if (tile.w != TILE_SIDE) {
      ++narrowTiles;
    }
  }

  // 272 = 8*32 + 16, тому останній ряд плиток нижчий; 480 ділиться націло,
  // тому вужчих плиток немає.
  const int expectedShort = (SCREEN_H % TILE_SIDE) ? TILES_X : 0;
  const int expectedNarrow = (SCREEN_W % TILE_SIDE) ? TILES_Y : 0;
  CHECK_EQ(shortTiles, expectedShort);
  CHECK_EQ(narrowTiles, expectedNarrow);
}

TEST(CaptureClipsAreasOutsideScreen)
{
  captureReset();
  fillArea(64 * 64, 0x0F0F);

  // Частково за лівою й верхньою межею.
  captureOnFlush(-10, -10, 20, 20, g_area, false);
  CHECK_EQ(captureDirtyCount(), 1);

  captureReset();
  // Повністю за межами — нічого не має статись.
  captureOnFlush(SCREEN_W + 5, SCREEN_H + 5, SCREEN_W + 10, SCREEN_H + 10,
                 g_area, false);
  CHECK_EQ(captureDirtyCount(), 0);

  captureReset();
  // Порожня й перевернута область.
  captureOnFlush(10, 10, 5, 5, g_area, false);
  CHECK_EQ(captureDirtyCount(), 0);

  // Відсутні пікселі не мають ламати ані лічильник кадрів, ані карту.
  captureOnFlush(0, 0, 10, 10, nullptr, true);
  CHECK_EQ(captureDirtyCount(), 0);
  CHECK_EQ(captureFrameCount(), 1);
}

TEST(CaptureCountsFinishedFrames)
{
  captureReset();
  CHECK_EQ(captureFrameCount(), 0);

  fillArea(TILE_SIDE * TILE_SIDE, 0x1111);
  captureOnFlush(0, 0, TILE_SIDE - 1, TILE_SIDE - 1, g_area, false);
  CHECK_EQ(captureFrameCount(), 0);

  captureOnFlush(0, 0, TILE_SIDE - 1, TILE_SIDE - 1, g_area, true);
  CHECK_EQ(captureFrameCount(), 1);
}

TEST(CaptureKeepsTilesDirtyWhenTransportIsSlow)
{
  captureReset();
  captureMarkAllDirty();
  CHECK_EQ(captureDirtyCount(), TILE_COUNT);

  // Транспорт устигає забрати лише десять плиток — решта **не зникає**.
  TileRef tile;
  for (int i = 0; i < 10; ++i) {
    CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  }
  CHECK_EQ(captureDirtyCount(), TILE_COUNT - 10);

  // Тим часом гачок малює далі — по вже забраній плитці (0,0).
  fillArea(TILE_SIDE * TILE_SIDE, 0x2222);
  captureOnFlush(0, 0, TILE_SIDE - 1, TILE_SIDE - 1, g_area, true);
  CHECK_EQ(captureDirtyCount(), TILE_COUNT - 10 + 1);

  // Коли транспорт нарешті дійшов до кінця, клієнт отримує все, включно з
  // новою версією плитки (0,0).
  CHECK_EQ(drainAll(), TILE_COUNT - 10 + 1);
  CHECK_EQ(captureDirtyCount(), 0);
}

TEST(CaptureDoesNotStarveOtherTiles)
{
  captureReset();
  captureMarkAllDirty();

  // Одна плитка перемальовується щопрохода, транспорт забирає по одній.
  // Обхід по колу має все одно обійти весь екран.
  bool seen[TILE_COUNT];
  memset(seen, 0, sizeof(seen));

  TileRef tile;
  int distinct = 0;

  for (int pass = 0; pass < TILE_COUNT * 2 && distinct < TILE_COUNT; ++pass) {
    fillArea(TILE_SIDE * TILE_SIDE, 0x3333);
    captureOnFlush(0, 0, TILE_SIDE - 1, TILE_SIDE - 1, g_area, false);

    if (!captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS)) {
      break;
    }

    const int index = (tile.y / TILE_SIDE) * TILES_X + (tile.x / TILE_SIDE);
    if (!seen[index]) {
      seen[index] = true;
      ++distinct;
    }
  }

  CHECK_EQ(distinct, TILE_COUNT);
}

TEST(CaptureReturnsUnsentTile)
{
  captureReset();

  // Транспорт забрав плитку, але відправити не зміг — має бути спосіб
  // повернути її, інакше зміна зникне тихо (межа гарантії з capture.h).
  fillArea(TILE_SIDE * TILE_SIDE, 0x4444);
  captureOnFlush(TILE_SIDE, 0, 2 * TILE_SIDE - 1, TILE_SIDE - 1, g_area, false);

  TileRef tile;
  CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  CHECK_EQ(tile.x, TILE_SIDE);
  CHECK_EQ(captureDirtyCount(), 0);

  captureReturnTile(tile);
  CHECK_EQ(captureDirtyCount(), 1);

  TileRef again;
  CHECK(captureTakeTile(again, g_tileBuf, TILE_MAX_PIXELS));
  CHECK_EQ(again.x, tile.x);
  CHECK_EQ(again.y, tile.y);

  // Плитка поза екраном не має нічого псувати.
  const TileRef bogus = {static_cast<uint16_t>(SCREEN_W), 0, 32, 32};
  captureReturnTile(bogus);
  CHECK_EQ(captureDirtyCount(), 0);
}

TEST(CaptureRefusesSmallDestination)
{
  captureReset();
  captureMarkAllDirty();

  TileRef tile;
  // Замалий буфер — відмова, але плитка при цьому **не витрачається**.
  CHECK(!captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS - 1));
  CHECK(!captureTakeTile(tile, nullptr, TILE_MAX_PIXELS));
  CHECK_EQ(captureDirtyCount(), TILE_COUNT);
}

TEST(CaptureMarkAllDirtyRefusesBeforeFirstFlush)
{
  // ⚠️ Стан «гачок жодного разу не викликався» — не екзотика: на пульті
  // тіньовий кадр лежить у SDRAM, а її секція оголошена NOLOAD і при старті не
  // обнуляється. Позначити плитки брудними до першого флешу означало б віддати
  // клієнту сміття від попереднього вмикання як картинку.
  captureReset(/*shadowReady=*/false);

  CHECK(!captureMarkAllDirty());
  CHECK_EQ(captureDirtyCount(), 0u);

  TileRef tile;
  CHECK(!captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));

  // Перший же флеш робить кадр придатним — і далі все як завжди.
  const uint16_t px = 0x1234;
  captureOnFlush(0, 0, 0, 0, &px, false);

  CHECK(captureMarkAllDirty());
  CHECK_EQ(captureDirtyCount(), static_cast<uint32_t>(TILE_COUNT));

  // Наступний виклик повторно вже нічого не блокує.
  CHECK(captureMarkAllDirty());
}

TEST(BuildFrameEndTellsHowMuchIsStillOnTheWay)
{
  // Вантаж FRAME_END — те, за чим клієнт вирішує, показувати кадр негайно чи
  // дочекатись решти плиток (docs/03-protocol.md, 0x03).
  captureReset();

  uint8_t out[FRAME_END_PAYLOAD_SIZE];

  // Порожня карта — кадр цілісний.
  buildFrameEnd(out, captureDirtyCount());
  CHECK_EQ(out[0], 0);
  CHECK_EQ(out[1], 0);

  // Уся сітка брудна — число мусить збігтися з лічильником, і саме
  // little-endian, як усе інше в протоколі.
  captureMarkAllDirty();
  buildFrameEnd(out, captureDirtyCount());
  CHECK_EQ(static_cast<uint32_t>(out[0] | (out[1] << 8)),
           static_cast<uint32_t>(TILE_COUNT));
  CHECK_EQ(captureDirtyCount(), static_cast<uint32_t>(TILE_COUNT));

  // Забрана плитка з числа зникає: клієнт має чекати рівно тих, що ще в дорозі.
  TileRef tile;
  CHECK(captureTakeTile(tile, g_tileBuf, TILE_MAX_PIXELS));
  buildFrameEnd(out, captureDirtyCount());
  CHECK_EQ(static_cast<uint32_t>(out[0] | (out[1] << 8)),
           static_cast<uint32_t>(TILE_COUNT - 1));

  // Порядок байтів на числі, що не вміщається в один байт: 300 = 0x012C.
  buildFrameEnd(out, 300);
  CHECK_EQ(out[0], 0x2C);
  CHECK_EQ(out[1], 0x01);
}
