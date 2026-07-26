/**
 * @file test_framing.c
 * @brief Перевірка кадрування моста на комп'ютері, без ESP32.
 *
 * `framing.c` не залежить від ESP-IDF навмисно — саме щоб його можна було
 * прогнати тут. Це третій примірник кадрування протоколу (перший у прошивці
 * пульта, другий у `tools/remote_ui_proto.py`), і сходитись вони мають на
 * байтах, а не на добрих намірах. Тому головний тест — **еталонний вектор із
 * `docs/03-protocol.md`**: порожній `PING` = `E7 7E 86 00 00 66 45`.
 *
 * Збірка й запуск:
 *   cc -std=c11 -Wall -Wextra -fsanitize=address,undefined \
 *      -I../main test_framing.c ../main/framing.c -o /tmp/t && /tmp/t
 */

#include "framing.h"

#include <stdio.h>
#include <string.h>

static int g_failed;
static int g_checks;

#define CHECK(cond, ...)                                                                           \
    do {                                                                                           \
        g_checks++;                                                                                \
        if (!(cond)) {                                                                             \
            g_failed++;                                                                            \
            printf("  ✗ %s:%d  ", __FILE__, __LINE__);                                             \
            printf(__VA_ARGS__);                                                                   \
            printf("\n");                                                                          \
        }                                                                                          \
    } while (0)

/* --- збирач пакетів для зворотного виклику розбирача --------------------- */

#define CAP 16

typedef struct {
    int count;
    uint8_t type[CAP];
    size_t len[CAP];
    uint8_t frame[CAP][RUI_FRAME_MAX];
} caught_t;

static void on_packet(void *ctx, uint8_t type, const uint8_t *frame, size_t frame_len)
{
    caught_t *c = (caught_t *)ctx;
    if (c->count >= CAP) {
        return;
    }
    c->type[c->count] = type;
    c->len[c->count] = frame_len;
    memcpy(c->frame[c->count], frame, frame_len);
    c->count++;
}

/* --- тести --------------------------------------------------------------- */

/** Еталонний вектор із docs/03-protocol.md. Найважливіший тест у файлі. */
static void test_reference_vector(void)
{
    printf("еталонний вектор: порожній PING\n");

    const uint8_t want[] = {0xE7, 0x7E, 0x86, 0x00, 0x00, 0x66, 0x45};
    uint8_t out[RUI_FRAME_MAX];

    const size_t n = rui_build(out, RUI_PKT_PING, NULL, 0);
    CHECK(n == sizeof(want), "довжина %zu, чекали %zu", n, sizeof(want));
    CHECK(memcmp(out, want, sizeof(want)) == 0, "байти розійшлися: %02X %02X %02X %02X %02X %02X %02X",
          out[0], out[1], out[2], out[3], out[4], out[5], out[6]);

    /* І окремо сам CRC по TYPE+LEN, як його описує документ. */
    const uint8_t body[] = {0x86, 0x00, 0x00};
    CHECK(rui_crc16(body, sizeof(body)) == 0x4566, "CRC %04X, чекали 4566",
          rui_crc16(body, sizeof(body)));
}

static void test_input_state_zero(void)
{
    printf("обнулений INPUT_STATE\n");

    uint8_t out[RUI_FRAME_MAX];
    const size_t n = rui_build_input_state_zero(out);

    CHECK(n == RUI_INPUT_STATE_FRAME, "довжина %zu, чекали %d", n, RUI_INPUT_STATE_FRAME);
    CHECK(n == 20, "на дроті має бути рівно 20 Б, вийшло %zu", n);
    CHECK(out[2] == RUI_PKT_INPUT_STATE, "тип %02X", out[2]);
    CHECK(out[3] == RUI_INPUT_STATE_PAYLOAD && out[4] == 0, "довжина вантажу %u", out[3]);

    for (int i = 0; i < RUI_INPUT_STATE_PAYLOAD; ++i) {
        CHECK(out[5 + i] == 0, "вантаж не нульовий на зсуві %d", i);
    }

    /* Кадр має проходити власний розбирач — це і є перевірка CRC. */
    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, out, n, on_packet, &c);
    CHECK(c.count == 1 && s.crc_errors == 0, "власний кадр не пройшов розбирач");
}

static void test_scan_simple(void)
{
    printf("розбір: чистий потік\n");

    uint8_t a[RUI_FRAME_MAX], b[RUI_FRAME_MAX];
    const uint8_t payload[] = {1, 2, 3, 4, 5};
    const size_t na = rui_build(a, RUI_PKT_PING, NULL, 0);
    const size_t nb = rui_build(b, RUI_PKT_TILE, payload, sizeof(payload));

    uint8_t stream[64];
    memcpy(stream, a, na);
    memcpy(stream + na, b, nb);

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, stream, na + nb, on_packet, &c);

    CHECK(c.count == 2, "пакетів %d, чекали 2", c.count);
    CHECK(s.packets == 2 && s.crc_errors == 0 && s.oversized == 0, "лічильники розійшлися");
    CHECK(c.type[0] == RUI_PKT_PING && c.type[1] == RUI_PKT_TILE, "типи розійшлися");
    CHECK(c.len[1] == nb && memcmp(c.frame[1], b, nb) == 0,
          "кадр віддається не байт у байт — а міст пересилає саме його");
}

/** Розбирач має пережити будь-яке дроблення: WebSocket і UART ріжуть як хочуть. */
static void test_scan_split(void)
{
    printf("розбір: потік порізаний як завгодно\n");

    uint8_t frame[RUI_FRAME_MAX];
    uint8_t payload[300];
    for (size_t i = 0; i < sizeof(payload); ++i) {
        payload[i] = (uint8_t)(i * 7 + 3);
    }
    const size_t n = rui_build(frame, RUI_PKT_TILE, payload, sizeof(payload));

    for (size_t chunk = 1; chunk <= 17; ++chunk) {
        caught_t c = {0};
        rui_scanner_t s;
        rui_scanner_reset(&s);
        for (size_t off = 0; off < n; off += chunk) {
            const size_t take = (off + chunk <= n) ? chunk : (n - off);
            rui_scanner_feed(&s, frame + off, take, on_packet, &c);
        }
        CHECK(c.count == 1, "шматок %zu: пакетів %d", chunk, c.count);
        CHECK(c.count == 1 && c.len[0] == n && memcmp(c.frame[0], frame, n) == 0,
              "шматок %zu: кадр зіпсовано", chunk);
    }
}

static void test_scan_garbage(void)
{
    printf("розбір: сміття навколо кадру\n");

    uint8_t frame[RUI_FRAME_MAX];
    const size_t n = rui_build(frame, RUI_PKT_FRAME_END, NULL, 0);

    uint8_t stream[64];
    size_t k = 0;
    /* Сміття, у якому є і половинки маркера. */
    const uint8_t junk[] = {0x00, 0xE7, 0xE7, 0x11, 0x7E, 0xFF, 0xE7};
    memcpy(stream + k, junk, sizeof(junk));
    k += sizeof(junk);
    memcpy(stream + k, frame, n);
    k += n;
    stream[k++] = 0xE7; /* обірваний хвіст */

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, stream, k, on_packet, &c);

    CHECK(c.count == 1, "пакетів %d, чекали 1", c.count);
    CHECK(s.crc_errors == 0, "сміття не має рахуватись як помилка CRC, а порахувалось %u",
          s.crc_errors);
}

/** E7 E7 7E — другий байт сам виявився початком маркера. */
static void test_scan_double_marker(void)
{
    printf("розбір: E7 E7 7E\n");

    uint8_t frame[RUI_FRAME_MAX];
    const size_t n = rui_build(frame, RUI_PKT_PING, NULL, 0);

    uint8_t stream[32];
    stream[0] = 0xE7;
    memcpy(stream + 1, frame, n);

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, stream, n + 1, on_packet, &c);

    CHECK(c.count == 1, "пакетів %d, чекали 1", c.count);
}

static void test_scan_bad_crc(void)
{
    printf("розбір: битий CRC\n");

    uint8_t frame[RUI_FRAME_MAX];
    const uint8_t payload[] = {9, 9, 9};
    const size_t n = rui_build(frame, RUI_PKT_TILE, payload, sizeof(payload));
    frame[n - 1] ^= 0xFF;

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, frame, n, on_packet, &c);

    CHECK(c.count == 0, "битий пакет пішов далі — саме цього не має бути");
    CHECK(s.crc_errors == 1, "crc_errors %u, чекали 1", s.crc_errors);
    CHECK(s.packets == 0, "packets %u, чекали 0", s.packets);
}

/**
 * Брехлива довжина.
 *
 * Ресинхронізація має початися одразу за старшим байтом LEN, а не через
 * 60000 байтів — інакше один битий байт зробив би міст сліпим надовго.
 */
static void test_scan_oversized(void)
{
    printf("розбір: довжина понад стелю\n");

    uint8_t good[RUI_FRAME_MAX];
    const size_t ng = rui_build(good, RUI_PKT_PING, NULL, 0);

    uint8_t stream[32];
    size_t k = 0;
    stream[k++] = 0xE7;
    stream[k++] = 0x7E;
    stream[k++] = RUI_PKT_TILE;
    stream[k++] = 0xFF; /* LEN = 0xFFFF, тобто 65535 */
    stream[k++] = 0xFF;
    memcpy(stream + k, good, ng);
    k += ng;

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, stream, k, on_packet, &c);

    CHECK(s.oversized == 1, "oversized %u, чекали 1", s.oversized);
    CHECK(c.count == 1, "наступний цілий пакет загубився: пакетів %d", c.count);
}

static void test_max_payload(void)
{
    printf("розбір: вантаж на всю стелю\n");

    static uint8_t payload[RUI_MAX_PAYLOAD];
    static uint8_t frame[RUI_FRAME_MAX];
    for (size_t i = 0; i < sizeof(payload); ++i) {
        payload[i] = (uint8_t)(i ^ 0x5A);
    }

    const size_t n = rui_build(frame, RUI_PKT_TILE, payload, sizeof(payload));
    CHECK(n == RUI_FRAME_MAX, "довжина %zu, чекали %d", n, RUI_FRAME_MAX);
    CHECK(n == 4103, "стеля кадру має бути 4103 Б, вийшло %zu", n);

    caught_t c = {0};
    rui_scanner_t s;
    rui_scanner_reset(&s);
    rui_scanner_feed(&s, frame, n, on_packet, &c);
    CHECK(c.count == 1 && c.len[0] == n, "найбільший кадр не пройшов");

    /* Понад стелю зібрати не можна. */
    CHECK(rui_build(frame, RUI_PKT_TILE, payload, RUI_MAX_PAYLOAD + 1) == 0,
          "вантаж понад стелю зібрався, а не мав");
}

/** Перелік має збігатися з прошивкою пульта — інакше міст відпускатиме не те. */
static void test_carries_input(void)
{
    printf("які пакети несуть ввід\n");

    CHECK(rui_carries_input(RUI_PKT_KEY), "KEY має нести ввід");
    CHECK(rui_carries_input(RUI_PKT_ENC), "ENC має нести ввід");
    CHECK(rui_carries_input(RUI_PKT_TOUCH), "TOUCH має нести ввід");
    CHECK(rui_carries_input(RUI_PKT_TRIM), "TRIM має нести ввід");
    CHECK(rui_carries_input(RUI_PKT_INPUT_STATE), "INPUT_STATE має нести ввід");

    CHECK(!rui_carries_input(RUI_PKT_PING), "PING вводу не несе — на цьому тримається безпека");
    CHECK(!rui_carries_input(RUI_PKT_REFRESH), "REFRESH вводу не несе");
    CHECK(!rui_carries_input(0x7F), "невідомий тип вводу не несе");
}

int main(void)
{
    test_reference_vector();
    test_input_state_zero();
    test_scan_simple();
    test_scan_split();
    test_scan_garbage();
    test_scan_double_marker();
    test_scan_bad_crc();
    test_scan_oversized();
    test_max_payload();
    test_carries_input();

    printf("\n%s: перевірок %d, невдалих %d\n", g_failed ? "ПОМИЛКА" : "усе гаразд", g_checks,
           g_failed);
    return g_failed ? 1 : 0;
}
