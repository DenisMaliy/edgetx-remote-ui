/*
 * TX16S Remote UI — захоплення екрана, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Ціна в пам'яті (480×272): тіньовий кадр 261 120 байт + бітова карта 20 байт.
 * Усе статичне, жодного malloc.
 */

#if defined(REMOTE_UI)

#include "capture.h"

#include <atomic>
#include <string.h>

#include "remote_ui.h"

#if defined(REMOTE_UI_STANDALONE)
// У тестах на ПК розміщення в пам'яті нікого не цікавить.
#define REMOTE_UI_SDRAM
#else
#include "definitions.h"  // __SDRAM
#define REMOTE_UI_SDRAM __SDRAM
#endif

namespace remote_ui {

namespace {

// Тіньовий кадр. Гачок приносить лише змінені прямокутники, а клієнту плитка
// потрібна цілою — тому останній відомий вигляд екрана треба десь тримати.
//
// 261 120 байт для 480×272. У внутрішній ОЗП STM32F429 (192 КБ під `.bss`,
// `boards/generic_stm32/linker/stm32f429_sdram/layout.ld`) це не влізе, тому
// буфер іде в SDRAM — туди ж, куди EdgeTX кладе власні кадрові буфери
// (`lcd.cpp`: `LCD_FIRST_FRAME_BUFFER ... __SDRAM`). На симуляторі макрос
// порожній (`targets/simu/memory_sections.h`), тож рядок один на обидві збірки.
//
// ⚠️ Наслідок для етапу 2: на кольоровій цілі **без** SDRAM тіньового кадру
// нікуди покласти, і підхід доведеться переглядати. Зараз таких цілей ми не
// збираємо.
uint16_t s_shadow[static_cast<size_t>(SCREEN_W) * SCREEN_H] REMOTE_UI_SDRAM;

constexpr size_t DIRTY_WORDS = (TILE_COUNT + 31) / 32;

// Бітова карта брудних плиток. std::atomic, бо її пишуть два потоки; на STM32
// і на ПК uint32_t атомарний без блокувань.
std::atomic<uint32_t> s_dirty[DIRTY_WORDS];

std::atomic<uint32_t> s_frameCount;

// Чи тіньовий кадр уже придатний до читання.
//
// Секція `.sdram` оголошена як NOLOAD (`linker/stm32f429_sdram/extra_sections.ld`),
// тобто, на відміну від `.bss`, при старті її ніхто не обнуляє: на пульті
// тіньовий кадр починає життя зі сміття від попереднього вмикання. Чистимо його
// при першому ж флеші.
//
// ⚠️ Наскільки вузьке вікно «транспорт уже є, кадру ще немає» — звірено з кодом
// EdgeTX, а не припущено. На звичайному шляху його немає взагалі: `edgeTxInit()`
// викликає `startSplash()` (`edgetx.cpp:1413`), а заставка домальовується
// негайно, і лише потім, на рядку 1520, іде `initSerialPorts()`. Тобто перший
// флеш стається ще до того, як наша задача взагалі з'явиться.
//
// Вікно відкривається, коли заставку пропущено, а це два випадки:
// `UNEXPECTED_SHUTDOWN()` (увімкнення після падіння) і `OPENTX_START_NO_SPLASH`
// (симулятор). Обидва рідкісні, обидва справжні — тому запобіжник лишається.
//
// Пише прапорець лише гачок, читає — і гачок, і транспорт. `release`/`acquire`
// тут не для порядку: вони гарантують, що «прапорець піднято» означає
// «memset уже завершився», а не «memset почався».
std::atomic<bool> s_shadowReady{false};

// Місце, з якого починається наступний обхід. Читає й пише лише транспорт,
// тому звичайна змінна.
uint32_t s_scanCursor;

// Головна умова всієї конструкції без м'ютекса. Якщо на якійсь цілі
// std::atomic<uint32_t> виявиться не безблокувальним, компілятор підставить
// виклик у libatomic, а той бере блокування — прямо в гачку, у задачі, яку
// витісняє мікшер. Краще не зібратись, ніж отримати це на льоту.
static_assert(std::atomic<uint32_t>::is_always_lock_free,
              "бітова карта плиток вимагає безблокувального atomic");

static_assert(sizeof(std::atomic<uint32_t>) == sizeof(uint32_t),
              "бітова карта має лишатись компактною");

inline void markTile(int index)
{
  const uint32_t bit = 1u << (index & 31);
  // release: пікселі мають лягти в тіньовий кадр **до** того, як плитка стане
  // видимою для транспорту.
  s_dirty[index >> 5].fetch_or(bit, std::memory_order_release);
}

}  // namespace

void captureOnFlush(int x1, int y1, int x2, int y2, const uint16_t* pixels,
                    bool isLast)
{
  // Чистимо тіньовий кадр один раз, при першому ж флеші — тобто до того, як
  // хоч одна плитка стане брудною і зможе піти на дріт. Чому це потрібно й
  // чому прапорець атомарний — біля його оголошення.
  //
  // ⚠️ Порядок обов'язковий: спершу memset, **потім** прапорець. Навпаки
  // транспорт міг би побачити «готово» посеред очищення й віддати клієнту
  // напівочищений кадр.
  if (!s_shadowReady.load(std::memory_order_relaxed)) {
    memset(s_shadow, 0, sizeof(s_shadow));
    s_shadowReady.store(true, std::memory_order_release);
  }

  if (pixels != nullptr && x2 >= x1 && y2 >= y1) {
    // Крок рядка джерела рахується від **необрізаної** області: саме так її
    // упакував LVGL.
    const int srcStride = x2 - x1 + 1;

    const int cx1 = (x1 > 0) ? x1 : 0;
    const int cy1 = (y1 > 0) ? y1 : 0;
    const int cx2 = (x2 < SCREEN_W - 1) ? x2 : SCREEN_W - 1;
    const int cy2 = (y2 < SCREEN_H - 1) ? y2 : SCREEN_H - 1;

    if (cx1 <= cx2 && cy1 <= cy2) {
      const size_t rowBytes = static_cast<size_t>(cx2 - cx1 + 1) * sizeof(uint16_t);

      for (int y = cy1; y <= cy2; ++y) {
        const uint16_t* src =
            pixels + static_cast<size_t>(y - y1) * srcStride + (cx1 - x1);
        uint16_t* dst = s_shadow + static_cast<size_t>(y) * SCREEN_W + cx1;
        memcpy(dst, src, rowBytes);
      }

      const int tx1 = cx1 / TILE_SIDE;
      const int ty1 = cy1 / TILE_SIDE;
      const int tx2 = cx2 / TILE_SIDE;
      const int ty2 = cy2 / TILE_SIDE;

      for (int ty = ty1; ty <= ty2; ++ty) {
        for (int tx = tx1; tx <= tx2; ++tx) {
          markTile(ty * TILES_X + tx);
        }
      }
    }
  }

  // Ознака кінця кадру не залежить від того, чи були пікселі.
  if (isLast) {
    s_frameCount.fetch_add(1, std::memory_order_release);
  }
}

bool captureTakeTile(TileRef& tile, uint16_t* out, size_t outPixels)
{
  if (out == nullptr || outPixels < TILE_MAX_PIXELS) {
    return false;
  }

  for (int n = 0; n < TILE_COUNT; ++n) {
    int index = static_cast<int>(s_scanCursor) + n;
    if (index >= TILE_COUNT) {
      index -= TILE_COUNT;
    }

    const uint32_t bit = 1u << (index & 31);
    std::atomic<uint32_t>& word = s_dirty[index >> 5];

    if ((word.load(std::memory_order_relaxed) & bit) == 0) {
      continue;
    }

    // Біт знімається **до** читання пікселів: якщо гачок утрутиться під час
    // копіювання, він поставить біт знову і плитка піде ще раз.
    // acquire: пікселі, покладені гачком, мають бути видимі після цього.
    const uint32_t prev = word.fetch_and(~bit, std::memory_order_acquire);
    if ((prev & bit) == 0) {
      continue;  // інший потік устиг забрати цю плитку
    }

    s_scanCursor = static_cast<uint32_t>((index + 1) % TILE_COUNT);

    const int tx = index % TILES_X;
    const int ty = index / TILES_X;
    const int x = tx * TILE_SIDE;
    const int y = ty * TILE_SIDE;
    const int w = (x + TILE_SIDE <= SCREEN_W) ? TILE_SIDE : (SCREEN_W - x);
    const int h = (y + TILE_SIDE <= SCREEN_H) ? TILE_SIDE : (SCREEN_H - y);

    tile.x = static_cast<uint16_t>(x);
    tile.y = static_cast<uint16_t>(y);
    tile.w = static_cast<uint16_t>(w);
    tile.h = static_cast<uint16_t>(h);

    const size_t rowBytes = static_cast<size_t>(w) * sizeof(uint16_t);
    for (int row = 0; row < h; ++row) {
      memcpy(out + static_cast<size_t>(row) * w,
             s_shadow + static_cast<size_t>(y + row) * SCREEN_W + x, rowBytes);
    }

    return true;
  }

  return false;
}

void captureReturnTile(const TileRef& tile)
{
  if (tile.x >= SCREEN_W || tile.y >= SCREEN_H) {
    return;
  }

  markTile((tile.y / TILE_SIDE) * TILES_X + (tile.x / TILE_SIDE));
}

bool captureMarkAllDirty()
{
  // ⚠️ До першого флешу показувати нічого: у тіньовому кадрі лежить те, що
  // лишилось у пам'яті від попереднього вмикання. Позначити плитки брудними
  // означало б віддати це клієнту як картинку.
  //
  // Чистити кадр звідси було б гірше: це memset на 261 КБ, і з цього контексту
  // він накладався б на memcpy гачка. Тому просто відмовляємо — і кажемо про це
  // викликачеві, щоб той повторив спробу. Без цього REFRESH, який прийшов
  // надто рано, зник би без сліду разом із обіцяним FRAME_END.
  if (!s_shadowReady.load(std::memory_order_acquire)) {
    return false;
  }

  for (int i = 0; i < TILE_COUNT; ++i) {
    markTile(i);
  }

  return true;
}

uint32_t captureFrameCount()
{
  return s_frameCount.load(std::memory_order_acquire);
}

uint32_t captureDirtyCount()
{
  uint32_t count = 0;
  for (int i = 0; i < TILE_COUNT; ++i) {
    const uint32_t bit = 1u << (i & 31);
    if (s_dirty[i >> 5].load(std::memory_order_relaxed) & bit) {
      ++count;
    }
  }
  return count;
}

void captureReset(bool shadowReady)
{
  for (size_t i = 0; i < DIRTY_WORDS; ++i) {
    s_dirty[i].store(0, std::memory_order_relaxed);
  }
  s_frameCount.store(0, std::memory_order_relaxed);
  s_scanCursor = 0;
  memset(s_shadow, 0, sizeof(s_shadow));

  // Кадр щойно обнулено — тобто типово він придатний до читання, навіть якщо
  // флешу ще не було. Інакше тести, які бруднять екран без гачка, дивились би
  // на порожню карту. `false` передає той один тест, що перевіряє поведінку до
  // першого флешу.
  s_shadowReady.store(shadowReady, std::memory_order_release);
}

}  // namespace remote_ui

// --- Точка входу для EdgeTX -------------------------------------------------
//
// Єдина функція, яку бачить чужий код. Навмисно поза простором імен і без
// прикрас: гачок у lcd.cpp має лишатись одним коротким рядком.

void remoteUiOnFlush(int x1, int y1, int x2, int y2, const uint16_t* pixels,
                     bool isLast)
{
  remote_ui::captureOnFlush(x1, y1, x2, y2, pixels, isLast);
}

#endif  // REMOTE_UI
