/*
 * TX16S Remote UI — кадрування протоколу (найнижчий шар).
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Тут немає ані картинки, ані стиснення, ані вводу — тільки перетворення
 * потоку байтів на пакети й назад. Формат описаний у docs/03-protocol.md:
 *
 *   +------+------+------+--------+-----------+--------+
 *   | 0xE7 | 0x7E | TYPE | LEN:2  | PAYLOAD   | CRC:2  |
 *   +------+------+------+--------+-----------+--------+
 *                          LE       LEN байт     LE
 *
 *   LEN — довжина PAYLOAD, 0..4096, little-endian.
 *   CRC — CRC-16/CCITT-FALSE по TYPE + LEN(2 байти) + PAYLOAD.
 *         Маркер і сам CRC у розрахунок не входять.
 *
 * Шар не знає нічого про залізо: ані роздільності, ані переліку клавіш.
 * Усе це — вміст PAYLOAD, справа верхніх шарів.
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

namespace remote_ui {

// --- Кадрування -----------------------------------------------------------

// Маркер синхронізації, два байти.
constexpr uint8_t FRAME_MARKER_0 = 0xE7;
constexpr uint8_t FRAME_MARKER_1 = 0x7E;

// Стеля довжини корисних даних. Більше — кадр відкидається одразу після LEN.
constexpr size_t MAX_PAYLOAD_SIZE = 4096;

// Decoder зберігає прийняту довжину й позицію в uint16_t (нижче) — якщо
// колись підняти стелю понад 65535, перевірка "declaredLen > MAX_PAYLOAD_SIZE"
// перестане спрацьовувати і Payload зациклиться замість виходу за буфер.
static_assert(MAX_PAYLOAD_SIZE <= 65535,
             "declaredLen і payloadPos у Decoder — uint16_t");

// Службові байти кадру: 2 маркер + 1 TYPE + 2 LEN + 2 CRC.
constexpr size_t FRAME_OVERHEAD = 7;

// Найбільший можливий кадр — розмір буфера передачі рахують від нього.
constexpr size_t MAX_FRAME_SIZE = FRAME_OVERHEAD + MAX_PAYLOAD_SIZE;

// --- Коди пакетів (docs/03-protocol.md) -----------------------------------
//
// Це частина протоколу, спільна для всіх цілей EdgeTX, а не специфіка пульта.
// Шар кадрування коди не тлумачить і невідомі не фільтрує: правило сумісності
// вимагає віддавати нагору все, що дійшло з правильним CRC.

enum PacketType : uint8_t {
  // Пульт -> клієнт
  PKT_HELLO = 0x01,
  PKT_TILE = 0x02,
  PKT_FRAME_END = 0x03,
  PKT_STATE = 0x04,
  PKT_LOG = 0x05,

  // Клієнт -> пульт
  PKT_KEY = 0x81,
  PKT_ENC = 0x82,
  PKT_TOUCH = 0x83,
  PKT_REFRESH = 0x84,
  PKT_TRIM = 0x85,
  PKT_PING = 0x86,
  PKT_INPUT_STATE = 0x87,

  // Зарезервовано під файлові операції (наступні етапи)
  PKT_FILE_LIST = 0x90,
  PKT_FILE_READ = 0x91,
  PKT_FILE_WRITE = 0x92,
  PKT_FILE_STAT = 0x93,
};

// --- Кодування ------------------------------------------------------------

// Складає готовий кадр у наданий буфер.
// Повертає довжину кадру в байтах або 0, якщо:
//   - outBuf == nullptr;
//   - payloadLen > MAX_PAYLOAD_SIZE;
//   - payload == nullptr при payloadLen > 0;
//   - у outBuf не вистачає місця (треба FRAME_OVERHEAD + payloadLen).
// Нічого не виділяє й нічого не чекає — уся пам'ять приходить ззовні.
size_t encodeFrame(uint8_t type, const uint8_t* payload, size_t payloadLen,
                   uint8_t* outBuf, size_t outBufSize);

// --- Декодування ----------------------------------------------------------

// Викликається синхронно з feed() на кожен розібраний пакет із правильним CRC.
// payload дійсний лише до повернення з обробника: це внутрішній буфер
// декодувальника, який наступний байт може переписати.
//
// Обробник виконується в контексті виклику feed() — не має права блокувати
// (може викликатись до сотень разів за один шматок вхідних даних) і не має
// права викликати feed()/reset() того самого Decoder: це затре state,
// declaredLen і payloadBuf посеред розбору поточного кадру.
using PacketHandler = void (*)(uint8_t type, const uint8_t* payload,
                               size_t length, void* context);

// Потоковий декодувальник. Стан живе всередині об'єкта, тому вхід можна
// різати як завгодно — хоч по одному байту. Об'єкт великий (буфер на
// MAX_PAYLOAD_SIZE), тож заводиться один раз статично, а не на стеку.
class Decoder
{
 public:
  Decoder();

  // Об'єкт важить 4+ КБ (буфер найбільшого PAYLOAD). Випадкове копіювання —
  // це тихий memcpy на кілька кілобайтів у прошивці; забороняємо явно.
  Decoder(const Decoder&) = delete;
  Decoder& operator=(const Decoder&) = delete;

  // Повертає декодувальник у стан пошуку маркера, лічильники не чіпає.
  void reset();

  // Згодовує шматок потоку. Повертає кількість пакетів, відданих обробнику
  // під час цього виклику. handler може бути nullptr — тоді пакети просто
  // рахуються й відкидаються.
  size_t feed(const uint8_t* data, size_t len, PacketHandler handler,
              void* context);

  // Лічильники для діагностики (переповнення нешкідливе, це не гроші).
  uint32_t getPacketCount() const { return packetCount; }
  uint32_t getCrcErrorCount() const { return crcErrorCount; }
  uint32_t getOversizedCount() const { return oversizedCount; }

 private:
  enum class State : uint8_t {
    Marker0,  // шукаємо 0xE7
    Marker1,  // маємо 0xE7, чекаємо 0x7E
    Type,     // читаємо TYPE
    LenLow,   // читаємо молодший байт LEN
    LenHigh,  // читаємо старший байт LEN
    Payload,  // читаємо рівно declaredLen байт
    CrcLow,   // читаємо молодший байт CRC
    CrcHigh,  // читаємо старший байт CRC і звіряємо
  };

  // Обробляє один байт. Повертає true, коли в payloadBuf лежить цілий пакет
  // із правильним CRC.
  bool feedByte(uint8_t byte);

  State state;
  uint8_t type;            // TYPE поточного кадру
  uint16_t declaredLen;    // LEN поточного кадру
  uint16_t payloadPos;     // скільки байт payload уже прийнято
  uint16_t crcCalc;        // CRC, порахований на льоту
  uint16_t crcReceived;    // CRC, узятий із кадру

  uint32_t packetCount;
  uint32_t crcErrorCount;
  uint32_t oversizedCount;

  uint8_t payloadBuf[MAX_PAYLOAD_SIZE];
};

}  // namespace remote_ui
