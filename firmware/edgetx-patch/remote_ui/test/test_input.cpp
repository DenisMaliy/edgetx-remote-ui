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

  input.onTouch(TOUCH_EVENT_DOWN, 100, 50, T0);

  const Touch down = pop(input, T0);
  CHECK(down.taken);
  CHECK(down.pressed);

  // Клієнт зник посеред дотику: наступне читання після тайм-ауту має віддати
  // відпускання — рівно одне — і повернути сенсор залізу.
  const Touch up = pop(input, T_LOST);
  CHECK(up.taken);
  CHECK(!up.pressed);
  CHECK_EQ(up.x, 100);
  CHECK_EQ(up.y, 50);

  CHECK(!pop(input, T_LOST + 1).taken);
}

// Після тайм-ауту читач дотик відпустив, а писар про це не знав би, і
// наступний DOWN від того самого клієнта проковтнувся б як «не перехід» —
// сенсор лишився б мертвим до першого UP. На UART розриву не існує взагалі,
// тож саме цей шлях там основний.
TEST(InputTouchWorksAgainAfterSilence)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, 10, 10, T0);
  CHECK(pop(input, T0).pressed);

  const Touch released = pop(input, T_LOST);  // тайм-аут відпустив
  CHECK(released.taken);
  CHECK(!released.pressed);

  // Клієнт живий і торкається знову.
  input.onTouch(TOUCH_EVENT_DOWN, 70, 80, T_LOST + 10);
  const Touch again = pop(input, T_LOST + 20);
  CHECK(again.taken);
  CHECK(again.pressed);
  CHECK_EQ(again.x, 70);
  CHECK_EQ(again.y, 80);

  // І рух після цього теж доходить.
  input.onTouch(TOUCH_EVENT_MOVE, 71, 81, T_LOST + 30);
  const Touch moved = pop(input, T_LOST + 40);
  CHECK(moved.taken);
  CHECK(moved.pressed);
  CHECK_EQ(moved.x, 71);
}

// Перехід має читатися цілим. Кожне поєднання координат і стану має пережити
// пакування в одне слово — інакше читач забрав би «натиснуто» від одного
// переходу з точкою від іншого.
TEST(InputTouchTransitionSurvivesPacking)
{
  InputState input;

  const int16_t xs[] = {0, 1, 100, static_cast<int16_t>(SCREEN_W - 1)};
  const int16_t ys[] = {0, 1, 100, static_cast<int16_t>(SCREEN_H - 1)};

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

// Будь-який пакет доводить, що клієнт живий, — навіть PING, який вводу не
// змінює. Інакше довге натискання відпускалось би само через секунду.
TEST(InputPingKeepsKeyHeld)
{
  InputState input;

  input.onKey(KEY_A, true, T0);

  uint32_t now = T0;
  for (int i = 0; i < 20; ++i) {
    now += INPUT_RELEASE_TIMEOUT_MS / 2;
    input.onAnyPacket(now);
    CHECK_EQ(input.takeKeys(now), 1u << KEY_A);
  }

  // А щойно пакети скінчились — відпускається.
  CHECK_EQ(input.takeKeys(now + INPUT_RELEASE_TIMEOUT_MS + 1), 0u);
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

  input.onTouch(TOUCH_EVENT_DOWN, 7, 9, T0);
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
  input.onTouch(TOUCH_EVENT_DOWN, 5, 5, T0);
  CHECK(pop(input, T0).pressed);  // читач побачив натиск

  input.onDisconnect();
  input.onAnyPacket(T0 + 100);  // новий клієнт привітався
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

  input.onTouch(TOUCH_EVENT_DOWN, 5, 5, T0);
  input.onDisconnect();
  input.onAnyPacket(T0 + 100);

  CHECK(!pop(input, T0 + 100).taken);

  // А свій власний дотик новий клієнт робить як завжди.
  input.onTouch(TOUCH_EVENT_DOWN, 40, 41, T0 + 110);
  const Touch down = pop(input, T0 + 120);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, 40);
  CHECK_EQ(down.y, 41);
}

// --- Сенсор -----------------------------------------------------------------

// LVGL опитує сенсор періодично; поки палець унизу, кожне опитування має
// бачити натиск. Інакше довге натискання розсипалось би на окремі кліки.
TEST(InputTouchHoldsPressBetweenPolls)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, 10, 20, T0);

  for (int i = 0; i < 5; ++i) {
    const Touch t = pop(input, T0 + i * 30);
    CHECK(t.taken);
    CHECK(t.pressed);
    CHECK_EQ(t.x, 10);
    CHECK_EQ(t.y, 20);
  }
}

// Швидкий тик: натиск і відпускання прийшли між двома опитуваннями LVGL.
// Обидва переходи мають дійти, інакше клік просто не станеться.
TEST(InputTouchTapIsNotLost)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, 33, 44, T0);
  input.onTouch(TOUCH_EVENT_UP, 33, 44, T0 + 1);

  const Touch down = pop(input, T0 + 30);
  CHECK(down.taken);
  CHECK(down.pressed);
  CHECK_EQ(down.x, 33);

  const Touch up = pop(input, T0 + 60);
  CHECK(up.taken);
  CHECK(!up.pressed);

  CHECK(!pop(input, T0 + 90).taken);
}

TEST(InputTouchMoveFollowsFinger)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, 10, 10, T0);
  CHECK(pop(input, T0).pressed);

  input.onTouch(TOUCH_EVENT_MOVE, 60, 70, T0 + 10);
  const Touch moved = pop(input, T0 + 20);
  CHECK(moved.taken);
  CHECK(moved.pressed);
  CHECK_EQ(moved.x, 60);
  CHECK_EQ(moved.y, 70);
}

// Рух без натиску переходом не є: інакше кожне ворушіння мишею над картинкою
// заступало б фізичний сенсор.
TEST(InputTouchMoveWithoutPressDoesNotClaimDriver)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_MOVE, 60, 70, T0);
  CHECK(!pop(input, T0).taken);
}

TEST(InputTouchIdleLeavesHardwareAlone)
{
  InputState input;

  input.onTouch(TOUCH_EVENT_DOWN, 1, 2, T0);
  input.onTouch(TOUCH_EVENT_UP, 1, 2, T0);

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

  input.onTouch(TOUCH_EVENT_DOWN, 5, 5, T0);
  input.onTouch(TOUCH_EVENT_DOWN, 6, 6, T0);
  input.onTouch(TOUCH_EVENT_DOWN, 7, 7, T0);

  CHECK(pop(input, T0).pressed);
  // Другий і третій DOWN — не переходи; лишається один натиск, який тримається.
  const Touch held = pop(input, T0);
  CHECK(held.taken);
  CHECK(held.pressed);
  CHECK_EQ(held.x, 7);  // точка при цьому свіжа
}

// Читач відстав більше, ніж уміщає історія переходів. Втратити проміжні —
// прикро, але припустимо; лишитись «натиснутим» після відпускання — ні.
TEST(InputTouchHistoryOverflowEndsReleased)
{
  InputState input;

  for (uint32_t i = 0; i < TOUCH_HISTORY * 3; ++i) {
    input.onTouch(TOUCH_EVENT_DOWN, 10, 10, T0);
    input.onTouch(TOUCH_EVENT_UP, 10, 10, T0);
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
  input.onTouch(TOUCH_EVENT_DOWN, 3, 4, T0);

  input.reset();

  CHECK(input.linkLost(T0));
  CHECK_EQ(input.takeKeys(T0), 0u);
  CHECK_EQ(input.takeTrims(T0), 0u);
  CHECK_EQ(input.encoderOffset(), 0);
  CHECK(!pop(input, T0).taken);
}
