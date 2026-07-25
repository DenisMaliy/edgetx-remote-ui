/*
 * Тести CRC-16/CCITT-FALSE.
 */

#include <stdint.h>

#include "../crc16.h"
#include "test_harness.h"

namespace {

// Класичний контрольний рядок для перевірки реалізацій CRC.
const uint8_t CHECK_STRING[9] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};

}  // namespace

// Якщо тут не 0x29B1 — це не CCITT-FALSE, а щось інше з тією ж назвою.
TEST(CrcKnownAnswer)
{
  CHECK_EQ(remote_ui::crc16(CHECK_STRING, sizeof(CHECK_STRING)), 0x29B1u);
}

// Порожній блок не змінює регістр: саме на це спирається пакет без даних.
TEST(CrcEmptyInputIsSeed)
{
  CHECK_EQ(remote_ui::crc16(CHECK_STRING, 0), remote_ui::CRC16_INIT);
  CHECK_EQ(remote_ui::crc16(nullptr, 0), remote_ui::CRC16_INIT);
}

// Побайтовий підрахунок (як у декодувальнику) має збігатися з блоковим
// (як у кодувальнику). Інакше кадри не сходились би ніколи.
TEST(CrcIncrementalMatchesBulk)
{
  uint16_t crc = remote_ui::CRC16_INIT;
  for (size_t i = 0; i < sizeof(CHECK_STRING); ++i) {
    crc = remote_ui::crc16Update(crc, CHECK_STRING[i]);
  }

  CHECK_EQ(crc, remote_ui::crc16(CHECK_STRING, sizeof(CHECK_STRING)));
  CHECK_EQ(crc, 0x29B1u);
}

// Продовження підрахунку із заданого зерна = підрахунок по склеєному блоку.
TEST(CrcSeedContinuesStream)
{
  const uint16_t firstHalf = remote_ui::crc16(CHECK_STRING, 4);
  const uint16_t whole = remote_ui::crc16(&CHECK_STRING[4], 5, firstHalf);

  CHECK_EQ(whole, remote_ui::crc16(CHECK_STRING, sizeof(CHECK_STRING)));
}
