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
#include "protocol.h"  // коди пакетів для applyInputPacket

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

// Двобайтове поле протоколу, little-endian.
uint16_t readLe16(const uint8_t* p)
{
  return static_cast<uint16_t>(p[0] | (p[1] << 8));
}

// Чотирибайтове поле протоколу, little-endian.
uint32_t readLe32(const uint8_t* p)
{
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

}  // namespace

// --- Бік клієнта ------------------------------------------------------------

void InputState::markAlive(uint32_t nowMs)
{
  // Спершу час, потім ознака життя: якщо читач утрутиться між ними, він у
  // найгіршому разі побачить «живий, але давно» і відпустить ввід. Помилка в
  // безпечний бік.
  lastPacketMs.store(nowMs, std::memory_order_relaxed);
  linkActive.store(true, std::memory_order_relaxed);
}

void InputState::onKey(uint8_t key, bool pressed, uint32_t nowMs)
{
  markAlive(nowMs);

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
  markAlive(nowMs);

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
  // ⚠️ Позначка життя ставиться **до** раннього виходу, а не після. Нульове
  // клацання — це законний спосіб сказати «я живий і шлю ввід», нічого при
  // цьому не рухаючи; на ньому тримається проба на точкову втрату в
  // tools/input_check.py (режим «як було до 0008»: зв'язок живий за правилами
  // прошивки, але рівень не повторюється). Перенести markAlive нижче — тихо
  // зламати ту пробу.
  markAlive(nowMs);

  if (steps == 0) {
    return;
  }

  // Проміжок від попереднього клацання — те саме число, яке фізичний драйвер
  // додає у свій лічильник (`rotencDt += now - last_tick`). З нього EdgeTX
  // рахує прискорення ручки; без нього прискорення застигає на максимумі.
  //
  // Перше клацання й клацання після паузи дають ENCODER_DT_IDLE_MS, тобто
  // нульове прискорення: людина щойно почала крутити, розганяти нема чого.
  uint32_t dt = ENCODER_DT_IDLE_MS;
  if (hasLastEncoder) {
    const uint32_t elapsed = nowMs - lastEncoderMs;
    if (elapsed < ENCODER_DT_IDLE_MS) {
      dt = elapsed;
    }
  }
  lastEncoderMs = nowMs;
  hasLastEncoder = true;

  // ⚠️ Час публікується **раніше** за положення, і порядок тут обов'язковий.
  //
  // Читач у rotaryDriverRead() бере спершу положення, потім час. Витісни його
  // задача LVGL між нашими двома записами при зворотному порядку — і читач
  // побачив би нове положення без його часу: diff != 0, dt ≈ 0, прискорення
  // стрибає на максимум, тобто рівно та вада, яку ці рядки й лікують.
  //
  // При такому порядку найгірше, що станеться, — час, забраний наперед. Він
  // осяде в rotencDt, а `lastDt` при diff == 0 EdgeTX не оновлює (запис
  // стоїть усередині `if (diff != 0)`), тож наступного разу різниця
  // порахується правильно.
  encDtPending.fetch_add(dt, std::memory_order_relaxed);
  encAccum.fetch_add(steps, std::memory_order_relaxed);
}

void InputState::resyncAfterTimeout()
{
  producerDown = false;

  // Той самий слід, що лишає onDisconnect(). Скинутого прапорця тут мало:
  // пояснення, чому саме, — в input.h над оголошенням.
  linkDropSeq.store(touchSeq.load(std::memory_order_relaxed),
                    std::memory_order_relaxed);
  linkEpoch.fetch_add(1, std::memory_order_release);
}

void InputState::pushTouchTransition(int16_t x, int16_t y, bool pressed)
{
  const uint32_t seq = touchSeq.load(std::memory_order_relaxed);
  // Комірка — одне атомарне слово, а не структура з трьох полів. Це не
  // педантизм: читач ходить сюди раз на опитування LVGL (30 мс), а вісім
  // переходів клієнт устигає прислати за мілісекунду. Писар неминуче
  // перезаписуватиме комірку під читачем, і зі звичайної структури той міг би
  // забрати `pressed` від одного переходу з координатами від іншого — тобто
  // залипнути натиснутим.
  history[seq % TOUCH_HISTORY].store(packSample(x, y, pressed),
                                     std::memory_order_release);
  // Комірку має бути видно раніше за лічильник, інакше читач візьме ще не
  // заповнене місце.
  touchSeq.store(seq + 1, std::memory_order_release);
}

void InputState::onTouch(uint8_t event, int16_t x, int16_t y, uint32_t nowMs)
{
  // Перевірка обов'язково **до** markAlive: той відсуває позначку часу, і
  // тиша, яка щойно була, перестала б бути видною.
  //
  // Дотик, що лишався натиснутим на час тиші, застарів увесь: і наше «палець
  // унизу» (наступний DOWN мусить знову стати переходом, інакше віддалений
  // сенсор лишиться мертвим до першого UP), і те, що встиг забрати читач,
  // якщо він тайм-ауту ще не помітив. На UART розриву не існує взагалі, тож
  // саме цей шлях там основний, а не запасний.
  if (linkLost(nowMs)) {
    resyncAfterTimeout();
  }

  markAlive(nowMs);

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

  pushTouchTransition(cx, cy, wantDown);
}

void InputState::onInputState(uint32_t keys, uint32_t trims, bool touchDown,
                              int16_t x, int16_t y, uint32_t nowMs)
{
  // ⚠️ Порядок кроків тут не косметичний: кожен тримається на попередньому.
  // Міняти — тільки разом із поясненнями.

  // 1. Тиша перевіряється **до** markAlive: той відсуває позначку часу, і те,
  //    що читач уже встиг відпустити ввід сам, перестало б бути видно.
  const bool lost = linkLost(nowMs);

  // 2. ⚠️ Дотик, що пережив тишу, застарів увесь — і наш прапорець «палець
  //    унизу», і те, що встиг забрати читач. Скинути тут самий лише
  //    producerDown мало: наступним рядком markAlive зітре тишу, читач її вже
  //    не побачить, а переходу «відпущено» в кільці не буде (скидати ж
  //    нічого) — і дотик залипне назавжди, ще й заступивши фізичний сенсор.
  //    Розгорнуте пояснення — над оголошенням resyncAfterTimeout() в input.h.
  //
  //    Обов'язково до кроку 6: межа «усе до сюди — чуже» має лягти раніше за
  //    переходи, які цей самий пакет може дописати.
  if (lost) {
    resyncAfterTimeout();
  }

  // 3. Позначка життя — обов'язково **до** масок. Читач, що втрутиться між
  //    записом масок і оновленням позначки, побачив би прострочену позначку і
  //    обнулив би щойно записані маски.
  markAlive(nowMs);

  // 4. Присвоєння, а не |= і не &=: саме воно і є зняттям залипання
  //    (docs/03-protocol.md, правило 1). Клавіша, про відпускання якої пакет
  //    загубився, зникає з маски рівно тут.
  keysHeld.store(keys, std::memory_order_relaxed);
  trimsHeld.store(trims, std::memory_order_relaxed);

  // 5. ⚠️ keysLatched і trimsLatched не чіпаються **ніколи** — ані
  //    встановлюються, ані скидаються. Засувку гасить лише читач (takeMask)
  //    або onDisconnect(). Інакше повтор стану «нічого не утримується», що
  //    прийшов між натисканням і опитуванням клавіш, з'їв би коротке
  //    натискання цілком (правило 2). Це найтонше місце всього пакета.

  // 6. Дотик — тільки переходом, ніколи присвоєнням (правило 3).
  const int16_t cx = clampCoord(x, SCREEN_W);
  const int16_t cy = clampCoord(y, SCREEN_H);

  if (touchDown) {
    // Рівень «унизу» діє ще й як MOVE, тому точка оновлюється і тоді, коли
    // переходу немає: так безкоштовно лікується втрачений MOVE (правило 4).
    touchPoint.store(packSample(cx, cy, false), std::memory_order_relaxed);
    if (!producerDown) {
      producerDown = true;
      pushTouchTransition(cx, cy, true);
    }
  } else if (producerDown) {
    // Синтетичне відпускання бере **останню відому точку**: координати при
    // біт0=0 не читаються взагалі (правило 3). Кільце при цьому не скидається
    // й не перемотується — непрочитаний натиск лишається непрочитаним і піде
    // читачеві попереду цього відпускання.
    int16_t lastX = 0;
    int16_t lastY = 0;
    bool ignored = false;
    unpackSample(touchPoint.load(std::memory_order_relaxed), lastX, lastY,
                 ignored);
    producerDown = false;
    pushTouchTransition(lastX, lastY, false);
  }
  // Рівень «пальця немає» при внутрішньому «немає» не робить нічого взагалі.

  // 7. Накопичувач енкодера не чіпається за жодних умов (правило 5): рівень
  //    замість накопичення дав би фантомний оберт назад.
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
  //
  // Разом із ним лишається і невибраний проміжок часу: клацання, яке ще не
  // дійшло до EdgeTX, дійде після розриву — і має принести свій час із собою,
  // інакше отримає максимальне прискорення.
  //
  // А ось відлік «коли було попереднє клацання» скидається: наступний клієнт
  // почне крутити з чистого аркуша, а не продовжить розгін попереднього.
  hasLastEncoder = false;

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

uint32_t InputState::takeEncoderDtMs()
{
  // Споживання, а не читання: лічильник EdgeTX накопичувальний, тож віддати
  // той самий проміжок двічі означало б розтягнути час і занизити прискорення.
  return encDtPending.exchange(0, std::memory_order_relaxed);
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
  encDtPending.store(0, std::memory_order_relaxed);
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
  lastEncoderMs = 0;
  hasLastEncoder = false;
  touchEpoch = 0;
  touchTaken = 0;
  consumerDown = false;
  consumerX = 0;
  consumerY = 0;
}

// --- Пакет клієнта -> стан вводу --------------------------------------------

bool applyInputPacket(InputState& input, uint8_t type, const uint8_t* payload,
                      size_t length, uint32_t nowMs)
{
  if (payload == nullptr && length > 0) {
    return false;
  }

  switch (type) {
    case PKT_KEY:
      if (length >= 2) {
        input.onKey(payload[0], payload[1] != 0, nowMs);
        return true;
      }
      return false;

    case PKT_ENC:
      if (length >= 1) {
        input.onEncoder(static_cast<int8_t>(payload[0]), nowMs);
        return true;
      }
      return false;

    case PKT_TOUCH:
      if (length >= 5) {
        input.onTouch(payload[0],
                      static_cast<int16_t>(readLe16(payload + 1)),
                      static_cast<int16_t>(readLe16(payload + 3)), nowMs);
        return true;
      }
      return false;

    case PKT_TRIM:
      if (length >= 2) {
        input.onTrim(payload[0], payload[1] != 0, nowMs);
        return true;
      }
      return false;

    case PKT_INPUT_STATE:
      // ⚠️ Коротший за INPUT_STATE_PAYLOAD_SIZE пакет не застосовується
      // взагалі — і позначку життя теж не оновлює. Це зіпсований пакет, а
      // помилятися треба в бік відпускання: інакше побитий вантаж міг би
      // нескінченно тримати клавішу натиснутою, нічого при цьому не
      // означаючи.
      //
      // Довший — застосовуються перші INPUT_STATE_PAYLOAD_SIZE байтів, решта
      // ігнорується мовчки: протокол дозволяє додавати поля тільки в кінець,
      // тож хвіст від новішого клієнта нам просто невідомий.
      if (length >= INPUT_STATE_PAYLOAD_SIZE) {
        input.onInputState(readLe32(payload), readLe32(payload + 4),
                           (payload[8] & 0x01) != 0,
                           static_cast<int16_t>(readLe16(payload + 9)),
                           static_cast<int16_t>(readLe16(payload + 11)), nowMs);
        return true;
      }
      return false;

    // PING, REFRESH і невідомі типи вводу не несуть, тому тайм-аут не
    // відсувають (пояснення вгорі input.h). Що з ними робити — справа
    // транспорту.
    default:
      return false;
  }
}

// Єдиний примірник. Не функція-статик: там компілятор додав би сторожа
// ініціалізації (`__cxa_guard_acquire`), тобто блокування на кожному зверненні
// з двох потоків. Тут усе ініціалізується нулями ще до старту програми.
InputState g_input;

InputState& inputState() { return g_input; }

}  // namespace remote_ui

#endif  // REMOTE_UI
