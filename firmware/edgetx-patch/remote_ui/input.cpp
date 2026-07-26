/*
 * TX16S Remote UI — емульований ввід, реалізація.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Пояснення й межі — в input.h. Тут лише механіка.
 */

#if defined(REMOTE_UI)

#include "input.h"

#include "geometry.h"

namespace remote_ui {

namespace {

int16_t clampCoord(int16_t value, int limit)
{
  if (value < 0) {
    return 0;
  }
  if (value >= limit) {
    return static_cast<int16_t>(limit - 1);
  }
  return value;
}

}  // namespace

// --- Бік клієнта ------------------------------------------------------------

void InputState::onAnyPacket(uint32_t nowMs)
{
  // Спершу час, потім ознака життя: якщо читач утрутиться між ними, він у
  // найгіршому разі побачить «живий, але давно» і відпустить ввід. Помилка в
  // безпечний бік.
  lastPacketMs.store(nowMs, std::memory_order_relaxed);
  linkActive.store(true, std::memory_order_relaxed);
}

void InputState::onKey(uint8_t key, bool pressed, uint32_t nowMs)
{
  onAnyPacket(nowMs);

  if (key >= INPUT_MAX_BITS) {
    return;  // у 32-бітову маску не влазить — не наша клавіша
  }

  const uint32_t bit = 1u << key;
  if (pressed) {
    // Спершу засувка, потім «натиснуто»: інакше читач, що втрутився між двома
    // операціями, побачив би натискання без засувки й міг би загубити його,
    // якби клавішу відпустили до наступного читання.
    keysLatched.fetch_or(bit, std::memory_order_relaxed);
    keysHeld.fetch_or(bit, std::memory_order_relaxed);
  } else {
    keysHeld.fetch_and(~bit, std::memory_order_relaxed);
  }
}

void InputState::onTrim(uint8_t index, bool pressed, uint32_t nowMs)
{
  onAnyPacket(nowMs);

  if (index >= INPUT_MAX_BITS) {
    return;
  }

  const uint32_t bit = 1u << index;
  if (pressed) {
    trimsLatched.fetch_or(bit, std::memory_order_relaxed);
    trimsHeld.fetch_or(bit, std::memory_order_relaxed);
  } else {
    trimsHeld.fetch_and(~bit, std::memory_order_relaxed);
  }
}

void InputState::onEncoder(int8_t steps, uint32_t nowMs)
{
  onAnyPacket(nowMs);

  if (steps == 0) {
    return;
  }
  encAccum.fetch_add(steps, std::memory_order_relaxed);
}

void InputState::onTouch(uint8_t event, int16_t x, int16_t y, uint32_t nowMs)
{
  // Перевірка обов'язково **до** onAnyPacket: той відсуває позначку часу, і
  // тиша, яка щойно була, перестала б бути видною.
  //
  // Читач міг відпустити дотик сам, за тайм-аутом тиші. Тоді наше «палець
  // унизу» застаріло, і наступний DOWN мусить знову стати переходом — інакше
  // віддалений сенсор лишився б мертвим до першого UP. На UART розриву не
  // існує взагалі, тож саме цей шлях там основний, а не запасний.
  if (linkLost(nowMs)) {
    producerDown = false;
  }

  onAnyPacket(nowMs);

  const int16_t cx = clampCoord(x, SCREEN_W);
  const int16_t cy = clampCoord(y, SCREEN_H);
  touchPoint.store(packSample(cx, cy, false), std::memory_order_relaxed);

  bool wantDown;
  switch (event) {
    case TOUCH_EVENT_DOWN:
      wantDown = true;
      break;
    case TOUCH_EVENT_UP:
      wantDown = false;
      break;
    case TOUCH_EVENT_MOVE:
      return;  // рух лише пересуває точку, переходом не є
    default:
      return;  // невідома подія — мовчки повз (правило сумісності)
  }

  if (wantDown == producerDown) {
    return;  // повторне «натиснуто» чи «відпущено» — теж не перехід
  }
  producerDown = wantDown;

  const uint32_t seq = touchSeq.load(std::memory_order_relaxed);
  // Комірка — одне атомарне слово, а не структура з трьох полів. Це не
  // педантизм: читач ходить сюди раз на опитування LVGL (30 мс), а вісім
  // переходів клієнт устигає прислати за мілісекунду. Писар неминуче
  // перезаписуватиме комірку під читачем, і зі звичайної структури той міг би
  // забрати `pressed` від одного переходу з координатами від іншого — тобто
  // залипнути натиснутим.
  history[seq % TOUCH_HISTORY].store(packSample(cx, cy, wantDown),
                                     std::memory_order_release);
  // Комірку має бути видно раніше за лічильник, інакше читач візьме ще не
  // заповнене місце.
  touchSeq.store(seq + 1, std::memory_order_release);
}

void InputState::onDisconnect()
{
  keysHeld.store(0, std::memory_order_relaxed);
  keysLatched.store(0, std::memory_order_relaxed);
  trimsHeld.store(0, std::memory_order_relaxed);
  trimsLatched.store(0, std::memory_order_relaxed);

  producerDown = false;

  // Позначаємо, де скінчився цей клієнт. Читач, який опитає сенсор уже після
  // того, як під'єднався наступний, за цією позначкою відкине все, що встиг
  // наробити попередній, і відпустить дотик замість програти його як свій.
  linkDropSeq.store(touchSeq.load(std::memory_order_relaxed),
                    std::memory_order_relaxed);
  linkEpoch.fetch_add(1, std::memory_order_release);

  // Накопичувач енкодера навмисно лишається як є: він не «натиснутий стан», а
  // положення. Обнулення дало б фантомний оберт назад на всю накопичену суму.

  linkActive.store(false, std::memory_order_relaxed);
}

// --- Бік EdgeTX -------------------------------------------------------------

uint32_t InputState::takeMask(std::atomic<uint32_t>& held,
                              std::atomic<uint32_t>& latched, bool lost)
{
  if (lost) {
    held.store(0, std::memory_order_relaxed);
    latched.store(0, std::memory_order_relaxed);
    return 0;
  }
  const uint32_t pressed = held.load(std::memory_order_relaxed);
  return pressed | latched.exchange(0, std::memory_order_relaxed);
}

uint32_t InputState::takeKeys(uint32_t nowMs)
{
  return takeMask(keysHeld, keysLatched, linkLost(nowMs));
}

uint32_t InputState::takeTrims(uint32_t nowMs)
{
  return takeMask(trimsHeld, trimsLatched, linkLost(nowMs));
}

int32_t InputState::encoderOffset() const
{
  return encAccum.load(std::memory_order_relaxed);
}

bool InputState::popTouch(int16_t& x, int16_t& y, bool& pressed, uint32_t nowMs)
{
  const uint32_t epoch = linkEpoch.load(std::memory_order_acquire);
  const bool lost = linkLost(nowMs);

  if (lost || epoch != touchEpoch) {
    // Клієнта, який тримав цей дотик, більше немає: або мовчить понад
    // тайм-аут, або з'єднання вже інше. Усе, що він устиг наробити, нікого не
    // стосується — а натиснутий дотик закривається відпусканням.
    touchEpoch = epoch;
    touchTaken = lost ? touchSeq.load(std::memory_order_acquire)
                      : linkDropSeq.load(std::memory_order_relaxed);
    if (consumerDown) {
      consumerDown = false;
      x = consumerX;
      y = consumerY;
      pressed = false;
      return true;  // одне відпускання — і драйвер знову вільний
    }
    if (lost) {
      return false;
    }
    // Зв'язок уже новий: далі йдемо звичайним шляхом, у нього можуть бути
    // власні події.
  }

  const uint32_t seq = touchSeq.load(std::memory_order_acquire);

  // Читач відстав більше, ніж уміщає історія: найстаріші переходи вже затерті.
  // Доганяємо. Останній перехід у історії лишається завжди, тому застрягти
  // «натиснутим» після відпускання неможливо.
  if (seq - touchTaken >= TOUCH_HISTORY) {
    touchTaken = seq - (TOUCH_HISTORY - 1);
  }

  if (touchTaken != seq) {
    const uint32_t slot =
        history[touchTaken % TOUCH_HISTORY].load(std::memory_order_acquire);
    unpackSample(slot, consumerX, consumerY, consumerDown);
    ++touchTaken;
    x = consumerX;
    y = consumerY;
    pressed = consumerDown;
    return true;
  }

  if (consumerDown) {
    // Дотик триває. Віддаємо найсвіжішу точку, щоб перетягування було плавним,
    // а довге натискання — довгим.
    bool ignored = false;
    unpackSample(touchPoint.load(std::memory_order_relaxed), consumerX,
                 consumerY, ignored);
    x = consumerX;
    y = consumerY;
    pressed = true;
    return true;
  }

  return false;
}

// --- Спільне ----------------------------------------------------------------

bool InputState::linkLost(uint32_t nowMs) const
{
  if (!linkActive.load(std::memory_order_relaxed)) {
    return true;
  }
  const uint32_t silence = nowMs - lastPacketMs.load(std::memory_order_relaxed);
  return silence > INPUT_RELEASE_TIMEOUT_MS;
}

// Координати вже обрізані по екрану, тому лізуть у 15 біт із запасом, а
// прапорець «натиснуто» — у шістнадцятий. Одне слово замість трьох полів
// потрібне, щоб читач не міг забрати напівоновлений стан (див. onTouch).
static_assert(SCREEN_W <= 32768 && SCREEN_H <= 32768,
              "координати дотику пакуються в 15 біт");

uint32_t InputState::packSample(int16_t x, int16_t y, bool pressed)
{
  return (static_cast<uint32_t>(x) & 0x7FFF) |
         ((static_cast<uint32_t>(y) & 0x7FFF) << 15) |
         (pressed ? (1u << 30) : 0u);
}

void InputState::unpackSample(uint32_t word, int16_t& x, int16_t& y,
                              bool& pressed)
{
  x = static_cast<int16_t>(word & 0x7FFF);
  y = static_cast<int16_t>((word >> 15) & 0x7FFF);
  pressed = (word & (1u << 30)) != 0;
}

void InputState::reset()
{
  keysHeld.store(0, std::memory_order_relaxed);
  keysLatched.store(0, std::memory_order_relaxed);
  trimsHeld.store(0, std::memory_order_relaxed);
  trimsLatched.store(0, std::memory_order_relaxed);
  encAccum.store(0, std::memory_order_relaxed);
  linkActive.store(false, std::memory_order_relaxed);
  lastPacketMs.store(0, std::memory_order_relaxed);
  linkEpoch.store(0, std::memory_order_relaxed);
  linkDropSeq.store(0, std::memory_order_relaxed);
  touchPoint.store(0, std::memory_order_relaxed);
  touchSeq.store(0, std::memory_order_relaxed);
  for (uint32_t i = 0; i < TOUCH_HISTORY; ++i) {
    history[i].store(0, std::memory_order_relaxed);
  }
  producerDown = false;
  touchEpoch = 0;
  touchTaken = 0;
  consumerDown = false;
  consumerX = 0;
  consumerY = 0;
}

// Єдиний примірник. Не функція-статик: там компілятор додав би сторожа
// ініціалізації (`__cxa_guard_acquire`), тобто блокування на кожному зверненні
// з двох потоків. Тут усе ініціалізується нулями ще до старту програми.
InputState g_input;

InputState& inputState() { return g_input; }

}  // namespace remote_ui

#endif  // REMOTE_UI
