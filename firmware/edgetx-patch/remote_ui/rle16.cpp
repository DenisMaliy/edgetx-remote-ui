/*
 * TX16S Remote UI — стиснення RLE16, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 */

#if defined(REMOTE_UI)

#include "rle16.h"

namespace remote_ui {

size_t rle16Encode(const uint16_t* pixels, size_t count, uint8_t* out,
                   size_t outSize)
{
  if (pixels == nullptr || out == nullptr || count == 0) {
    return 0;
  }

  size_t pos = 0;
  size_t i = 0;

  while (i < count) {
    const uint16_t px = pixels[i];

    // Лічильник — один байт, тому серія обрізається на 255 і продовжується
    // наступною парою.
    size_t run = 1;
    while (run < 255 && i + run < count && pixels[i + run] == px) {
      ++run;
    }

    // Не вмістились — викликач піде сирим шляхом. Виходимо одразу, не
    // дописуючи обрізаного «хвоста»: половина пар гірша за відсутність даних.
    if (pos + 3 > outSize) {
      return 0;
    }

    out[pos++] = static_cast<uint8_t>(run);
    out[pos++] = static_cast<uint8_t>(px & 0xFF);
    out[pos++] = static_cast<uint8_t>((px >> 8) & 0xFF);

    i += run;
  }

  return pos;
}

size_t rle16Decode(const uint8_t* data, size_t len, uint16_t* out,
                   size_t outPixels)
{
  if (data == nullptr || out == nullptr) {
    return 0;
  }

  // Довжина, не кратна трьом, означає обрізаний або битий потік.
  if (len == 0 || (len % 3) != 0) {
    return 0;
  }

  size_t written = 0;

  for (size_t p = 0; p < len; p += 3) {
    const uint8_t run = data[p];
    if (run == 0) {
      return 0;  // лічильник 0 форматом не передбачений
    }

    const uint16_t px =
        static_cast<uint16_t>(data[p + 1] | (data[p + 2] << 8));

    if (written + run > outPixels) {
      return 0;  // розпаковане не влазить — краще нічого, ніж за межі буфера
    }

    for (uint8_t k = 0; k < run; ++k) {
      out[written++] = px;
    }
  }

  return written;
}

}  // namespace remote_ui

#endif  // REMOTE_UI
