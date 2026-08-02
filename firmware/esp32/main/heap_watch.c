/**
 * @file heap_watch.c
 * @brief Прилад вільної купи: за етапом, у вікні, по хвилинах.
 */

#include "heap_watch.h"

#include <stdio.h>

#include "bridge_cfg.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ws_bridge.h"

static const char *TAG = "heap";

/** Скільки кроків підняття моста прилад здатен запам'ятати. */
#define HEAP_MARKS_MAX 8

/** «Такого ще не траплялось» — у JSON виходить `null`, а не число. */
#define HEAP_NEVER UINT32_MAX

/** Довжина хвилинного вікна для перевірки на витік, мкс. */
#define HEAP_MINUTE_US (60 * 1000 * 1000)

/**
 * Скільки ще після виходу з обробника просідання належить віддачі сторінки, мс.
 *
 * ⚠️ Не запас «про всяк випадок», а виправлення хибної приписки, яку перший же
 * замір і показав: найглибша точка стояла в «спокої» (38.1 КБ), а не в
 * «сторінці» (41.2 КБ). Причина в тому, що `httpd_resp_send()` вертається,
 * коли байти **поставлені в чергу** lwIP, а не коли вони зійшли в ефір. Черга
 * TCP і буфери Wi-Fi тримають їх ще стільки, скільки триває передача.
 *
 * ⚠️ Число мале навмисно, і межа тут із двох боків. Найбільший файл після
 * стиснення — 22 КБ; на заміряних ~150 КБ/с він зникає з черги за ~150 мс.
 * Розтягувати хвіст далі не можна: рівно в цьому вікні браузер, дочитавши
 * сторінку, відкриває WebSocket і просить **повний кадр** на 20–40 КБ, і
 * довгий хвіст записав би найдорожчу дію потоку в графу «сторінка».
 *
 * Тобто прилад раніше брехав в один бік, і виправляти це треба було, не
 * почавши брехати в другий. Залишковий ризик названий прямо в `heap_watch.h`.
 */
#define HEAP_PAGE_TAIL_MS 200

static struct {
    const char *what;
    uint32_t free_after;
} s_marks[HEAP_MARKS_MAX];
static uint8_t s_marks_n;

/** Скільки було вільно до першої позначки — стеля, від якої все рахується. */
static uint32_t s_ceiling;

/** Мінімум за етапом. */
static uint32_t s_min_phase[HEAP_PHASE_COUNT];

/** Мінімум у вікні заміру (скидається запитом). */
static uint32_t s_min_window;

/* --- перевірка на витік --------------------------------------------------
 *
 * ⚠️ Витік ловиться **рівнем повернення**, а не мінімумом, і це виправлення
 * після першого ж прогону, який показав хибну тривогу.
 *
 * Задача формулює перевірку як «мінімум за десять хвилин проти мінімуму за
 * першу хвилину». Буквально так робити не можна: мінімум у хвилині задає не
 * дрейф запасу, а те, **чи трапилась у цій хвилині подія**. У прогоні
 * мінімум першої хвилини був 103.9 КБ (нічого не відбувалось), а другої —
 * 40.0 КБ (завантажилась сторінка). Правило «стало менше — витік» назвало б
 * витоком звичайне завантаження сторінки.
 *
 * Витік виглядає інакше: купа перестає **повертатись** на попередній рівень.
 * Тому міряється найбільше вільне за хвилину — той рівень, на який усе
 * відпускається після події. Він тане тільки тоді, коли щось справді не
 * повернули.
 */
static uint32_t s_recover_first;   /**< рівень повернення за першу хвилину */
static uint32_t s_recover_last;    /**< за останню **завершену** хвилину */
static uint32_t s_recover_this;    /**< за хвилину, що триває */
static uint32_t s_min_first_minute; /**< мінімум першої хвилини — лишається як є */
static uint32_t s_min_last_minute;
static uint32_t s_min_this_minute;
static int64_t s_minute_started_us;
static uint32_t s_minutes_done;

/** Глибина віддачі файлу сторінки. Пише лише задача httpd — вона одна. */
static volatile int s_page_depth;
/**
 * Коли востаннє вийшли з обробника віддачі, мс від старту.
 *
 * ⚠️ **Мілісекунди в 32 бітах, а не мікросекунди в 64.** Пише задача httpd,
 * читає `rx_task`; на 32-бітній Xtensa доступ до 64-бітного слова не
 * атомарний, і `volatile` цього не змінює — читач міг би зловити половину
 * старого значення і половину нового. Вікно вузьке (раз на ~71 хв, коли
 * міняється старше слово), а наслідок дрібний, але це саме той клас гонки,
 * який у цьому проєкті прийнято називати, а не переживати.
 *
 * 32 біти мілісекунд переповнюються за 49 діб; ціна переповнення — один
 * знімок, приписаний не тому етапу.
 */
static volatile uint32_t s_page_left_ms;

/** Знята, але ще не надрукована скарга на низьку купу.
 *  Усі чотири ділять `rx_task` (пише) і сторож (читає), тож `volatile` тут
 *  однаковий для всіх — половинчаста позначка збивала б із пантелику. */
static volatile bool s_warn_pending;
static volatile uint32_t s_warn_free;
static volatile heap_phase_t s_warn_phase;
static volatile bool s_warned_ever;

static int64_t s_last_sample_us;

/** Замовлення «почати нове вікно», яке виконає найближчий знімок. */
static volatile bool s_window_reset_req;

static void window_reset_now(void);

const char *heap_phase_name(heap_phase_t phase)
{
    switch (phase) {
    case HEAP_PHASE_IDLE:   return "спокій";
    case HEAP_PHASE_STREAM: return "потік пікселів";
    case HEAP_PHASE_PAGE:   return "віддача сторінки";
    case HEAP_PHASE_COUNT:  break;
    }
    return "невідомо";
}

/**
 * @brief Що міст робить просто зараз.
 *
 * ⚠️ Порядок перевірок не випадковий. Віддача сторінки йде **при живому
 * телефоні** теж (людина перезавантажила вкладку), і саме вона дорожча —
 * тож вона й перемагає. Інакше найглибші просідання приписувались би потоку
 * пікселів, який до них непричетний, і лікували б чергу до Wi-Fi.
 */
static heap_phase_t phase_now(int64_t now_us)
{
    const uint32_t now_ms = (uint32_t)(now_us / 1000);
    if (s_page_depth > 0 || (now_ms - s_page_left_ms) < HEAP_PAGE_TAIL_MS) {
        return HEAP_PHASE_PAGE;
    }
    return ws_bridge_has_client() ? HEAP_PHASE_STREAM : HEAP_PHASE_IDLE;
}

void heap_watch_init(void)
{
    const uint32_t now_free = (uint32_t)esp_get_free_heap_size();

    s_ceiling = now_free;
    /* ⚠️ Не `now_free`, а «не бувало». Етап, якого ще не траплялось, зі
     * стелею в полі читався б як «найгірше за віддачу сторінки — 253 КБ»,
     * тобто як доказ, що з нею все гаразд. Порожнє поле бреше менше. */
    for (size_t i = 0; i < HEAP_PHASE_COUNT; ++i) {
        s_min_phase[i] = HEAP_NEVER;
    }
    /* «Сторінку не віддавали жодного разу»: рахунок різниці беззнаковий, тож
     * далеке минуле — це рівно `now_ms - HEAP_PAGE_TAIL_MS - 1` і глибше. */
    s_page_left_ms = (uint32_t)(esp_timer_get_time() / 1000) - HEAP_PAGE_TAIL_MS - 1;
    /* Прямо, а не прапорцем: задач ще немає, конкурувати нема з ким, а поля
     * мусять бути заповнені **до** першого запиту `/api/stats`. */
    window_reset_now();
}

void heap_watch_mark(const char *what)
{
    const uint32_t now_free = (uint32_t)esp_get_free_heap_size();

    if (s_marks_n < HEAP_MARKS_MAX) {
        s_marks[s_marks_n].what = what;
        s_marks[s_marks_n].free_after = now_free;
        s_marks_n++;
    }

    /* Друкувати тут можна й треба: старт, консоль вільна, задач ще немає.
     * Це єдиний спосіб побачити розкладку постійно зайнятого, не питаючи
     * міст по мережі — а мережа на цьому кроці ще й не піднята. */
    ESP_LOGI(TAG, "після кроку «%s» вільно %u Б (з'їдено %u)", what,
             (unsigned)now_free, (unsigned)(s_ceiling - now_free));
}

void heap_watch_page_enter(void) { s_page_depth++; }
void heap_watch_page_leave(void)
{
    if (s_page_depth > 0) {
        s_page_depth--;
    }
    s_page_left_ms = (uint32_t)(esp_timer_get_time() / 1000);
}

void heap_watch_sample(int64_t now_us)
{
    /* ⚠️ Справжня частота знімків задається **не цим рядком**, а витком
     * `rx_task`: той читає UART із тайм-аутом 10 мс і буфером 2048 Б, який на
     * 2 625 000 бод набігає за ~7.8 мс. Тобто знімок робиться раз на 8–10 мс,
     * ~100–130 разів на секунду, і обмежувач нижче не спрацьовує **ніколи**.
     * Він лишається сторожем на випадок, якщо виток колись прискориться.
     *
     * ⚠️ Наслідок, який треба знати, читаючи числа: роздільність приладу —
     * 8–10 мс, тобто того ж порядку, що й тривалість просідання при віддачі
     * дрібного файлу. Дно могло бути глибшим за записане. Перевага над
     * сторожем із періодом 100 мс — десятикратна, а не стократна. */
    if (now_us - s_last_sample_us < 1000) {
        return;
    }
    s_last_sample_us = now_us;

    /* Скид виконується тут — щоб письменник полів вікна лишався один. */
    if (s_window_reset_req) {
        s_window_reset_req = false;
        window_reset_now();
    }

    const uint32_t now_free = (uint32_t)esp_get_free_heap_size();
    const heap_phase_t phase = phase_now(now_us);

    if (now_free < s_min_phase[phase]) {
        s_min_phase[phase] = now_free;
    }
    if (now_free < s_min_window) {
        s_min_window = now_free;
    }
    if (now_free < s_min_this_minute) {
        s_min_this_minute = now_free;
    }
    if (now_free > s_recover_this) {
        s_recover_this = now_free;
    }

    if (now_us - s_minute_started_us >= HEAP_MINUTE_US) {
        s_min_last_minute = s_min_this_minute;
        s_recover_last = s_recover_this;
        if (s_minutes_done == 0) {
            s_min_first_minute = s_min_this_minute;
            s_recover_first = s_recover_this;
        }
        s_minutes_done++;
        s_min_this_minute = now_free;
        s_recover_this = now_free;
        s_minute_started_us = now_us;
    }

    if (!s_warned_ever && !s_warn_pending && now_free < BRIDGE_HEAP_WARN_BYTES) {
        s_warn_free = now_free;
        s_warn_phase = phase;
        s_warn_pending = true; /* друкує сторож — див. коментар у заголовку */
    }
}

bool heap_watch_take_warning(uint32_t *free_bytes, const char **phase)
{
    if (!s_warn_pending) {
        return false;
    }
    /* ⚠️ Порядок обов'язковий: спершу «більше ніколи», потім «уже забрав».
     * У зворотному порядку `rx_task` встигає в проміжку побачити
     * `!s_warned_ever && !s_warn_pending` і звести прапорець наново — і
     * одноразове попередження друкується вдруге. */
    s_warned_ever = true;
    s_warn_pending = false;

    if (free_bytes) {
        *free_bytes = s_warn_free;
    }
    if (phase) {
        *phase = heap_phase_name(s_warn_phase);
    }
    return true;
}

uint32_t heap_watch_window_min(void) { return s_min_window; }

/**
 * @brief Власне перезапуск вікна. Кличеться **лише** з `heap_watch_sample()`
 *        або зі старту, доки задач ще немає.
 */
static void window_reset_now(void)
{
    const uint32_t now_free = (uint32_t)esp_get_free_heap_size();

    s_min_window = now_free;

    /* ⚠️ Хвилинний відлік перезапускається **разом** із вікном заміру, і це не
     * причісування. Раніше він починався в `heap_watch_init()` — тобто першим
     * рядком `app_main`, до підняття Wi-Fi і сервера. «Перша хвилина»
     * запам'ятовувала найглибшу яму старту, і порівняння з нею не спрацювало б
     * ніколи: перевірка «чи тане запас» перетворювалась на машину, яка завжди
     * каже «тримається». А перевірка, що завжди мовчить, гірша за відсутню.
     *
     * Наслідок для замірів: `POST /api/heap/reset` починає **і** вікно, **і**
     * перевірку на витік. Це навмисно — новий замір має бути новим цілком. */
    s_min_first_minute = HEAP_NEVER;
    s_min_last_minute = HEAP_NEVER;
    s_min_this_minute = now_free;
    s_recover_first = HEAP_NEVER;
    s_recover_last = HEAP_NEVER;
    s_recover_this = now_free;
    s_minutes_done = 0;
    s_minute_started_us = esp_timer_get_time();
}

/**
 * @brief Попросити почати нове вікно.
 *
 * ⚠️ Прохання, а не дія, і це не ускладнення заради краси. Усі поля вікна
 * пише `rx_task` у `heap_watch_sample()`; якби їх писала ще й задача httpd,
 * виникала б гонка з єдиним неприємним наслідком — «читай-зміни-запиши» в
 * знімку міг би **повернути назад** щойно зроблений скид, і вікно почалося б
 * не з чистого аркуша. Обіцянка «замір починається зараз» мовчки не
 * виконувалась би.
 *
 * З прапорцем письменник лишається один, а затримка — до одного знімка,
 * тобто менша за 10 мс.
 */
void heap_watch_window_reset(void) { s_window_reset_req = true; }

/** Число або `null`, якщо такого етапу ще не траплялось. Буфер на виклик свій:
 *  усі п'ять значень підставляються в один `snprintf`. */
static const char *or_null(uint32_t v, char *buf, size_t cap)
{
    if (v == HEAP_NEVER) {
        return "null";
    }
    snprintf(buf, cap, "%u", (unsigned)v);
    return buf;
}

size_t heap_watch_json(char *out, size_t cap)
{
    char b_idle[12], b_stream[12], b_page[12], b_first[12], b_last[12];
    char b_rec_first[12], b_rec_last[12];

    int n = snprintf(out, cap,
                     "{\"ceiling\":%u,\"warn_at\":%u,\"warned\":%s,"
                     "\"min_window\":%u,\"min_idle\":%s,\"min_stream\":%s,"
                     "\"min_page\":%s,\"min_first_min\":%s,\"min_last_min\":%s,"
                     "\"recover_first_min\":%s,\"recover_last_min\":%s,"
                     "\"minutes\":%u,\"boot\":[",
                     (unsigned)s_ceiling, (unsigned)BRIDGE_HEAP_WARN_BYTES,
                     s_warned_ever ? "true" : "false", (unsigned)s_min_window,
                     or_null(s_min_phase[HEAP_PHASE_IDLE], b_idle, sizeof(b_idle)),
                     or_null(s_min_phase[HEAP_PHASE_STREAM], b_stream, sizeof(b_stream)),
                     or_null(s_min_phase[HEAP_PHASE_PAGE], b_page, sizeof(b_page)),
                     or_null(s_min_first_minute, b_first, sizeof(b_first)),
                     or_null(s_min_last_minute, b_last, sizeof(b_last)),
                     or_null(s_recover_first, b_rec_first, sizeof(b_rec_first)),
                     or_null(s_recover_last, b_rec_last, sizeof(b_rec_last)),
                     (unsigned)s_minutes_done);
    if (n < 0 || (size_t)n >= cap) {
        return 0;
    }

    for (uint8_t i = 0; i < s_marks_n; ++i) {
        const int k = snprintf(out + n, cap - (size_t)n, "%s[\"%s\",%u]", i ? "," : "",
                               s_marks[i].what, (unsigned)s_marks[i].free_after);
        if (k < 0 || (size_t)(n + k) >= cap) {
            return 0;
        }
        n += k;
    }

    const int k = snprintf(out + n, cap - (size_t)n, "]}");
    if (k < 0 || (size_t)(n + k) >= cap) {
        return 0;
    }
    return (size_t)(n + k);
}
