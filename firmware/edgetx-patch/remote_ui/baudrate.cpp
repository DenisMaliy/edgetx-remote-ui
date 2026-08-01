/*
 * TX16S Remote UI — перемикання швидкості каналу: перелік і пакети.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Опис і обґрунтування — у baudrate.h. Тут сама робота.
 */

#if defined(REMOTE_UI)

#include "baudrate.h"

namespace remote_ui {

namespace {

// Двобайтове поле протоколу, little-endian.
void writeLe16(uint8_t* p, uint16_t value)
{
  p[0] = static_cast<uint8_t>(value & 0xFF);
  p[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
}

// Чотирибайтове поле протоколу, little-endian.
void writeLe32(uint8_t* p, uint32_t value)
{
  p[0] = static_cast<uint8_t>(value & 0xFF);
  p[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
  p[2] = static_cast<uint8_t>((value >> 16) & 0xFF);
  p[3] = static_cast<uint8_t>((value >> 24) & 0xFF);
}

uint32_t readLe32(const uint8_t* p)
{
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

}  // namespace

// Перелік короткий і перевіряється рідко — лінійний пошук тут дешевший за
// будь-яку структуру, і його видно очима.
bool baudAllowed(uint32_t baud)
{
  for (size_t i = 0; i < BAUD_ALLOWED_COUNT; ++i) {
    if (BAUD_ALLOWED[i] == baud) {
      return true;
    }
  }
  return false;
}

bool parseBaudSet(const uint8_t* payload, size_t length, uint32_t& baud,
                  uint8_t& nonce)
{
  if (payload == nullptr || length < BAUD_SET_SIZE) {
    return false;
  }

  // Довший вантаж не відкидається: невідомий хвіст ігнорується, як велить
  // правило сумісності протоколу. Коротший — відкидається цілком, бо частково
  // прочитане число швидкості гірше за нечитане.
  baud = readLe32(payload);
  nonce = payload[4];
  return true;
}

size_t buildBaudReport(uint8_t* out, size_t outSize, uint8_t verdict,
                       uint8_t nonce, uint32_t target, uint32_t current,
                       uint16_t switchDelayMs, uint16_t revertWindowMs,
                       uint16_t revertCount)
{
  if (out == nullptr || outSize < BAUD_REPORT_SIZE) {
    return 0;
  }

  out[0] = verdict;
  out[1] = nonce;
  writeLe32(out + 2, target);
  writeLe32(out + 6, current);
  writeLe16(out + 10, switchDelayMs);
  writeLe16(out + 12, revertWindowMs);
  writeLe16(out + 14, revertCount);

  return BAUD_REPORT_SIZE;
}

}  // namespace remote_ui

#endif  // REMOTE_UI
