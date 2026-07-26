/*
 * TX16S Remote UI — прив'язка емульованого вводу до EdgeTX.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Це весь код, який знає одночасно і про наш стан вводу (input.h), і про
 * EdgeTX. Чотири функції, кожна на два рядки: годинник, опис заліза, виклик.
 * Сам автомат вводу лишається чистим і перевіряється тестами без EdgeTX.
 *
 * Звідси нічого не викликається саме по собі: функції смикають гачки 6 і 7 з
 * docs/05-hooks.md, тобто задачі EdgeTX.
 */

#if defined(REMOTE_UI) && !defined(REMOTE_UI_STANDALONE)

#include "input.h"
#include "remote_ui.h"

#include "hal/key_driver.h"  // keysGetSupported, keysGetMaxTrims
#include "os/time.h"         // time_get_ms

uint32_t remoteUiGetKeys()
{
  // Маска підтримуваних клавіш — остання перешкода для клієнта, який шле
  // код клавіші, якої на цій цілі немає. У протоколі коди спільні для всіх
  // пультів, а набір клавіш у кожного свій.
  return remote_ui::inputState().takeKeys(time_get_ms()) & keysGetSupported();
}

uint32_t remoteUiGetTrims()
{
  // У EdgeTX біти тримерів — це напрямки: на кожен тример два (менше/більше).
  const uint32_t directions = static_cast<uint32_t>(keysGetMaxTrims()) * 2;
  const uint32_t mask =
      (directions >= 32) ? 0xFFFFFFFFu : ((1u << directions) - 1);

  return remote_ui::inputState().takeTrims(time_get_ms()) & mask;
}

int32_t remoteUiGetEncoderOffset()
{
  return remote_ui::inputState().encoderOffset();
}

bool remoteUiPopTouch(int16_t* x, int16_t* y, bool* pressed)
{
  if (x == nullptr || y == nullptr || pressed == nullptr) {
    return false;
  }
  return remote_ui::inputState().popTouch(*x, *y, *pressed, time_get_ms());
}

#endif  // REMOTE_UI && !REMOTE_UI_STANDALONE
