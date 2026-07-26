/*
 * TX16S Remote UI — пакет HELLO, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 */

#if defined(REMOTE_UI) && !defined(REMOTE_UI_STANDALONE)

#include "hello.h"

#include <string.h>

#include "board.h"           // LCD_W/LCD_H, HARDWARE_TOUCH, ROTARY_ENCODER_NAVIGATION
#include "geometry.h"
#include "hal/key_driver.h"  // keysGetSupported, keysGetLabel, keysGetMaxTrims
#include "stamp.h"           // VERSION, VERSION_SUFFIX

// FLAVOUR приходить не зі stamp.h, а прапорцем компілятора:
// radio/src/CMakeLists.txt -> add_definitions(-DFLAVOUR="${FLAVOUR}").

namespace remote_ui {

// Маска підтримуваних клавіш у HELLO — 4 байти. Поки EnumKeys коротший за 32
// значення, усе сходиться; якщо upstream колись розширить перелік, `1u << key`
// стане невизначеною поведінкою, а маска почне тихо брехати. Хай краще
// зупиниться збірка при перебазуванні.
static_assert(MAX_KEYS <= 32, "маска клавіш у HELLO — 32 біти");

namespace {

// Маленький курсор із перевіркою меж: HELLO складається з різнорідних полів,
// і рахувати зсуви руками — найкоротший шлях до запису за буфер.
struct Writer {
  uint8_t* buf;
  size_t size;
  size_t pos;
  bool ok;

  void u8(uint8_t value)
  {
    if (!ok || pos + 1 > size) {
      ok = false;
      return;
    }
    buf[pos++] = value;
  }

  void u16(uint16_t value)
  {
    u8(static_cast<uint8_t>(value & 0xFF));
    u8(static_cast<uint8_t>((value >> 8) & 0xFF));
  }

  void u32(uint32_t value)
  {
    u16(static_cast<uint16_t>(value & 0xFFFF));
    u16(static_cast<uint16_t>((value >> 16) & 0xFFFF));
  }

  // Рядок фіксованої довжини, доповнений нулями. Довший — обрізається;
  // нуль у кінці не гарантується, тому клієнт читає рівно `len` байтів.
  void text(const char* value, size_t len)
  {
    if (!ok || pos + len > size) {
      ok = false;
      return;
    }
    memset(buf + pos, 0, len);
    if (value != nullptr) {
      const size_t n = strnlen(value, len);
      memcpy(buf + pos, value, n);
    }
    pos += len;
  }
};

}  // namespace

size_t buildHello(uint8_t* out, size_t outSize)
{
  if (out == nullptr) {
    return 0;
  }

  Writer w{out, outSize, 0, true};

  w.u8(1);  // версія протоколу
  w.u16(static_cast<uint16_t>(SCREEN_W));
  w.u16(static_cast<uint16_t>(SCREEN_H));
  w.u8(HELLO_PIXFMT_RGB565);

  uint8_t flags = 0;
#if defined(HARDWARE_TOUCH)
  flags |= HELLO_FLAG_TOUCH;
#endif
#if defined(ROTARY_ENCODER_NAVIGATION)
  flags |= HELLO_FLAG_ENCODER;
#endif
  // Файлові операції — наступні етапи, біт поки нуль.

  // ⚠️ Безумовно, без жодного `#if`. Прошивка, зібрана з цим файлом, розуміє
  // `INPUT_STATE` завжди: розбір лежить в `applyInputPacket()`, тобто в тому
  // самому дереві. Умова тут означала б, що біт може збрехати.
  flags |= HELLO_FLAG_INPUT_STATE;

  w.u8(flags);

  w.u8(keysGetMaxTrims());

  const uint32_t supported = keysGetSupported();
  w.u32(supported);

  // Перелік клавіш: беремо тільки ті, що ціль справді має, і тільки з назвою.
  // `_key_labels` містить nullptr на місцях невизначених клавіш, тож обидві
  // перевірки потрібні.
  uint8_t count = 0;
  for (int key = 0; key < MAX_KEYS; ++key) {
    if ((supported & (1u << key)) == 0) {
      continue;
    }
    if (keysGetLabel(static_cast<EnumKeys>(key)) == nullptr) {
      continue;
    }
    ++count;
  }
  w.u8(count);

  for (int key = 0; key < MAX_KEYS; ++key) {
    if ((supported & (1u << key)) == 0) {
      continue;
    }
    const char* label = keysGetLabel(static_cast<EnumKeys>(key));
    if (label == nullptr) {
      continue;
    }
    w.u8(static_cast<uint8_t>(key));
    w.text(label, HELLO_KEY_NAME_LEN);
  }

  w.text(FLAVOUR, HELLO_TARGET_LEN);

  // Версія без префікса ("pre-"), але з суфіксом: у 16 байтів поле вміщає
  // "2.12.2-remoteui" рівно, а суфікс — це ознака нашої збірки, і клієнту
  // корисніше бачити саме її.
  w.text(VERSION VERSION_SUFFIX, HELLO_VERSION_LEN);

  return w.ok ? w.pos : 0;
}

}  // namespace remote_ui

#endif  // REMOTE_UI && !REMOTE_UI_STANDALONE
