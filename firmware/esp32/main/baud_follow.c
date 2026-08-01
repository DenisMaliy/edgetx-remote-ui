/**
 * @file baud_follow.c
 * @brief Міст іде за пультом при зміні швидкості каналу.
 *
 * Обґрунтування — у baud_follow.h. Тут сама робота.
 */

#include "baud_follow.h"

#include <inttypes.h>

#include "bridge_cfg.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "framing.h"
#include "stats.h"
#include "uart_link.h"

static const char *TAG = "baud";

/**
 * Скільки міст мовчки терпить, не чуючи пульта на не-домашній швидкості,
 * перш ніж піти додому.
 *
 * ⚠️ Число мусить бути **більшим** за вікно повернення пульта, і не на волосину.
 * Пульт у своєму вікні мовчить навмисно (так відкат коштує один запис
 * дільника), тобто тиша тут — очікувана поведінка, а не ознака біди. Підемо
 * додому раніше за нього — розійдемось із ним у протилежні боки й самі
 * створимо ту розбіжність, від якої обидва сторожі й поставлені.
 *
 * Вікно пульта приходить у самому підтвердженні; додаємо до нього секунду
 * запасу на затримку задачі (заміряні 132 мс) із доброю мірою.
 */
#define BAUD_HOME_EXTRA_MS 1000

/** Як часто нагадувати пультові про себе, поки ми не вдома. */
#define BAUD_KEEPALIVE_MS 500

static uint32_t s_current = BRIDGE_BAUDRATE;

/* Заплановане перемикання. 0 — нічого не заплановано.
 *
 * ⚠️ Намір і дія навмисно розведені в часі. Помітити підтвердження може лише
 * обробник пакета, а він виконується **всередині** розбирача; перемкнути там
 * же означало б скинути розбирач посеред його ж роботи — рівно те, від чого
 * застерігає `framing.h`. Тому обробник тільки записує, а робить прохід. */
static uint32_t s_pending;
static int64_t s_switch_at_us;

/* Скільки терпіти тишу на не-домашній швидкості. Приходить із пакета. */
static uint32_t s_home_after_ms = 1500;

static int64_t s_last_heard_us;
static int64_t s_last_keepalive_us;

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

void baud_follow_init(void)
{
    s_current = BRIDGE_BAUDRATE;
    s_pending = 0;
    s_last_heard_us = esp_timer_get_time();
    s_last_keepalive_us = s_last_heard_us;
    g_stats.baud_current = s_current;
}

uint32_t baud_follow_current(void) { return s_current; }

void baud_follow_on_packet(uint8_t type, const uint8_t *payload, size_t len)
{
    /* ⚠️ Будь-який валідний пакет означає «пульт нас чує», і саме цим
     * закривається сторож нижче. Окремого підтвердження не заводимо з тієї
     * самої причини, що й у прошивці: звичайний потік підтверджує сам себе. */
    s_last_heard_us = esp_timer_get_time();

    if (type != RUI_PKT_BAUD || len < RUI_BAUD_PAYLOAD) {
        return;
    }

    const uint8_t verdict = payload[0];
    const uint32_t target = le32(payload + 2);
    const uint16_t switch_delay = le16(payload + 10);
    const uint16_t revert_window = le16(payload + 12);

    g_stats.baud_radio_reverts = le16(payload + 14);

    if (verdict == RUI_BAUD_REVERTED) {
        /* Пульт повернувся сам і щойно сказав, де він. Якщо ми ще не вдома —
         * біжимо слідом негайно, не чекаючи сторожа. */
        if (s_current != BRIDGE_BAUDRATE) {
            s_pending = BRIDGE_BAUDRATE;
            s_switch_at_us = 0; /* без затримки: наздоганяємо */
        }
        return;
    }

    if (verdict != RUI_BAUD_ACCEPTED || target == s_current) {
        /* UNSUPPORTED, BUSY, NOT_APPLICABLE — не сталося нічого, і нам теж
         * робити нічого. Клієнт побачить вердикт сам: пакет іде далі до нього
         * як є. */
        return;
    }

    /* ⚠️ Чекаємо рівно те число, яке назвав пульт, а не власну константу.
     * Так пульт з іншим періодом проходу працюватиме без правки моста —
     * принцип «нуль специфіки конкретного пульта» діє й тут. */
    s_pending = target;
    s_switch_at_us = esp_timer_get_time() + (int64_t)switch_delay * 1000;
    s_home_after_ms = (uint32_t)revert_window + BAUD_HOME_EXTRA_MS;

    ESP_LOGI(TAG, "пульт іде на %" PRIu32 " бод, я слідом через %u мс", target,
             (unsigned)switch_delay);
}

/** Власне перемикання. Порядок той самий, що в прошивці пульта. */
static void switch_to(uint32_t baud, const char *why)
{
    uart_link_set_baudrate(baud);
    s_current = baud;
    s_pending = 0;
    g_stats.baud_current = baud;

    const int64_t now = esp_timer_get_time();
    s_last_heard_us = now;
    s_last_keepalive_us = now;

    /* ⚠️ Озватися треба НЕГАЙНО, а не через період нагадування.
     *
     * Це не оптимізація, а умова, без якої перемикання не працює взагалі.
     * Пульт, перемкнувшись, мовчить і чекає осмисленого пакета рівно вікно
     * повернення — 500 мс. Міст перемикається на 100 мс пізніше; якщо перше
     * слово він скаже ще через 500 мс нагадування, воно спізниться на 95 мс,
     * і пульт піде додому за мить до того, як його почули.
     *
     * Саме так і сталося на першому прогоні 2 625 000: обидва боки перемкнулись
     * бездоганно й обидва повернулись додому, а виглядало це як «швидкість не
     * тримається».
     */
    if (baud != BRIDGE_BAUDRATE) {
        uart_link_send_ping();
    }

    ESP_LOGI(TAG, "швидкість каналу: %" PRIu32 " бод (%s)", baud, why);
}

bool baud_follow_tick(int64_t now_us)
{
    if (s_pending != 0) {
        if (now_us >= s_switch_at_us) {
            const bool home = (s_pending == BRIDGE_BAUDRATE);
            switch_to(s_pending, home ? "пульт відкотився" : "слідом за пультом");
            return true;
        }
        return false;
    }

    if (s_current == BRIDGE_BAUDRATE) {
        return false;
    }

    /* Ми не вдома. Дві повинності, поки це так. */

    /* 1. Нагадувати про себе. Пульт іде додому, не почувши **нічого**, а
     *    телефон може відвалитись будь-коли — тоді потік від клієнта зникне,
     *    і пульт відкотився б, лишивши нас самих на чужій швидкості. */
    if (now_us - s_last_keepalive_us >= (int64_t)BAUD_KEEPALIVE_MS * 1000) {
        uart_link_send_ping();
        s_last_keepalive_us = now_us;
    }

    /* 2. Іти додому, якщо пульта не чути. Це дзеркало його ж сторожа: він
     *    вертається на BRIDGE_BAUDRATE, і ми маємо опинитись там само. */
    if ((now_us - s_last_heard_us) / 1000 >= (int64_t)s_home_after_ms) {
        g_stats.baud_bridge_reverts++;
        ESP_LOGW(TAG, "пульта не чути %" PRIu32 " мс — іду додому", s_home_after_ms);
        switch_to(BRIDGE_BAUDRATE, "пульта не чути");
        return true;
    }

    return false;
}
