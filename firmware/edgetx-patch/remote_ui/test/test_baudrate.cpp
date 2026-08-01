/*
 * Тести перемикання швидкості каналу: перелік і два пакети протоколу.
 *
 * Самого перемикання тут немає — воно живе в транспорті й потребує драйвера.
 * Тут перевіряється те, що можна перевірити без заліза: чи не пролізе в
 * перелік значення, яке зламало б дискретизування, і чи не розповзеться
 * розкладка пакетів між прошивкою й мостом.
 */

#include "baudrate.h"
// ⚠️ Тайм-аут відпускання вводу береться звідси, а не переоголошується числом:
// тест нижче стверджує, що вікно повернення й тайм-аут не заважають одне
// одному, і оголошений локально дублікат зробив би цю перевірку перевіркою
// самої себе — вона пройшла б зі старим числом після зміни справжнього.
#include "input.h"
#include "test_harness.h"

using namespace remote_ui;

// --- Перелік ----------------------------------------------------------------

// ⚠️ Перелік пиняється цілком, а не «кожен його елемент дозволений».
//
// Різниця істотна. Обхід масиву проти `baudAllowed()` ловить зламаний
// лінійний пошук, але **не** ловить зламаного переліку: додане завтра
// 2 500 000 пройшло б і тест, і `static_assert` (воно ж менше за стелю). А
// саме воно й відкинуте рішенням — на пульті це BRR = 17, тобто 2 470 588 і
// похибка −1.176 %, гірша за все інше в переліку, при тому що поруч є точне
// 2 625 000.
//
// Перелік закритий рішенням, а не смаком. Розширення — це нова ADR, а не
// правка масиву, і тест тут стоїть, щоб про це нагадати падінням.
TEST(BaudListIsExactlyTheAgreedOne)
{
  const uint32_t expected[] = {921600, 1000000, 1500000, 2000000, 2625000};

  CHECK_EQ(BAUD_ALLOWED_COUNT, sizeof(expected) / sizeof(expected[0]));
  for (size_t i = 0; i < BAUD_ALLOWED_COUNT; ++i) {
    CHECK_EQ(BAUD_ALLOWED[i], expected[i]);
  }
}

TEST(BaudAllowedList)
{
  // Домашня швидкість мусить бути в переліку, і саме першою: відкат завжди йде
  // на неї, і людина має мати змогу свідомо туди повернутись.
  CHECK_EQ(BAUD_ALLOWED[0], 921600u);

  for (size_t i = 0; i < BAUD_ALLOWED_COUNT; ++i) {
    CHECK(baudAllowed(BAUD_ALLOWED[i]));
  }

  CHECK(!baudAllowed(0));
  CHECK(!baudAllowed(115200));
  CHECK(!baudAllowed(20000000));

  // Відкинуті рішенням значення мають лишатись відкинутими.
  CHECK(!baudAllowed(2500000));
  CHECK(!baudAllowed(2100000));
}

// ⚠️ Найважливіший тест файлу.
//
// Стеля 2 625 000 = 42 МГц / 16 — це не просто «апаратна межа з запасом», а
// умова, за якої весь механізм коштує нуль рядків у чужому коді. За нею
// драйвер перемкнув би дискретизування на ×8, тобто писав би `CR1.OVER8` при
// увімкненому USART, а це вже не один рядок, а нова ADR (tasks/0017).
//
// Тест стоїть тут саме тому, що порушити цю умову легко й привабливо: комусь
// колись захочеться додати «ще трошки швидше».
TEST(BaudCeilingIsHardLimit)
{
  CHECK_EQ(BAUD_CEILING, 2625000u);

  for (size_t i = 0; i < BAUD_ALLOWED_COUNT; ++i) {
    CHECK(BAUD_ALLOWED[i] <= BAUD_CEILING);
  }
}

TEST(BaudListIsSortedAndUnique)
{
  for (size_t i = 1; i < BAUD_ALLOWED_COUNT; ++i) {
    CHECK(BAUD_ALLOWED[i] > BAUD_ALLOWED[i - 1]);
  }
}

// --- Розбір BAUD_SET --------------------------------------------------------

TEST(ParseBaudSetOk)
{
  // 2 000 000 = 0x001E8480, little-endian.
  const uint8_t payload[BAUD_SET_SIZE] = {0x80, 0x84, 0x1E, 0x00, 7};

  uint32_t baud = 0;
  uint8_t nonce = 0;
  CHECK(parseBaudSet(payload, sizeof(payload), baud, nonce));
  CHECK_EQ(baud, 2000000u);
  CHECK_EQ(nonce, 7u);
}

// Обрізаний вантаж відкидається цілком: частково прочитане число швидкості
// гірше за нечитане.
TEST(ParseBaudSetRejectsShort)
{
  const uint8_t payload[4] = {0x80, 0x84, 0x1E, 0x00};

  uint32_t baud = 0xDEADBEEF;
  uint8_t nonce = 0x5A;
  CHECK(!parseBaudSet(payload, sizeof(payload), baud, nonce));

  // Вихідні змінні не чіпані — викликач може покластись на своє попереднє
  // значення, а не отримати половину чужого.
  CHECK_EQ(baud, 0xDEADBEEFu);
  CHECK_EQ(nonce, 0x5Au);

  CHECK(!parseBaudSet(nullptr, BAUD_SET_SIZE, baud, nonce));
  CHECK(!parseBaudSet(payload, 0, baud, nonce));
}

// Довший вантаж не відкидається: невідомий хвіст ігнорується. Це правило
// сумісності протоколу — майбутнє розширення пакета не має ламати цю прошивку.
TEST(ParseBaudSetIgnoresTail)
{
  const uint8_t payload[BAUD_SET_SIZE + 3] = {0x00, 0x09, 0x3D, 0x00, 200,
                                              0xFF, 0xFF, 0xFF};

  uint32_t baud = 0;
  uint8_t nonce = 0;
  CHECK(parseBaudSet(payload, sizeof(payload), baud, nonce));
  CHECK_EQ(baud, 4000000u);  // 0x003D0900 — навмисно поза переліком
  CHECK_EQ(nonce, 200u);

  // Розбір не фільтрує: перевірка переліку — окреме рішення окремого шару.
  CHECK(!baudAllowed(baud));
}

// --- Складання BAUD ---------------------------------------------------------

TEST(BuildBaudReportLayout)
{
  uint8_t out[BAUD_REPORT_SIZE];

  const size_t len =
      buildBaudReport(out, sizeof(out), BAUD_ACCEPTED, 7, 2000000, 921600,
                      BAUD_SWITCH_DELAY_MS, BAUD_REVERT_WINDOW_MS, 3);
  CHECK_EQ(len, BAUD_REPORT_SIZE);

  const uint8_t expected[BAUD_REPORT_SIZE] = {
      0x00, 0x07,                    // вердикт, nonce
      0x80, 0x84, 0x1E, 0x00,        // ціль 2 000 000
      0x00, 0x10, 0x0E, 0x00,        // поточна 921 600
      0x64, 0x00,                    // switch_delay 100
      0xF4, 0x01,                    // revert_window 500
      0x03, 0x00,                    // відкотів 3
  };
  CHECK_BYTES_EQ(out, expected, BAUD_REPORT_SIZE);
}

TEST(BuildBaudReportRejectsSmallBuffer)
{
  uint8_t out[BAUD_REPORT_SIZE - 1];

  CHECK_EQ(buildBaudReport(out, sizeof(out), BAUD_REVERTED, 0, 921600, 921600,
                           BAUD_SWITCH_DELAY_MS, BAUD_REVERT_WINDOW_MS, 1),
           0u);
  CHECK_EQ(buildBaudReport(nullptr, BAUD_REPORT_SIZE, BAUD_REVERTED, 0, 921600,
                           921600, BAUD_SWITCH_DELAY_MS, BAUD_REVERT_WINDOW_MS,
                           1),
           0u);
}

// Лічильник відкотів насичується, а не переповнюється: діагностика, що
// починає рахувати з нуля після 65535 відкотів, збрехала б рівно в тому
// випадку, заради якого її ставили.
TEST(BuildBaudReportSaturatedCounter)
{
  uint8_t out[BAUD_REPORT_SIZE];

  CHECK_EQ(buildBaudReport(out, sizeof(out), BAUD_REVERTED, 0, 921600, 921600,
                           BAUD_SWITCH_DELAY_MS, BAUD_REVERT_WINDOW_MS, 0xFFFF),
           BAUD_REPORT_SIZE);
  CHECK_EQ(out[14], 0xFFu);
  CHECK_EQ(out[15], 0xFFu);
}

// «Не застосовне» передається нулем у полі поточної швидкості — щоб клієнту
// було що показати замість числа, і щоб нуль не можна було сплутати з
// дійсною швидкістю (нуля в переліку немає й бути не може).
TEST(BuildBaudReportNotApplicable)
{
  uint8_t out[BAUD_REPORT_SIZE];

  CHECK_EQ(buildBaudReport(out, sizeof(out), BAUD_NOT_APPLICABLE, 42, 0, 0,
                           BAUD_SWITCH_DELAY_MS, BAUD_REVERT_WINDOW_MS, 0),
           BAUD_REPORT_SIZE);
  CHECK_EQ(out[0], BAUD_NOT_APPLICABLE);
  CHECK_EQ(out[1], 42u);
  for (size_t i = 6; i < 10; ++i) {
    CHECK_EQ(out[i], 0u);
  }
}

// --- Числа перемикання ------------------------------------------------------

// ⚠️ Критерій 3.2 задачі 0017, зафіксований тестом, а не лише коментарем.
//
// Найдовше мовчання вводу при **успішному** перемиканні складається з:
//   250  останній INPUT_STATE -> підтвердження в моста (період клієнта)
//   100  міст чекає switch_delay_ms
//    10  міст переналаштовує порт
//   250  перший INPUT_STATE після перемикання (період клієнта)
//   132  заміряна найгірша затримка задачі моста (задача 0013)
//   ---
//   742  проти тайм-ауту відпускання вводу 1000 мс
//
// Вікно повернення в цю суму не входить узагалі: при успіху воно закривається
// першим валідним кадром, утричі раніше за власну стелю.
TEST(BaudTimingLeavesInputTimeoutAlone)
{
  // Обидва числа — справжні, з input.h. Саме тому тест зламається, якщо
  // тайм-аут відпускання колись зменшать: він і поставлений, щоб зламатись.
  constexpr uint32_t BRIDGE_RECONFIG_MS = 10;
  constexpr uint32_t BRIDGE_WORST_LATENCY_MS = 132;

  const uint32_t worstSilence = INPUT_STATE_PERIOD_MS + BAUD_SWITCH_DELAY_MS +
                                BRIDGE_RECONFIG_MS + INPUT_STATE_PERIOD_MS +
                                BRIDGE_WORST_LATENCY_MS;

  CHECK_EQ(worstSilence, 742u);
  CHECK(worstSilence < INPUT_RELEASE_TIMEOUT_MS);

  // Запас має лишатись помітним, а не «майже вкладається». 258 мс — це той
  // запас, заради якого вікно не роблять коротшим за 500.
  CHECK(INPUT_RELEASE_TIMEOUT_MS - worstSilence >= 250);
}

// Вікно повернення мусить бути помітно довшим за те, що міст витрачає при
// відмові: 100 (його таймер) + 10 (переналаштування) + 132 (затримка задачі)
// ≈ 242 мс. Закоротке вікно дало б найгірший з можливих результатів — відкат
// при мості, який насправді перемкнувся правильно.
TEST(BaudRevertWindowCoversBridgeWorstCase)
{
  constexpr uint32_t BRIDGE_RECONFIG_MS = 10;
  constexpr uint32_t BRIDGE_WORST_LATENCY_MS = 132;

  const uint32_t bridgeWorst =
      BAUD_SWITCH_DELAY_MS + BRIDGE_RECONFIG_MS + BRIDGE_WORST_LATENCY_MS;

  CHECK_EQ(bridgeWorst, 242u);
  CHECK(BAUD_REVERT_WINDOW_MS >= bridgeWorst * 2);

  // І при цьому вікно мусить лишатись коротшим за тайм-аут вводу: інакше
  // невдалий дослід тримав би клавішу натиснутою довше, ніж людина встигне
  // зрозуміти, що сталося.
  CHECK(BAUD_REVERT_WINDOW_MS < 1000);
}
