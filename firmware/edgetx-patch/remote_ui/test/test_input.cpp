/*
 * Тести емульованого вводу.
 *
 * Головне тут — не «клавіша натиснулась», а **клавіша відпустилась**.
 * Залипла емульована клавіша при обриві означає, що пульт натискає щось сам
 * по собі, і людина цього не побачить, бо екрана немає. Тому окремо
 * перевіряється кожен шлях відпускання: розрив, тиша в каналі, перепідключення.
 */

#include "input.h"

#include "geometry.h"  // SCREEN_W/SCREEN_H — межі, по яких обрізається дотик
#include "protocol.h"  // коди пакетів: частина тестів ходить шляхом транспорту
#include "test_harness.h"

using namespace remote_ui;

namespace {

// Умовні коди клавіш. Справжні беруться з EnumKeys, але цей шар про них не
// знає — і саме це перевіряється: він працює з номерами бітів, а не з TX16S.
constexpr uint8_t KEY_A = 2;
constexpr uint8_t KEY_B = 7;

constexpr uint32_t T0 = 1000;

// Час, коли тайм-аут уже точно минув.
constexpr uint32_t T_LOST = T0 + INPUT_RELEASE_TIMEOUT_MS + 1;

// ⚠️ Точки для перевірок виводяться з роздільності, а не пишуться числами.
// Координата обрізається по екрану, тож прибите число на кшталт (60, 70)
// перетворює тест на пастку: на 212×64 він падає не тому, що код зламаний, а
// тому, що 70 більше за висоту. Три різні точки, жодна не збігається з іншою
// навіть на найменшому підтримуваному екрані.
constexpr int16_t X1 = static_cast<int16_t>(SCREEN_W / 4);
constexpr int16_t Y1 = static_cast<int16_t>(SCREEN_H / 4);
constexpr int16_t X2 = static_cast<int16_t>(SCREEN_W / 2);
constexpr int16_t Y2 = static_cast<int16_t>(SCREEN_H / 2);
constexpr int16_t X3 = static_cast<int16_t>(SCREEN_W - 1);
constexpr int16_t Y3 = static_cast<int16_t>(SCREEN_H - 1);

static_assert(X1 != X2 && X2 != X3 && Y1 != Y2 && Y2 != Y3,
              "точки перевірок мають бути різними на будь-якому екрані");

struct Touch {
  int16_t x = -1;
  int16_t y = -1;
  bool pressed = false;
  bool taken = false;
};

Touch pop(InputState& input, uint32_t nowMs)
{
  Touch t;
  t.taken = input.popTouch(t.x, t.y, t.pressed, nowMs);
  return t;
}

// Вантаж 0x87 INPUT_STATE, складений байт у байт за docs/03-protocol.md:
// 4 маски клавіш LE, 4 маски тримерів LE, прапорці (біт0 = палець унизу),
// x LE, y LE. Складаємо руками навмисно — тест має перевіряти розкладку на
// дроті, а не повторювати за розбором.
struct StatePayload {
  uint8_t bytes[INPUT_STATE_PAYLOAD_SIZE] = {};
};

StatePayload makeState(uint32_t keys, uint32_t trims, bool down, int16_t x,
                       int16_t y)
{
  StatePayload p;
  for (int i = 0; i < 4; ++i) {
    p.bytes[i] = static_cast<uint8_t>(keys >> (8 * i));
    p.bytes[4 + i] = static_cast<uint8_t>(trims >> (8 * i));
  }
  p.bytes[8] = down ? 0x01 : 0x00;
  p.bytes[9] = static_cast<uint8_t>(static_cast<uint16_t>(x));
  p.bytes[10] = static_cast<uint8_t>(static_cast<uint16_t>(x) >> 8);
  p.bytes[11] = static_cast<uint8_t>(static_cast<uint16_t>(y));
  p.bytes[12] = static_cast<uint8_t>(static_cast<uint16_t>(y) >> 8);
  return p;
}

// Повний стан вводу тим самим шляхом, яким його везе транспорт: через розбір
// пакета, а не прямим викликом методу. Так тести перевіряють і розкладку
// байтів, і правило «позначку життя ставить лише розбір».
bool sendState(InputState& input, uint32_t keys, uint32_t trims, bool down,
               int16_t x, int16_t y, uint32_t nowMs)
{
  const StatePayload p = makeState(keys, trims, down, x, y);
  return applyInputPacket(input, PKT_INPUT_STATE, p.bytes, sizeof(p.bytes),
                          nowMs);
}

}  // namespace

// --- Порожній стан ----------------------------------------------------------

TEST(InputStartsSilent)
{
  InputState input;

  CHECK(input.linkLost(T0));
  CHECK_EQ(input.takeKeys(T0), 0u);
  CHECK_EQ(input.takeTrims(T0), 0u);
  CHECK_EQ(input.encoderOffset(), 0);
  CHECK(!pop(input, T0).taken);  // сенсор лишається фізичному драйверу
}

// --- Клавіші ----------------------------------------------------------------

TEST(InputKeyPressAppearsInMask)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_A);
  // Клавішу тримають — вона лишається натиснутою й на наступних читаннях.
  CHECK_EQ(input.takeKeys(T0 + 10), 1u << KEY_A);
  CHECK_EQ(input.takeKeys(T0 + 20), 1u << KEY_A);
}

TEST(InputKeyReleaseClearsMask)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_A);

  input.onKey(KEY_A, false, T0 + 30);
  CHECK_EQ(input.takeKeys(T0 + 30), 0u);
}

TEST(InputKeysAreIndependent)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onKey(KEY_B, true, T0);
  CHECK_EQ(input.takeKeys(T0), (1u << KEY_A) | (1u << KEY_B));

  input.onKey(KEY_A, false, T0);
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_B);
}

// Натискання, що почалось і скінчилось між двома читаннями, не має зникнути:
// keysPollingCycle() ходить сюди раз на 10 мс, і без засувки короткий тик
// клієнта пропав би безслідно.
TEST(InputShortPressSurvivesBetweenPolls)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onKey(KEY_A, false, T0 + 1);

  CHECK_EQ(input.takeKeys(T0 + 10), 1u << KEY_A);  // рівно один раз
  CHECK_EQ(input.takeKeys(T0 + 20), 0u);
}

TEST(InputIgnoresKeyOutsideMask)
{
  InputState input;

  input.onKey(INPUT_MAX_BITS, true, T0);      // рівно за межею
  input.onKey(200, true, T0);                 // і далеко за нею
  CHECK_EQ(input.takeKeys(T0), 0u);
}

// --- Тримери ----------------------------------------------------------------

TEST(InputTrimsUseDirectionBits)
{
  InputState input;

  input.onTrim(3, true, T0);
  CHECK_EQ(input.takeTrims(T0), 1u << 3);
  CHECK_EQ(input.takeKeys(T0), 0u);  // тримери й клавіші не змішуються

  input.onTrim(3, false, T0 + 5);
  CHECK_EQ(input.takeTrims(T0 + 5), 0u);
}

// --- ⚠️ Безпека: тиша в каналі відпускає все --------------------------------

TEST(InputSilenceReleasesKeys)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_A);

  // Ще в межах тайм-ауту — клавіша натиснута.
  CHECK_EQ(input.takeKeys(T0 + INPUT_RELEASE_TIMEOUT_MS), 1u << KEY_A);

  // Тайм-аут минув. Жодного пакета про відпускання не було й не буде.
  CHECK_EQ(input.takeKeys(T_LOST), 0u);
  CHECK(input.linkLost(T_LOST));

  // І вона не «повертається», коли час іде далі.
  CHECK_EQ(input.takeKeys(T_LOST + 5000), 0u);
}

TEST(InputSilenceReleasesTrims)
{
  InputState input;

  input.onTrim(1, true, T0);
  CHECK_EQ(input.takeTrims(T0), 1u << 1);
  CHECK_EQ(input.takeTrims(T_LOST), 0u);
}

TEST(InputSilenceReleasesTouch)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);

  const Touch down = pop(input, T0);
  CHECK(down.taken);
  CHECK(down.pressed);

  // Клієнт зник посеред дотику: наступне читання після тайм-ауту має віддати
  // відпускання — рівно одне — і повернути сенсор залізу.
  const Touch up = pop(input, T_LOST);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK_EQ(up.x, X1);
  CHECK_EQ(up.y, Y1);

  CHECK(!pop(input, T_LOST + 1).taken);
}

// Після тайм-ауту читач дотик відпустив, а писар про це не знав би, і
// наступний DOWN від того самого клієнта проковтнувся б як «не перехід» —
// сенсор лишився б мертвим до першого UP. На UART розриву не існує взагалі,
// тож саме цей шлях там основний.
TEST(InputTouchWorksAgainAfterSilence)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  const Touch released = pop(input, T_LOST);  // тайм-аут відпустив
  CHECK(released.taken);
  CHECK(!released.pressed);

  // Клієнт живий і торкається знову.
  input.onTouch(TOUCH_EVENT_DOWN, X2, Y2, T_LOST + 10);
  const Touch again = pop(input, T_LOST + 20);
  CHECK(again.taken);
  CHECK(again.pressed);
  CHECK_EQ(again.x, X2);
  CHECK_EQ(again.y, Y2);

  // І рух після цього теж доходить.
  input.onTouch(TOUCH_EVENT_MOVE, X3, Y3, T_LOST + 30);
  const Touch moved = pop(input, T_LOST + 40);
  CHECK(moved.taken);
  CHECK(moved.pressed);
  CHECK_EQ(moved.x, X3);
  CHECK_EQ(moved.y, Y3);
}

// Той самий глухий кут, але входом через чесний пакет TOUCH UP, а не через
// повтор стану. Різниця лише в наслідках: через UP залипання вилікує наступний
// дотик людини, а через повтор стану — ніколи. Другий вхід у ту саму яму
// лишати не можна, тим паче в задачі саме про залипання.
TEST(InputTouchUpReleasesWhenProducerSeesTimeoutFirst)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  // Читач не опитував; тиша перевалила за тайм-аут; приходить чесний UP.
  input.onTouch(TOUCH_EVENT_UP, X1, Y1, T_LOST);

  const Touch up = pop(input, T_LOST + 1);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK(!pop(input, T_LOST + 2).taken);
}

// Перехід має читатися цілим. Кожне поєднання координат і стану має пережити
// пакування в одне слово — інакше читач забрав би «натиснуто» від одного
// переходу з точкою від іншого.
TEST(InputTouchTransitionSurvivesPacking)
{
  InputState input;

  const int16_t xs[] = {0, 1, X2, X3};
  const int16_t ys[] = {0, 1, Y2, Y3};

  for (int16_t x : xs) {
    for (int16_t y : ys) {
      input.onTouch(TOUCH_EVENT_DOWN, x, y, T0);
      const Touch down = pop(input, T0);
      CHECK(down.taken);
      CHECK(down.pressed);
      CHECK_EQ(down.x, x);
      CHECK_EQ(down.y, y);

      input.onTouch(TOUCH_EVENT_UP, x, y, T0);
      const Touch up = pop(input, T0);
      CHECK(up.taken);
      CHECK(!up.pressed);
      CHECK_EQ(up.x, x);
      CHECK_EQ(up.y, y);
    }
  }
}

// ⚠️ Годинник тайм-ауту — це INPUT_STATE, і тільки він (разом з іншими
// пакетами вводу). Поки стан повторюється, утримувана клавіша лишається
// натиснутою скільки завгодно довго.
TEST(InputStateKeepsKeyHeld)
{
  InputState input;

  input.onKey(KEY_A, true, T0);

  uint32_t now = T0;
  for (int i = 0; i < 20; ++i) {
    now += INPUT_STATE_PERIOD_MS;
    CHECK(sendState(input, 1u << KEY_A, 0, false, 0, 0, now));
    CHECK_EQ(input.takeKeys(now), 1u << KEY_A);
  }

  // А щойно пакети скінчились — відпускається.
  CHECK_EQ(input.takeKeys(now + INPUT_RELEASE_TIMEOUT_MS + 1), 0u);
}

// ⚠️ Найважливіша зміна поведінки задачі 0008: PING вводу не несе, тому
// тайм-аут не відсуває. Причина — міст ESP32 на етапі 2: він власний
// посередник і може слати PING після того, як телефон від'єднався. При
// старому правилі клавіша, утримувана в мить розриву Wi-Fi, лишилась би
// натиснутою назавжди, і людина цього не побачила б.
TEST(InputPingDoesNotHoldInput)
{
  InputState input;

  input.onKey(KEY_A, true, T0);

  // Клієнт живий і балакучий, але шле саме те, що вводу не несе.
  for (uint32_t t = T0 + 100; t <= T_LOST; t += 100) {
    CHECK(!applyInputPacket(input, PKT_PING, nullptr, 0, t));
    CHECK(!applyInputPacket(input, PKT_REFRESH, nullptr, 0, t));
    CHECK(!applyInputPacket(input, 0xEE, nullptr, 0, t));  // і невідомий тип
  }

  // Рівно на межі — ще натиснута, за нею — відпущена, попри весь той трафік.
  CHECK_EQ(input.takeKeys(T0 + INPUT_RELEASE_TIMEOUT_MS), 1u << KEY_A);
  CHECK(input.linkLost(T_LOST));
  CHECK_EQ(input.takeKeys(T_LOST), 0u);
}

// --- ⚠️ Безпека: розрив зв'язку відпускає все негайно ------------------------

TEST(InputDisconnectReleasesKeysAndTrims)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onKey(KEY_B, true, T0);
  input.onTrim(2, true, T0);

  input.onDisconnect();

  // Не за тайм-аутом, а тієї ж миті.
  CHECK_EQ(input.takeKeys(T0), 0u);
  CHECK_EQ(input.takeTrims(T0), 0u);
  CHECK(input.linkLost(T0));
}

TEST(InputDisconnectReleasesTouch)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  input.onDisconnect();

  const Touch up = pop(input, T0);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK(!pop(input, T0).taken);
}

// Навіть якщо читач жодного разу не встиг побачити натискання до розриву,
// після нього не має лишитись нічого натиснутого.
TEST(InputDisconnectBeatsLatch)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onDisconnect();

  CHECK_EQ(input.takeKeys(T0), 0u);
}

// Новий клієнт не відповідає за те, що встиг натиснути попередній. Найгірший
// випадок — коли новий устиг під'єднатись раніше, ніж LVGL опитав сенсор: тоді
// «зв'язку немає» читач так і не побачить, і відпустити дотик має щось інше.
TEST(InputReconnectReleasesTouchOfPreviousClient)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);  // читач побачив натиск

  input.onDisconnect();
  // Новий клієнт привітався порожнім станом — саме так це виглядає на дроті
  // після зміни правила «тайм-аут відсуває лише ввід».
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 100));
  CHECK(!input.linkLost(T0 + 100));
  CHECK_EQ(input.takeKeys(T0 + 100), 0u);

  const Touch up = pop(input, T0 + 100);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK(!pop(input, T0 + 100).taken);
}

// Той самий розрив, але читач натиску так і не побачив: тоді відпускати нема
// чого, і головне — натиск попереднього клієнта не має програтись заднім
// числом уже за нового.
TEST(InputReconnectDropsUnseenTouch)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  input.onDisconnect();
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 100));

  CHECK(!pop(input, T0 + 100).taken);

  // А свій власний дотик новий клієнт робить як завжди.
  input.onTouch(TOUCH_EVENT_DOWN, X2, Y2, T0 + 110);
  const Touch down = pop(input, T0 + 120);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X2);
  CHECK_EQ(down.y, Y2);
}

// --- Сенсор -----------------------------------------------------------------

// LVGL опитує сенсор періодично; поки палець унизу, кожне опитування має
// бачити натиск. Інакше довге натискання розсипалось би на окремі кліки.
TEST(InputTouchHoldsPressBetweenPolls)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);

  for (int i = 0; i < 5; ++i) {
    const Touch t = pop(input, T0 + i * 30);
    CHECK(t.taken);
    CHECK(t.pressed);
    CHECK_EQ(t.x, X1);
    CHECK_EQ(t.y, Y1);
  }
}

// Швидкий тик: натиск і відпускання прийшли між двома опитуваннями LVGL.
// Обидва переходи мають дійти, інакше клік просто не станеться.
TEST(InputTouchTapIsNotLost)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  input.onTouch(TOUCH_EVENT_UP, X1, Y1, T0 + 1);

  const Touch down = pop(input, T0 + 30);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X1);

  const Touch up = pop(input, T0 + 60);
  CHECK(up.taken);
  CHECK(!up.pressed);

  CHECK(!pop(input, T0 + 90).taken);
}

TEST(InputTouchMoveFollowsFinger)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  input.onTouch(TOUCH_EVENT_MOVE, X2, Y2, T0 + 10);
  const Touch moved = pop(input, T0 + 20);
  CHECK(moved.taken);
  CHECK(moved.pressed);
  CHECK_EQ(moved.x, X2);
  CHECK_EQ(moved.y, Y2);
}

// Рух без натиску переходом не є: інакше кожне ворушіння мишею над картинкою
// заступало б фізичний сенсор.
TEST(InputTouchMoveWithoutPressDoesNotClaimDriver)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_MOVE, X2, Y2, T0);
  CHECK(!pop(input, T0).taken);
}

TEST(InputTouchIdleLeavesHardwareAlone)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  input.onTouch(TOUCH_EVENT_UP, X1, Y1, T0);

  CHECK(pop(input, T0).taken);   // натиск
  CHECK(pop(input, T0).taken);   // відпускання
  CHECK(!pop(input, T0).taken);  // далі сенсор фізичний
  CHECK(!pop(input, T0).taken);
}

TEST(InputTouchClampsToScreen)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, -50, 30000, T0);
  const Touch t = pop(input, T0);
  CHECK(t.taken);
  CHECK_EQ(t.x, 0);
  CHECK_EQ(t.y, SCREEN_H - 1);
}

TEST(InputTouchIgnoresRepeatedDown)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  input.onTouch(TOUCH_EVENT_DOWN, X2, Y2, T0);
  input.onTouch(TOUCH_EVENT_DOWN, X3, Y3, T0);

  CHECK(pop(input, T0).pressed);
  // Другий і третій DOWN — не переходи; лишається один натиск, який тримається.
  const Touch held = pop(input, T0);
  CHECK(held.taken);
  CHECK(held.pressed);
  CHECK_EQ(held.x, X3);  // точка при цьому свіжа
}

// Читач відстав більше, ніж уміщає історія переходів. Втратити проміжні —
// прикро, але припустимо; лишитись «натиснутим» після відпускання — ні.
TEST(InputTouchHistoryOverflowEndsReleased)
{
  InputState input;

  for (uint32_t i = 0; i < TOUCH_HISTORY * 3; ++i) {
    input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
    input.onTouch(TOUCH_EVENT_UP, X1, Y1, T0);
  }

  // Скільки б переходів не програлось, останній із них — відпускання.
  Touch last;
  int guard = 0;
  while (guard++ < 100) {
    const Touch t = pop(input, T0);
    if (!t.taken) {
      break;
    }
    last = t;
  }
  CHECK(last.taken);
  CHECK(!last.pressed);
  CHECK(!pop(input, T0).taken);
}

// --- ⚠️ Повний стан вводу (0x87 INPUT_STATE) --------------------------------
//
// Пакет існує заради одного випадку: загубилось саме «відпущено», а зв'язок
// живий. Тайм-аут такого не ловить — ловить рівень, який приходить раз на
// 250 мс і заміщує собою попередній.

// Найпростіший випадок: читач натискання вже забрав, засувки немає, лишився
// сам рівень. Повтор стану з порожньою маскою його знімає.
TEST(InputStateHealsStuckKey)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_A);

  // «Відпущено» не прийшло — загубилось у каналі. Прийшов черговий стан.
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + INPUT_STATE_PERIOD_MS));
  CHECK_EQ(input.takeKeys(T0 + INPUT_STATE_PERIOD_MS), 0u);
}

// ⚠️ Найтонше місце всієї задачі. Клавіші защіпаються тому, що читач ходить
// раз на 10 мс і короткий тик інакше пропав би. Повтор стану «нічого не
// утримується», що прийшов між натисканням і читанням, не сміє зняти засувку —
// інакше пакет, покликаний лікувати залипання, почав би їсти натискання.
TEST(InputStateDoesNotEatUnreadKeyLatch)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  // Читач сюди ще не дійшов — і саме зараз приходить порожній стан.
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 5));

  CHECK_EQ(input.takeKeys(T0 + 10), 1u << KEY_A);  // рівно один раз
  CHECK_EQ(input.takeKeys(T0 + 20), 0u);           // і більше ніколи
}

TEST(InputStateDoesNotEatUnreadTrimLatch)
{
  InputState input;

  input.onTrim(3, true, T0);
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 5));

  CHECK_EQ(input.takeTrims(T0 + 10), 1u << 3);
  CHECK_EQ(input.takeTrims(T0 + 20), 0u);
}

// Зворотний бік: загубилось «натиснуто». Рівень підіймає біт, якого не було, і
// тримає його, поки стан повторюється.
TEST(InputStateRaisesMissedPress)
{
  InputState input;

  CHECK(sendState(input, 1u << KEY_B, 1u << 1, false, 0, 0, T0));
  CHECK_EQ(input.takeKeys(T0), 1u << KEY_B);
  CHECK_EQ(input.takeTrims(T0), 1u << 1);

  const uint32_t next = T0 + INPUT_STATE_PERIOD_MS;
  CHECK(sendState(input, 1u << KEY_B, 1u << 1, false, 0, 0, next));
  CHECK_EQ(input.takeKeys(next), 1u << KEY_B);

  const uint32_t last = next + INPUT_STATE_PERIOD_MS;
  CHECK(sendState(input, 0, 0, false, 0, 0, last));
  CHECK_EQ(input.takeKeys(last), 0u);
  CHECK_EQ(input.takeTrims(last), 0u);
}

// Рівень ідемпотентний — на цьому тримається відсутність номера
// послідовності в пакеті (docs/03-protocol.md, правило 6).
TEST(InputStateIsIdempotent)
{
  InputState input;

  CHECK(sendState(input, 1u << KEY_A, 0, true, X1, Y1, T0));
  CHECK(sendState(input, 1u << KEY_A, 0, true, X1, Y1, T0 + 1));
  CHECK(sendState(input, 1u << KEY_A, 0, true, X1, Y1, T0 + 2));

  // Сенсор: рівно один натиск на три однакові пакети.
  const Touch down = pop(input, T0 + 3);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X1);
  CHECK_EQ(down.y, Y1);

  // Клавіші: жодної засувки не накопичилось — один порожній стан прибирає все.
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 4));
  CHECK_EQ(input.takeKeys(T0 + 5), 0u);

  // І переходів у черзі рівно два: натиск і відпускання.
  const Touch up = pop(input, T0 + 6);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK(!pop(input, T0 + 7).taken);
}

// Залиплий дотик небезпечніший за клавішу: він ще й заступає фізичний сенсор.
TEST(InputStateHealsStuckTouch)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  // «Відпущено» загубилось; прийшов стан «пальця немає».
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + INPUT_STATE_PERIOD_MS));

  const Touch up = pop(input, T0 + INPUT_STATE_PERIOD_MS);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK_EQ(up.x, X1);
  CHECK_EQ(up.y, Y1);

  // І сенсор повернувся залізу.
  CHECK(!pop(input, T0 + INPUT_STATE_PERIOD_MS + 1).taken);
}

// ⚠️ Дотик застосовується тільки переходом, ніколи присвоєнням. Присвоєння
// повз чергу стерло б непрочитаний натиск — і LVGL лишився б натиснутим
// назавжди, бо відпускання прийшло б у порожнечу.
TEST(InputStateDoesNotSwallowUnreadTouchDown)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X2, Y2, T0);
  // Читач сюди ще не дійшов.
  CHECK(sendState(input, 0, 0, false, X3, Y3, T0 + 10));

  const Touch down = pop(input, T0 + 20);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X2);
  CHECK_EQ(down.y, Y2);

  const Touch up = pop(input, T0 + 30);
  CHECK(up.taken);
  CHECK(!up.pressed);

  CHECK(!pop(input, T0 + 40).taken);
}

// ⚠️ Найгірший порядок подій з усіх можливих, і водночас найімовірніший при
// обриві трохи довшому за тайм-аут.
//
// Тайм-аут помічають двоє незалежно: писар (тут) і читач (у popTouch). Читач
// ходить сюди раз на опитування LVGL, і між миттю, коли тайм-аут настав, і
// приходом наступного пакета він цілком може не встигнути. Тоді тишу першим
// бачить писар — а вже наступним рядком сам її і стирає, оновивши позначку
// життя.
//
// Якщо писар при цьому просто скине свій прапорець «палець унизу», вийде
// глухий кут: переходу в кільці немає (скидати вже нічого), тиші теж немає
// (позначка свіжа), і читач віддаватиме «натиснуто» **вічно**, заступаючи
// собою фізичний сенсор. Повтори стану «пальця немає» кожні 250 мс нічого не
// змінять — писар щоразу проходитиме тією самою порожньою гілкою.
TEST(InputStateReleasesTouchWhenProducerSeesTimeoutFirst)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);  // читач тримає дотик

  // Тиша перевалила за тайм-аут, але читач сюди не заходив. Перший, хто її
  // помічає, — писар, і робить це на пакеті клієнта.
  CHECK(sendState(input, 0, 0, false, 0, 0, T_LOST));

  const Touch up = pop(input, T_LOST + 1);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK(!pop(input, T_LOST + 2).taken);  // і сенсор повернувся залізу
}

// Той самий глухий кут має не лише розчищатись, а й не заважати далі: клієнт
// живий, і його наступний дотик мусить дійти як звичайний.
TEST(InputStateTouchWorksAfterProducerTimeout)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  CHECK(sendState(input, 0, 0, false, 0, 0, T_LOST));
  const Touch up = pop(input, T_LOST + 1);
  CHECK(up.taken);
  CHECK(!up.pressed);

  // Клієнт торкається знову — рівнем, бо «натиснуто» могло й загубитись.
  CHECK(sendState(input, 0, 0, true, X2, Y2, T_LOST + INPUT_STATE_PERIOD_MS));
  const Touch down = pop(input, T_LOST + INPUT_STATE_PERIOD_MS + 1);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X2);
  CHECK_EQ(down.y, Y2);
}

// Загублене «натиснуто» рівень теж лікує — синтетичним натиском.
TEST(InputStateSynthesisesTouchDown)
{
  InputState input;

  CHECK(sendState(input, 0, 0, true, X2, Y2, T0));

  const Touch down = pop(input, T0);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X2);
  CHECK_EQ(down.y, Y2);
}

// Координати при біт0=0 не читаються взагалі: синтетичне відпускання бере
// останню відому точку. Інакше палець «стрибнув» би перед відпусканням, і
// натискання зарахувалось би не туди, куди його зробила людина.
TEST(InputStateReleaseUsesLastKnownPoint)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  // Координати в пакеті завідомо інші — і мають бути проігноровані.
  CHECK(sendState(input, 0, 0, false, X3, Y3, T0 + 10));

  const Touch up = pop(input, T0 + 20);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK_EQ(up.x, X1);
  CHECK_EQ(up.y, Y1);
}

// Рівень «унизу» діє ще й як MOVE: так безкоштовно лікується втрачений рух.
TEST(InputStateLevelActsAsMove)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(pop(input, T0).pressed);

  CHECK(sendState(input, 0, 0, true, X2, Y2, T0 + 10));

  const Touch moved = pop(input, T0 + 20);
  CHECK(moved.taken);
  CHECK(moved.pressed);
  CHECK_EQ(moved.x, X2);
  CHECK_EQ(moved.y, Y2);
}

// ...але переходом при цьому не стає. Перевіряється відпусканням: зайвий
// натиск у черзі виліз би саме тут — після відпускання лишилось би ще одне
// подія, і дотик воскрес би.
TEST(InputStateLevelAddsNoExtraTransition)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);
  CHECK(sendState(input, 0, 0, true, X2, Y2, T0 + 10));   // рівень «унизу»
  CHECK(sendState(input, 0, 0, false, X3, Y3, T0 + 20));  // і «пальця немає»

  const Touch down = pop(input, T0 + 30);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X1);
  CHECK_EQ(down.y, Y1);

  // Відпускання бере точку, оновлену рівнем, — не з першого натиску й не з
  // координат самого пакета відпускання.
  const Touch up = pop(input, T0 + 40);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK_EQ(up.x, X2);
  CHECK_EQ(up.y, Y2);

  CHECK(!pop(input, T0 + 50).taken);
}

// Енкодер накопичувальний, тому в пакеті його немає й чіпати його пакет не
// має права: заміщення накопичувача рівнем дало б фантомний оберт назад.
TEST(InputStateLeavesEncoderAlone)
{
  InputState input;

  input.onEncoder(3, T0);
  CHECK_EQ(input.encoderOffset(), 3);
  CHECK_EQ(input.takeEncoderDtMs(), ENCODER_DT_IDLE_MS);

  CHECK(sendState(input, 0xFFFFFFFFu, 0xFFFFFFFFu, true, X1, Y1, T0 + 10));
  CHECK(sendState(input, 0, 0, false, 0, 0, T0 + 20));

  CHECK_EQ(input.encoderOffset(), 3);
  CHECK_EQ(input.takeEncoderDtMs(), 0u);  // і часу енкодера теж не додає
}

// ⚠️ Обрізаний пакет не застосовується **і не відсуває тайм-аут**. Помилятися
// треба в бік відпускання: інакше побитий вантаж міг би нескінченно тримати
// клавішу натиснутою, нічого при цьому не означаючи.
TEST(InputStateShortPayloadIsIgnored)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  const StatePayload full = makeState(0, 0, false, 0, 0);

  for (size_t len = 0; len < INPUT_STATE_PAYLOAD_SIZE; ++len) {
    CHECK(!applyInputPacket(input, PKT_INPUT_STATE, full.bytes, len, T0 + 100));
  }

  // Маску не стерто...
  CHECK_EQ(input.takeKeys(T0 + 100), 1u << KEY_A);
  // ...і час не відсунуто: тайм-аут рахується від T0, а не від T0 + 100.
  CHECK(input.linkLost(T_LOST));
  CHECK_EQ(input.takeKeys(T_LOST), 0u);
}

// Довший пакет — від новішого клієнта. Застосовуються перші 13 байтів, хвіст
// ігнорується: протокол дозволяє додавати поля тільки в кінець.
TEST(InputStateLongPayloadAppliesHead)
{
  InputState input;

  uint8_t buf[INPUT_STATE_PAYLOAD_SIZE + 4];
  const StatePayload head = makeState(1u << KEY_B, 0, true, X1, Y1);
  for (size_t i = 0; i < INPUT_STATE_PAYLOAD_SIZE; ++i) {
    buf[i] = head.bytes[i];
  }
  for (size_t i = INPUT_STATE_PAYLOAD_SIZE; i < sizeof(buf); ++i) {
    buf[i] = 0xAA;  // поля, яких ми ще не знаємо
  }

  CHECK(applyInputPacket(input, PKT_INPUT_STATE, buf, sizeof(buf), T0));

  CHECK_EQ(input.takeKeys(T0), 1u << KEY_B);
  const Touch down = pop(input, T0);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, X1);
  CHECK_EQ(down.y, Y1);
}

// Координати рівня обрізаються по екрану тим самим правилом, що й у TOUCH.
TEST(InputStateClampsCoordinates)
{
  InputState input;

  CHECK(sendState(input, 0, 0, true, static_cast<int16_t>(SCREEN_W + 500),
                  static_cast<int16_t>(SCREEN_H + 500), T0));

  const Touch down = pop(input, T0);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, SCREEN_W - 1);
  CHECK_EQ(down.y, SCREEN_H - 1);
}

// --- Енкодер ----------------------------------------------------------------

// Назовні йде накопичене положення: гачок додає його до фізичного, а різницю
// рахує EdgeTX. Якби ми віддавали приріст і обнуляли його, наступне читання
// дало б фантомний оберт назад.
TEST(InputEncoderAccumulates)
{
  InputState input;

  input.onEncoder(1, T0);
  CHECK_EQ(input.encoderOffset(), 1);

  input.onEncoder(1, T0);
  CHECK_EQ(input.encoderOffset(), 2);

  // Читання нічого не споживає.
  CHECK_EQ(input.encoderOffset(), 2);

  input.onEncoder(-3, T0);
  CHECK_EQ(input.encoderOffset(), -1);
}

// Обрив не має рухати енкодер: обнулення накопичувача дало б рівно той
// фантомний оберт назад, від якого ми й пішли на накопичувач.
TEST(InputEncoderSurvivesDisconnect)
{
  InputState input;

  input.onEncoder(5, T0);
  input.onDisconnect();
  CHECK_EQ(input.encoderOffset(), 5);

  CHECK_EQ(input.takeKeys(T_LOST), 0u);
  CHECK_EQ(input.encoderOffset(), 5);
}

// Час між клацаннями — те, з чого EdgeTX рахує прискорення ручки. Без нього
// dt виходить нульовим, EdgeTX вважає, що крутять нескінченно швидко, і крок
// із другого клацання стрибає на кілька десятків.
TEST(InputEncoderReportsTimeBetweenClicks)
{
  InputState input;

  CHECK_EQ(input.takeEncoderDtMs(), 0u);  // нічого не крутили — нічого й немає

  // Перше клацання розганяти нема з чого: віддаємо «пауза», тобто нуль
  // прискорення.
  input.onEncoder(1, T0);
  CHECK_EQ(input.takeEncoderDtMs(), ENCODER_DT_IDLE_MS);

  // Далі — справжні проміжки, накопичені між читаннями EdgeTX.
  input.onEncoder(1, T0 + 20);
  input.onEncoder(1, T0 + 30);
  CHECK_EQ(input.takeEncoderDtMs(), 30u);

  // Читання споживає: віддати той самий проміжок двічі означало б занизити
  // прискорення.
  CHECK_EQ(input.takeEncoderDtMs(), 0u);

  // Довга пауза — і наступне клацання знову як перше.
  input.onEncoder(1, T0 + 10000);
  CHECK_EQ(input.takeEncoderDtMs(), ENCODER_DT_IDLE_MS);
}

TEST(InputEncoderZeroStepMovesNothing)
{
  InputState input;

  input.onEncoder(0, T0);
  CHECK_EQ(input.encoderOffset(), 0);
  CHECK_EQ(input.takeEncoderDtMs(), 0u);
}

// ⚠️ Нульове клацання нічого не рухає, але тайм-аут відсуває: це законний
// спосіб сказати «я живий і шлю ввід». На ньому тримається проба на точкову
// втрату в tools/input_check.py — режим «як було до 0008» шле саме ENC:0
// замість INPUT_STATE, щоб довести, що зв'язок живий за правилами прошивки, а
// рівень при цьому не повторюється. Тест стереже той контракт.
TEST(InputEncoderZeroStepStillProvesClientAlive)
{
  InputState input;

  input.onKey(KEY_A, true, T0);

  uint32_t now = T0;
  for (int i = 0; i < 10; ++i) {
    now += INPUT_STATE_PERIOD_MS;
    const uint8_t zero = 0;
    CHECK(applyInputPacket(input, PKT_ENC, &zero, 1, now));
    CHECK(!input.linkLost(now));
    CHECK_EQ(input.takeKeys(now), 1u << KEY_A);  // клавіша не відпускається
  }

  CHECK_EQ(input.encoderOffset(), 0);  // і ручка при цьому стоїть

  // А замовк — і відпустилось, як завжди.
  CHECK_EQ(input.takeKeys(now + INPUT_RELEASE_TIMEOUT_MS + 1), 0u);
}

// Клацання, яке ще не дійшло до EdgeTX, переживає розрив разом зі своїм часом:
// накопичувач положення теж не обнуляється, тож обидва мають лишитись у парі.
// А відлік «коли було попереднє» скидається — новий клієнт починає з нуля.
TEST(InputEncoderDtSurvivesDisconnect)
{
  InputState input;

  input.onEncoder(1, T0);
  input.onEncoder(1, T0 + 20);
  input.onDisconnect();
  CHECK_EQ(input.takeEncoderDtMs(), ENCODER_DT_IDLE_MS + 20);

  input.onEncoder(1, T0 + 30);
  CHECK_EQ(input.takeEncoderDtMs(), ENCODER_DT_IDLE_MS);
}

// --- Час --------------------------------------------------------------------

// Лічильник мілісекунд EdgeTX переповнюється приблизно раз на 49 днів.
// Віднімання без знака переживає це саме по собі — але тільки якщо ніде не
// стоїть порівняння «більше/менше» на самих позначках часу.
TEST(InputTimeWrapDoesNotRelease)
{
  InputState input;

  const uint32_t beforeWrap = 0xFFFFFF00u;
  input.onKey(KEY_A, true, beforeWrap);

  const uint32_t afterWrap = 0x00000010u;  // 272 мс по той бік нуля
  CHECK(!input.linkLost(afterWrap));
  CHECK_EQ(input.takeKeys(afterWrap), 1u << KEY_A);

  // А тайм-аут через нуль усе одно спрацьовує.
  CHECK(input.linkLost(afterWrap + INPUT_RELEASE_TIMEOUT_MS));
}

// --- Скид -------------------------------------------------------------------

TEST(InputResetClearsEverything)
{
  InputState input;

  input.onKey(KEY_A, true, T0);
  input.onTrim(0, true, T0);
  input.onEncoder(9, T0);
  input.onTouch(TOUCH_EVENT_DOWN, X1, Y1, T0);

  input.reset();

  CHECK(input.linkLost(T0));
  CHECK_EQ(input.takeKeys(T0), 0u);
  CHECK_EQ(input.takeTrims(T0), 0u);
  CHECK_EQ(input.encoderOffset(), 0);
  CHECK_EQ(input.takeEncoderDtMs(), 0u);
  CHECK(!pop(input, T0).taken);
}
