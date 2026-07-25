/*
 * Тести кадрування Remote UI: кодування, потокове декодування, живучість.
 */

#include <stdint.h>
#include <string.h>

#include <random>

#include "../protocol.h"
#include "alloc_guard.h"
#include "test_harness.h"

using remote_ui::Decoder;
using remote_ui::MAX_FRAME_SIZE;
using remote_ui::MAX_PAYLOAD_SIZE;

namespace {

// --- Збирач пакетів -------------------------------------------------------

struct Capture {
  static constexpr size_t MAX_PACKETS = 4;

  size_t count;
  size_t overflow;
  uint8_t types[MAX_PACKETS];
  size_t lengths[MAX_PACKETS];
  uint8_t payloads[MAX_PACKETS][MAX_PAYLOAD_SIZE];

  void reset()
  {
    count = 0;
    overflow = 0;
  }

  void add(uint8_t type, const uint8_t* payload, size_t length)
  {
    if (count >= MAX_PACKETS) {
      ++overflow;
      return;
    }
    types[count] = type;
    lengths[count] = length;
    if (length > 0) {
      memcpy(payloads[count], payload, length);
    }
    ++count;
  }
};

void onPacket(uint8_t type, const uint8_t* payload, size_t length,
              void* context)
{
  static_cast<Capture*>(context)->add(type, payload, length);
}

// --- Робочі буфери --------------------------------------------------------
//
// Усе статично: 16 КБ збирача на стеку кожного тесту ні до чого, та й самі
// тести мають жити за тими ж правилами, що й прошивка.

Capture g_capture;
Decoder g_decoder;
uint8_t g_payload[MAX_PAYLOAD_SIZE];
uint8_t g_frame[MAX_FRAME_SIZE];
uint8_t g_stream[2 * MAX_FRAME_SIZE + 64];

// Впізнаваний, але не однорідний вміст — щоб зсув на байт було видно.
void fillPattern(uint8_t* buf, size_t len, uint8_t seed)
{
  for (size_t i = 0; i < len; ++i) {
    buf[i] = static_cast<uint8_t>(seed + i * 31u + (i >> 5));
  }
}

// Кодує кадр, згодовує свіжому декодувальнику одним шматком і звіряє все,
// що має збігтися. Повертає довжину кадру.
size_t checkRoundTrip(uint8_t type, const uint8_t* payload, size_t payloadLen)
{
  const size_t frameLen =
      remote_ui::encodeFrame(type, payload, payloadLen, g_frame,
                             sizeof(g_frame));
  CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + payloadLen);
  if (frameLen == 0) {
    return 0;
  }

  g_decoder.reset();
  g_capture.reset();

  const size_t found =
      g_decoder.feed(g_frame, frameLen, onPacket, &g_capture);

  CHECK_EQ(found, 1u);
  CHECK_EQ(g_capture.count, 1u);
  if (g_capture.count == 1) {
    CHECK_EQ(g_capture.types[0], type);
    CHECK_EQ(g_capture.lengths[0], payloadLen);
    if (g_capture.lengths[0] == payloadLen && payloadLen > 0) {
      CHECK_BYTES_EQ(g_capture.payloads[0], payload, payloadLen);
    }
  }

  return frameLen;
}

}  // namespace

// --- Константи --------------------------------------------------------

// Значення зі специфікації (docs/03-protocol.md) прибиті тестом: якщо їх
// колись підняти, не помітивши, усі інші тести підлаштуються символічно
// й нічого не спіймають — саме ці числа мають лишитись незмінними.
TEST(ProtocolConstantsMatchSpec)
{
  CHECK_EQ(MAX_PAYLOAD_SIZE, 4096u);
  CHECK_EQ(remote_ui::FRAME_OVERHEAD, 7u);
}

// --- Кодування ------------------------------------------------------------

// Точний вигляд кадру на дроті. Значення CRC пораховані незалежно, поза цим
// кодом; якщо тут щось поїде — поїде сумісність із мостом і клієнтом.
TEST(EncodeGoldenFrame)
{
  // PING без даних: E7 7E 86 00 00 | CRC 0x4566
  const uint8_t expectedPing[] = {0xE7, 0x7E, 0x86, 0x00, 0x00, 0x66, 0x45};
  size_t len = remote_ui::encodeFrame(remote_ui::PKT_PING, nullptr, 0, g_frame,
                                      sizeof(g_frame));
  CHECK_EQ(len, sizeof(expectedPing));
  CHECK_BYTES_EQ(g_frame, expectedPing, sizeof(expectedPing));

  // HELLO з одним байтом 0xAA: E7 7E 01 01 00 AA | CRC 0xD1E4
  const uint8_t oneByte[] = {0xAA};
  const uint8_t expectedHello[] = {0xE7, 0x7E, 0x01, 0x01,
                                   0x00, 0xAA, 0xE4, 0xD1};
  len = remote_ui::encodeFrame(remote_ui::PKT_HELLO, oneByte, sizeof(oneByte),
                               g_frame, sizeof(g_frame));
  CHECK_EQ(len, sizeof(expectedHello));
  CHECK_BYTES_EQ(g_frame, expectedHello, sizeof(expectedHello));
}

// Кодувальник не має права нічого зіпсувати за межами наданого буфера:
// на кожен сумнівний виклик — нуль і жодного запису.
TEST(EncodeRejectsBadArguments)
{
  fillPattern(g_payload, 32, 1);

  // Довжина понад стелю.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_TILE, g_payload,
                                  MAX_PAYLOAD_SIZE + 1, g_frame,
                                  sizeof(g_frame)),
           0u);

  // Буфера не вистачає рівно на один байт.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_TILE, g_payload, 10, g_frame,
                                  remote_ui::FRAME_OVERHEAD + 10 - 1),
           0u);

  // Буфера вистачає рівно.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_TILE, g_payload, 10, g_frame,
                                  remote_ui::FRAME_OVERHEAD + 10),
           remote_ui::FRAME_OVERHEAD + 10);

  // Немає даних, але обіцяна довжина.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_TILE, nullptr, 5, g_frame,
                                  sizeof(g_frame)),
           0u);

  // Немає буфера призначення.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_TILE, g_payload, 5, nullptr,
                                  sizeof(g_frame)),
           0u);

  // Порожній пакет із nullptr — законний випадок.
  CHECK_EQ(remote_ui::encodeFrame(remote_ui::PKT_FRAME_END, nullptr, 0,
                                  g_frame, sizeof(g_frame)),
           remote_ui::FRAME_OVERHEAD);
}

// --- Туди й назад ---------------------------------------------------------

TEST(RoundTripEmptyPayload)
{
  checkRoundTrip(remote_ui::PKT_FRAME_END, nullptr, 0);
}

TEST(RoundTripOneByte)
{
  const uint8_t payload[] = {0x5A};
  checkRoundTrip(remote_ui::PKT_ENC, payload, sizeof(payload));
}

TEST(RoundTripMaxPayload)
{
  fillPattern(g_payload, MAX_PAYLOAD_SIZE, 0x11);
  checkRoundTrip(remote_ui::PKT_TILE, g_payload, MAX_PAYLOAD_SIZE);
}

// Правило сумісності: невідомі типи шар кадрування не фільтрує, він віддає
// їх нагору як є. Рішення «не знаю такого» ухвалюється вище.
TEST(UnknownPacketTypeDelivered)
{
  const uint8_t payload[] = {0x01, 0x02, 0x03};
  checkRoundTrip(0x7F, payload, sizeof(payload));
}

// --- Потокове декодування -------------------------------------------------

// Головний тест шару: UART віддає дані як завгодно порізаними.
TEST(ByteByByteFeeding)
{
  fillPattern(g_payload, 137, 0x33);
  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_TILE, g_payload, 137, g_frame, sizeof(g_frame));
  CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + 137);

  g_decoder.reset();
  g_capture.reset();

  size_t found = 0;
  for (size_t i = 0; i < frameLen; ++i) {
    found += g_decoder.feed(&g_frame[i], 1, onPacket, &g_capture);
  }

  CHECK_EQ(found, 1u);
  CHECK_EQ(g_capture.count, 1u);
  if (g_capture.count == 1) {
    CHECK_EQ(g_capture.types[0], remote_ui::PKT_TILE);
    CHECK_EQ(g_capture.lengths[0], 137u);
    CHECK_BYTES_EQ(g_capture.payloads[0], g_payload, 137);
  }
}

// Розрив у кожній можливій позиції, включно з нульовою і останньою.
// Для кожної позиції — окремий декодувальник.
TEST(SplitAtEveryOffset)
{
  const size_t payloadLen = 300;
  fillPattern(g_payload, payloadLen, 0x77);

  // Хай усередині трапиться і маркер — розрив саме на ньому найцікавіший.
  g_payload[100] = remote_ui::FRAME_MARKER_0;
  g_payload[101] = remote_ui::FRAME_MARKER_1;

  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_TILE, g_payload, payloadLen, g_frame, sizeof(g_frame));
  CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + payloadLen);

  size_t badSplits = 0;

  for (size_t cut = 0; cut <= frameLen; ++cut) {
    Decoder decoder;
    g_capture.reset();

    size_t found = decoder.feed(g_frame, cut, onPacket, &g_capture);
    found += decoder.feed(&g_frame[cut], frameLen - cut, onPacket, &g_capture);

    if (found != 1 || g_capture.count != 1 ||
        g_capture.types[0] != remote_ui::PKT_TILE ||
        g_capture.lengths[0] != payloadLen ||
        memcmp(g_capture.payloads[0], g_payload, payloadLen) != 0) {
      ++badSplits;
    }
  }

  CHECK_EQ(badSplits, 0u);
}

// Маркер усередині корисних даних — річ звичайна. Декодувальник, який уже
// замкнувся на кадр, читає рівно LEN байт і на маркер не реагує.
TEST(MarkerInsidePayload)
{
  const size_t payloadLen = 64;
  fillPattern(g_payload, payloadLen, 0x05);

  // На початку, посередині й наприкінці — щоб зачепити всі стани.
  g_payload[0] = remote_ui::FRAME_MARKER_0;
  g_payload[1] = remote_ui::FRAME_MARKER_1;
  g_payload[30] = remote_ui::FRAME_MARKER_0;
  g_payload[31] = remote_ui::FRAME_MARKER_1;
  g_payload[62] = remote_ui::FRAME_MARKER_0;
  g_payload[63] = remote_ui::FRAME_MARKER_1;

  checkRoundTrip(remote_ui::PKT_TILE, g_payload, payloadLen);
}

// Сміття перед кадром, зокрема поодинокі E7 і 7E та послідовність E7 E7 7E
// (останній байт сміття зливається з маркером справжнього кадру).
TEST(GarbageBeforeFrame)
{
  const uint8_t garbage[] = {0x00, 0xFF, 0x7E, 0x12, 0xE7,
                             0x34, 0xAA, 0x55, 0xE7};

  const uint8_t payload[] = {0x81, 0x01};
  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_KEY, payload, sizeof(payload), g_frame, sizeof(g_frame));
  CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + sizeof(payload));

  memcpy(g_stream, garbage, sizeof(garbage));
  memcpy(&g_stream[sizeof(garbage)], g_frame, frameLen);

  g_decoder.reset();
  g_capture.reset();

  const size_t found = g_decoder.feed(g_stream, sizeof(garbage) + frameLen,
                                      onPacket, &g_capture);

  CHECK_EQ(found, 1u);
  CHECK_EQ(g_capture.count, 1u);
  if (g_capture.count == 1) {
    CHECK_EQ(g_capture.types[0], remote_ui::PKT_KEY);
    CHECK_EQ(g_capture.lengths[0], sizeof(payload));
    CHECK_BYTES_EQ(g_capture.payloads[0], payload, sizeof(payload));
  }
}

// Битий CRC. Важлива не так відмова від зіпсованого пакета, як те, що
// наступний за ним справжній пакет усе одно розбирається: помилка не має
// засліплювати надовго.
TEST(CorruptedCrcRecovers)
{
  const size_t payloadLen = 32;
  fillPattern(g_payload, payloadLen, 0x40);

  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_TILE, g_payload, payloadLen, g_frame, sizeof(g_frame));
  CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + payloadLen);

  // Перший кадр — зіпсований одним бітом усередині PAYLOAD. Межі кадру при
  // цьому лишаються визначеними, бо LEN недоторканий.
  memcpy(g_stream, g_frame, frameLen);
  g_stream[5 + payloadLen / 2] ^= 0x01;

  // Другий кадр — справжній, впритул, без розділювача.
  const uint8_t secondPayload[] = {0x02, 0x00, 0x00};
  const size_t secondLen =
      remote_ui::encodeFrame(remote_ui::PKT_TOUCH, secondPayload,
                             sizeof(secondPayload), &g_stream[frameLen],
                             sizeof(g_stream) - frameLen);
  CHECK_EQ(secondLen, remote_ui::FRAME_OVERHEAD + sizeof(secondPayload));

  g_decoder.reset();
  g_capture.reset();
  const uint32_t crcErrorsBefore = g_decoder.getCrcErrorCount();

  const size_t found =
      g_decoder.feed(g_stream, frameLen + secondLen, onPacket, &g_capture);

  CHECK_EQ(found, 1u);
  CHECK_EQ(g_capture.count, 1u);
  CHECK_EQ(g_decoder.getCrcErrorCount() - crcErrorsBefore, 1u);
  if (g_capture.count == 1) {
    CHECK_EQ(g_capture.types[0], remote_ui::PKT_TOUCH);
    CHECK_EQ(g_capture.lengths[0], sizeof(secondPayload));
    CHECK_BYTES_EQ(g_capture.payloads[0], secondPayload, sizeof(secondPayload));
  }
}

// Обіцяно 4096 байт, прийшло 10 і потік замовк. Ніякого зависання й ніякого
// виходу за межі внутрішнього буфера (це ловить санітайзер). Коли потік
// оживає — декодувальник дочитує обіцяне, відкидає кадр по CRC і бере
// наступний справжній.
TEST(TruncatedFrameDoesNotHang)
{
  // Заголовок обіцяє рівно MAX_PAYLOAD_SIZE = 4096 = 0x1000.
  const uint8_t header[] = {0xE7, 0x7E, remote_ui::PKT_TILE, 0x00, 0x10};

  g_decoder.reset();
  g_capture.reset();
  const uint32_t crcErrorsBefore = g_decoder.getCrcErrorCount();

  CHECK_EQ(g_decoder.feed(header, sizeof(header), onPacket, &g_capture), 0u);

  // Перші 10 байт даних — і тиша.
  fillPattern(g_payload, 10, 0x60);
  CHECK_EQ(g_decoder.feed(g_payload, 10, onPacket, &g_capture), 0u);

  // Порожній виклик і виклик без буфера нічого не ламають.
  CHECK_EQ(g_decoder.feed(g_payload, 0, onPacket, &g_capture), 0u);
  CHECK_EQ(g_decoder.feed(nullptr, 16, onPacket, &g_capture), 0u);

  // Незв'язаний шматок — для декодувальника це просто ще 6 байт даних.
  const uint8_t unrelated[] = {0x00, 0x11, 0x22, 0x33, 0x44, 0x55};
  CHECK_EQ(g_decoder.feed(unrelated, sizeof(unrelated), onPacket, &g_capture),
           0u);

  // Добиваємо обіцяне сміттям: 4096 - 10 - 6.
  const size_t remaining = MAX_PAYLOAD_SIZE - 10 - sizeof(unrelated);
  memset(g_payload, 0xA5, remaining);
  CHECK_EQ(g_decoder.feed(g_payload, remaining, onPacket, &g_capture), 0u);

  // Два байти CRC, які точно не зійдуться.
  const uint8_t badCrc[] = {0x00, 0x00};
  CHECK_EQ(g_decoder.feed(badCrc, sizeof(badCrc), onPacket, &g_capture), 0u);
  CHECK_EQ(g_decoder.getCrcErrorCount() - crcErrorsBefore, 1u);

  // А тепер справжній кадр — він має розібратися.
  const uint8_t payload[] = {0x2A};
  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_KEY, payload, sizeof(payload), g_frame, sizeof(g_frame));

  CHECK_EQ(g_decoder.feed(g_frame, frameLen, onPacket, &g_capture), 1u);
  CHECK_EQ(g_capture.count, 1u);
  if (g_capture.count == 1) {
    CHECK_EQ(g_capture.types[0], remote_ui::PKT_KEY);
    CHECK_EQ(g_capture.lengths[0], sizeof(payload));
    CHECK_BYTES_EQ(g_capture.payloads[0], payload, sizeof(payload));
  }
}

// Брехлива довжина. Ресинхронізація має початися одразу за LEN, а не після
// уявних 65535 байт — доводить це справжній кадр, покладений упритул.
TEST(OversizedLenRejectedImmediately)
{
  const uint8_t payload[] = {0x01};
  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_KEY, payload, sizeof(payload), g_frame, sizeof(g_frame));

  // LEN = 0xFFFF і LEN = 4097 — обидва понад стелю.
  const uint8_t liar1[] = {0xE7, 0x7E, remote_ui::PKT_TILE, 0xFF, 0xFF};
  const uint8_t liar2[] = {0xE7, 0x7E, remote_ui::PKT_TILE, 0x01, 0x10};

  size_t pos = 0;
  memcpy(&g_stream[pos], liar1, sizeof(liar1));
  pos += sizeof(liar1);
  memcpy(&g_stream[pos], g_frame, frameLen);
  pos += frameLen;
  memcpy(&g_stream[pos], liar2, sizeof(liar2));
  pos += sizeof(liar2);
  memcpy(&g_stream[pos], g_frame, frameLen);
  pos += frameLen;

  g_decoder.reset();
  g_capture.reset();
  const uint32_t oversizedBefore = g_decoder.getOversizedCount();

  const size_t found = g_decoder.feed(g_stream, pos, onPacket, &g_capture);

  CHECK_EQ(found, 2u);
  CHECK_EQ(g_capture.count, 2u);
  CHECK_EQ(g_decoder.getOversizedCount() - oversizedBefore, 2u);
  if (g_capture.count == 2) {
    CHECK_EQ(g_capture.types[0], remote_ui::PKT_KEY);
    CHECK_EQ(g_capture.types[1], remote_ui::PKT_KEY);
    CHECK_EQ(g_capture.lengths[0], sizeof(payload));
    CHECK_EQ(g_capture.lengths[1], sizeof(payload));
  }
}

// Мегабайт псевдовипадкових байтів із фіксованим зерном. Перевіряємо не
// вміст, а живучість: ні падіння, ні виходу за межі буфера (санітайзер),
// і після всього декодувальник придатний до роботи.
TEST(RandomGarbageDoesNotCrash)
{
  const size_t chunkSize = MAX_PAYLOAD_SIZE;
  const size_t totalBytes = 1024u * 1024u;

  std::mt19937 rng(12345);

  g_decoder.reset();
  g_capture.reset();

  for (size_t done = 0; done < totalBytes; done += chunkSize) {
    for (size_t i = 0; i < chunkSize; ++i) {
      g_payload[i] = static_cast<uint8_t>(rng() & 0xFF);
    }
    // Обробник із збирачем: якщо випадково складеться валідний пакет
    // (шанс мізерний), нічого не зламається.
    g_decoder.feed(g_payload, chunkSize, onPacket, &g_capture);
  }

  // Декодувальник живий і придатний до роботи.
  g_decoder.reset();
  g_capture.reset();

  const uint8_t payload[] = {0x03};
  const size_t frameLen = remote_ui::encodeFrame(
      remote_ui::PKT_KEY, payload, sizeof(payload), g_frame, sizeof(g_frame));

  CHECK_EQ(g_decoder.feed(g_frame, frameLen, onPacket, &g_capture), 1u);
  CHECK_EQ(g_capture.count, 1u);
}

// --- Динамічна пам'ять ----------------------------------------------------

// Кодування й декодування не мають виділяти пам'ять — узагалі.
// Каркас стежить за цим у кожному тесті; тут перевірка ще й явна.
TEST(NoHeapAllocations)
{
  const unsigned long before = test_alloc::guardedAllocations();

  fillPattern(g_payload, MAX_PAYLOAD_SIZE, 0x99);

  for (size_t len = 0; len <= MAX_PAYLOAD_SIZE; len += 1024) {
    const size_t frameLen = remote_ui::encodeFrame(
        remote_ui::PKT_TILE, g_payload, len, g_frame, sizeof(g_frame));
    CHECK_EQ(frameLen, remote_ui::FRAME_OVERHEAD + len);

    g_decoder.reset();
    g_capture.reset();
    CHECK_EQ(g_decoder.feed(g_frame, frameLen, onPacket, &g_capture), 1u);

    // І те саме по одному байту — найдовший шлях через автомат станів.
    g_decoder.reset();
    g_capture.reset();
    for (size_t i = 0; i < frameLen; ++i) {
      g_decoder.feed(&g_frame[i], 1, onPacket, &g_capture);
    }
    CHECK_EQ(g_capture.count, 1u);
  }

  CHECK_EQ(test_alloc::guardedAllocations() - before, 0u);
}

// Доказ, що попередній тест не порожній звук: сторож справді помічає
// виділення. Тут воно дозволене рівно одне.
TEST(AllocGuardDetectsAllocation)
{
  test_harness::g_expectedAllocations = 1;

  const unsigned long before = test_alloc::guardedAllocations();

  // volatile — щоб компілятор не викинув пару new/delete як зайву.
  uint8_t* volatile probe = new uint8_t[64];
  probe[0] = 0x01;

  CHECK_EQ(test_alloc::guardedAllocations() - before, 1u);

  delete[] probe;
}
