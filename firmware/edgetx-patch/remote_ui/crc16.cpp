/*
 * TX16S Remote UI — CRC-16/CCITT-FALSE, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 */

#if defined(REMOTE_UI)

#include "crc16.h"

namespace remote_ui {

uint16_t crc16Update(uint16_t crc, uint8_t byte)
{
  // Байт входить у старший розряд регістра, далі вісім зсувів уліво
  // з умовним відніманням полінома. Це означення CCITT-FALSE «як у книжці».
  crc ^= static_cast<uint16_t>(byte) << 8;

  for (uint8_t bit = 0; bit < 8; ++bit) {
    if (crc & 0x8000) {
      crc = static_cast<uint16_t>((crc << 1) ^ CRC16_POLY);
    } else {
      crc = static_cast<uint16_t>(crc << 1);
    }
  }

  return crc;
}

uint16_t crc16(const uint8_t* data, size_t len, uint16_t seed)
{
  uint16_t crc = seed;

  // Порожній блок — законний випадок (пакет без корисних даних).
  if (data == nullptr) {
    return crc;
  }

  for (size_t i = 0; i < len; ++i) {
    crc = crc16Update(crc, data[i]);
  }

  return crc;
}

}  // namespace remote_ui

#endif  // REMOTE_UI
