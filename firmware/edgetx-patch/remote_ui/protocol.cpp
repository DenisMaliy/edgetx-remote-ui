/*
 * TX16S Remote UI — кадрування протоколу, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 */

#ifdef REMOTE_UI

#include "protocol.h"

#include <string.h>

#include "crc16.h"

namespace remote_ui {

// --- Кодування ------------------------------------------------------------

size_t encodeFrame(uint8_t type, const uint8_t* payload, size_t payloadLen,
                   uint8_t* outBuf, size_t outBufSize)
{
  if (outBuf == nullptr) {
    return 0;
  }

  if (payloadLen > MAX_PAYLOAD_SIZE) {
    return 0;
  }

  if (payload == nullptr && payloadLen > 0) {
    return 0;
  }

  const size_t frameLen = FRAME_OVERHEAD + payloadLen;
  if (outBufSize < frameLen) {
    return 0;
  }

  outBuf[0] = FRAME_MARKER_0;
  outBuf[1] = FRAME_MARKER_1;
  outBuf[2] = type;
  outBuf[3] = static_cast<uint8_t>(payloadLen & 0xFF);
  outBuf[4] = static_cast<uint8_t>((payloadLen >> 8) & 0xFF);

  if (payloadLen > 0) {
    memcpy(&outBuf[5], payload, payloadLen);
  }

  // CRC рахується по TYPE + LEN + PAYLOAD; маркер до нього не входить.
  const uint16_t crc = crc16(&outBuf[2], 3 + payloadLen);

  outBuf[5 + payloadLen] = static_cast<uint8_t>(crc & 0xFF);
  outBuf[6 + payloadLen] = static_cast<uint8_t>((crc >> 8) & 0xFF);

  return frameLen;
}

// --- Декодування ----------------------------------------------------------

Decoder::Decoder()
    : state(State::Marker0),
      type(0),
      declaredLen(0),
      payloadPos(0),
      crcCalc(CRC16_INIT),
      crcReceived(0),
      packetCount(0),
      crcErrorCount(0),
      oversizedCount(0),
      payloadBuf()
{
}

void Decoder::reset()
{
  state = State::Marker0;
  type = 0;
  declaredLen = 0;
  payloadPos = 0;
  crcCalc = CRC16_INIT;
  crcReceived = 0;
}

bool Decoder::feedByte(uint8_t byte)
{
  switch (state) {
    case State::Marker0:
      if (byte == FRAME_MARKER_0) {
        state = State::Marker1;
      }
      break;

    case State::Marker1:
      if (byte == FRAME_MARKER_1) {
        // Замкнулись на кадр: далі читаємо рівно те, що обіцяє заголовок.
        crcCalc = CRC16_INIT;
        state = State::Type;
      } else if (byte == FRAME_MARKER_0) {
        // Послідовність E7 E7 7E теж має спрацювати: лишаємось тут.
      } else {
        state = State::Marker0;
      }
      break;

    case State::Type:
      type = byte;
      crcCalc = crc16Update(crcCalc, byte);
      state = State::LenLow;
      break;

    case State::LenLow:
      declaredLen = byte;
      crcCalc = crc16Update(crcCalc, byte);
      state = State::LenHigh;
      break;

    case State::LenHigh:
      declaredLen = static_cast<uint16_t>(declaredLen |
                                          (static_cast<uint16_t>(byte) << 8));
      crcCalc = crc16Update(crcCalc, byte);

      if (declaredLen > MAX_PAYLOAD_SIZE) {
        // Брехлива довжина. Жодної спроби прочитати чи пропустити стільки
        // байтів — ресинхронізація починається з наступного байта.
        ++oversizedCount;
        state = State::Marker0;
      } else if (declaredLen == 0) {
        state = State::CrcLow;
      } else {
        payloadPos = 0;
        state = State::Payload;
      }
      break;

    case State::Payload:
      // payloadPos < declaredLen <= MAX_PAYLOAD_SIZE — вихід за буфер
      // неможливий за побудовою.
      payloadBuf[payloadPos++] = byte;
      crcCalc = crc16Update(crcCalc, byte);
      if (payloadPos >= declaredLen) {
        state = State::CrcLow;
      }
      break;

    case State::CrcLow:
      crcReceived = byte;
      state = State::CrcHigh;
      break;

    case State::CrcHigh:
      crcReceived = static_cast<uint16_t>(crcReceived |
                                          (static_cast<uint16_t>(byte) << 8));
      // Незалежно від результату звірки повертаємось до пошуку маркера —
      // саме з цього місця, а не з середини щойно прочитаного payload.
      state = State::Marker0;

      if (crcReceived == crcCalc) {
        ++packetCount;
        return true;
      }

      ++crcErrorCount;
      break;
  }

  return false;
}

size_t Decoder::feed(const uint8_t* data, size_t len, PacketHandler handler,
                     void* context)
{
  size_t found = 0;

  if (data == nullptr) {
    return 0;
  }

  for (size_t i = 0; i < len; ++i) {
    if (feedByte(data[i])) {
      ++found;
      if (handler != nullptr) {
        handler(type, payloadBuf, declaredLen, context);
      }
    }
  }

  return found;
}

}  // namespace remote_ui

#endif  // REMOTE_UI
