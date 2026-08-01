/**
 * @file framing.h
 * @brief Кадрування протоколу Remote UI: CRC, збирання кадру, потоковий розбір.
 *
 * ## Навіщо мостові кадрування, якщо він тупий
 *
 * Міст справді не інтерпретує **вміст** пакетів: він не знає ні розміру
 * екрана, ні RLE, ні того, що таке плитка. Але межі пакетів знати мусить, і
 * на це є три окремі причини, кожної з яких вистачило б самої:
 *
 * 1. **Відкидати можна тільки цілими пакетами.** Захлинувся WebSocket —
 *    плитки відкидаються (це вже ухвалено, ADR-0003). Якщо відкидати
 *    довільний шматок байтів, клієнт з'їсть обірваний кадр, спіткнеться на
 *    CRC і за розділом «Точка ресинхронізації» втратить ще до 4096 байтів
 *    разом із валідними кадрами, що там лежали. Пакетна межа робить втрату
 *    рівно тією, якою ми її задумали.
 * 2. **Заміри вимагають розрізняти два місця втрат** — скільки відкидає міст
 *    і скільки пульт. Без типу пакета порахувати плитки нема як.
 * 3. **Бита плитка не варта ефіру.** Пакет із хибним CRC далі не йде: канал
 *    Wi-Fi вужчий за дріт, а клієнт однаково викинув би його.
 *
 * Це дзеркало `remote_ui::Decoder` з прошивки пульта і `Decoder` з
 * `tools/remote_ui_proto.py`. Третій примірник — тому третій, що двом
 * попереднім нема як опинитись на ESP32; порядок ресинхронізації в усіх
 * трьох однаковий і описаний у `docs/03-protocol.md`.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RUI_MARKER0 0xE7
#define RUI_MARKER1 0x7E

#define RUI_MAX_PAYLOAD 4096
#define RUI_FRAME_OVERHEAD 7 /* маркер(2) + тип(1) + довжина(2) + CRC(2) */
#define RUI_FRAME_MAX (RUI_FRAME_OVERHEAD + RUI_MAX_PAYLOAD)

/* Пульт → клієнт */
#define RUI_PKT_HELLO 0x01
#define RUI_PKT_TILE 0x02
#define RUI_PKT_FRAME_END 0x03
#define RUI_PKT_STATE 0x04
#define RUI_PKT_LOG 0x05
#define RUI_PKT_BAUD 0x06

/* Клієнт → пульт */
#define RUI_PKT_KEY 0x81
#define RUI_PKT_ENC 0x82
#define RUI_PKT_TOUCH 0x83
#define RUI_PKT_REFRESH 0x84
#define RUI_PKT_TRIM 0x85
#define RUI_PKT_PING 0x86
#define RUI_PKT_INPUT_STATE 0x87
#define RUI_PKT_BAUD_SET 0x88

/** Вантаж `0x06 BAUD` — 16 Б. Розкладка в `remote_ui/baudrate.h`. */
#define RUI_BAUD_PAYLOAD 16

/** Вердикти пакета `BAUD`. Дзеркало `remote_ui::BaudVerdict`. */
#define RUI_BAUD_ACCEPTED 0
#define RUI_BAUD_UNSUPPORTED 1
#define RUI_BAUD_BUSY 2
#define RUI_BAUD_NOT_APPLICABLE 3
#define RUI_BAUD_REVERTED 4

/** Корисний вантаж `INPUT_STATE` — 13 Б, разом із обгорткою 20 Б. */
#define RUI_INPUT_STATE_PAYLOAD 13
#define RUI_INPUT_STATE_FRAME (RUI_FRAME_OVERHEAD + RUI_INPUT_STATE_PAYLOAD)

/**
 * @brief Чи несе пакет ввід.
 *
 * ⚠️ Рівно той самий перелік, що й у прошивці пульта: `KEY`, `ENC`, `TOUCH`,
 * `TRIM`, `INPUT_STATE`. `PING`, `REFRESH` і невідомі типи вводу не несуть і
 * доказом живого клієнта не є (docs/03-protocol.md, «Тайм-аут відпускання»).
 *
 * Міст мусить дотримуватись цього правила, бо воно записане саме заради
 * нього: міст — власний посередник і цілком може слати `PING` далі після
 * того, як телефон від'єднався.
 */
static inline bool rui_carries_input(uint8_t type)
{
    return type == RUI_PKT_KEY || type == RUI_PKT_ENC || type == RUI_PKT_TOUCH ||
           type == RUI_PKT_TRIM || type == RUI_PKT_INPUT_STATE;
}

/** CRC-16/CCITT-FALSE, один байт. Поліном 0x1021, початок 0xFFFF. */
static inline uint16_t rui_crc16_update(uint16_t crc, uint8_t byte)
{
    crc ^= (uint16_t)byte << 8;
    for (int i = 0; i < 8; ++i) {
        crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
    }
    return crc;
}

/** CRC-16/CCITT-FALSE по буферу. */
uint16_t rui_crc16(const uint8_t *data, size_t len);

/**
 * @brief Зібрати кадр на дроті.
 * @return довжина кадру, або 0 якщо вантаж понад стелю.
 *
 * `out` має вміщати `RUI_FRAME_OVERHEAD + len` байтів.
 */
size_t rui_build(uint8_t *out, uint8_t type, const uint8_t *payload, size_t len);

/**
 * @brief Обнулений `INPUT_STATE` — «відпустити все».
 *
 * Саме цей кадр міст шле пульту, щойно втратив телефон. Не тайм-аут, не
 * очікування: розрив Wi-Fi має відпускати ввід за мілісекунди
 * (docs/03-protocol.md, вимога до моста).
 *
 * Нулі безпечні за побудовою: маски клавіш і тримерів присвоюються, а дотик
 * при біт0=0 дописує `UP` у чергу переходів, беручи останню відому точку —
 * координати в цьому разі пульт не читає взагалі.
 *
 * @param out буфер щонайменше `RUI_INPUT_STATE_FRAME` байтів.
 * @return довжина кадру.
 */
size_t rui_build_input_state_zero(uint8_t *out);

/* ------------------------------------------------------ потоковий розбір --*/

typedef enum {
    RUI_S_SYNC0 = 0,
    RUI_S_SYNC1,
    RUI_S_TYPE,
    RUI_S_LEN0,
    RUI_S_LEN1,
    RUI_S_PAYLOAD,
    RUI_S_CRC0,
    RUI_S_CRC1,
} rui_scan_state_t;

/**
 * @brief Готовий пакет.
 * @param ctx        контекст виклику
 * @param type       тип пакета
 * @param frame      увесь кадр разом із маркером і CRC — саме те, що йде далі
 * @param frame_len  довжина кадру
 */
typedef void (*rui_packet_cb)(void *ctx, uint8_t type, const uint8_t *frame, size_t frame_len);

typedef struct {
    uint8_t frame[RUI_FRAME_MAX];
    size_t pos;   /**< скільки байтів кадру вже зібрано */
    size_t need;  /**< скільки байтів вантажу лишилось дочитати */
    uint16_t crc; /**< накопичувальний CRC по TYPE+LEN+PAYLOAD */
    uint16_t got_crc;
    uint8_t state;

    /* Лічильники — джерело для /api/stats. */
    uint32_t packets;    /**< пакетів прийнято цілими */
    uint32_t crc_errors; /**< відкинуто по CRC */
    uint32_t oversized;  /**< брехлива довжина понад 4096 */
} rui_scanner_t;

void rui_scanner_reset(rui_scanner_t *s);

/**
 * @brief Згодувати розбирачу шматок байтів.
 *
 * `cb` викликається по одному разу на кожен цілий пакет, синхронно.
 */
void rui_scanner_feed(rui_scanner_t *s, const uint8_t *data, size_t len, rui_packet_cb cb,
                      void *ctx);
