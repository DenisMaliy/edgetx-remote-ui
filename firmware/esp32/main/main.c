/**
 * @file main.c
 * @brief Міст TX16S Remote UI: пульт по дроту, телефон по Wi-Fi.
 *
 * ## Дві задачі, а не одна, і чому саме так
 *
 * Приймання з UART **не має права блокуватись**. На 921600 бод апаратна
 * черга наповнюється за 1.4 мс, і задача, що заснула в надрах Wi-Fi, з'їла б
 * кадри мовчки. Тому шлях розрізаний надвоє скінченною чергою:
 *
 * ```
 *   UART ──► rx_task ──► черга ──► ws_tx_task ──► Wi-Fi
 *            (не чекає)  (скінченна)  (має право чекати)
 * ```
 *
 * Черга скінченна навмисно, і її переповнення **і є** тим самим «захлинувся
 * WebSocket — плитки відкидаються», що ухвалено в ADR-0003. Місце, де ми
 * відкидаємо, тут рівно одне, і воно рахується.
 *
 * ⚠️ Це **не** накопичення кадру. У черзі лежить щонайбільше 24 КБ, тоді як
 * повний кадр екрана — 255 КБ і не влазить у жодну з плат.
 */

#include <string.h>

#include "baud_follow.h"
#include "bridge_cfg.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "framing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "heap_watch.h"
#include "nvs_flash.h"
#include "stats.h"
#include "uart_link.h"
#include "wifi_ap.h"
#include "ws_bridge.h"

static const char *TAG = "bridge";

/* Пачка мусить вміщати щонайменше один найбільший кадр цілком, інакше
 * `chunk_flush` нижче не врятує й станеться вихід за межі буфера. */
_Static_assert(BRIDGE_WS_CHUNK_BYTES >= RUI_FRAME_MAX,
               "пачка WebSocket менша за найбільший кадр протоколу");

/* Черга мусить вміщати пачку, інакше `xRingbufferSend` не прийме її ніколи —
 * і міст мовчки відкидав би все, показуючи зростання `chunks_dropped`. */
_Static_assert(BRIDGE_WS_RING_BYTES > BRIDGE_WS_CHUNK_BYTES,
               "черга до Wi-Fi не вміщає навіть однієї пачки");

/* Найбільший пакет від клієнта — `INPUT_STATE`, 20 Б на дроті. Якщо стеля
 * приймання опиниться нижче, міст рватиме з'єднання на кожному пакеті. */
_Static_assert(BRIDGE_WS_RX_MAX >= RUI_INPUT_STATE_FRAME,
               "стеля приймання нижча за найбільший пакет від клієнта");

static RingbufHandle_t s_ring;
static rui_scanner_t s_uart_scan;

/* Пачка, яку збирає rx_task. Тільки його, іншим тут робити нічого. */
static uint8_t s_chunk[BRIDGE_WS_CHUNK_BYTES];
static size_t s_chunk_len;
static uint32_t s_chunk_tiles;
static uint32_t s_chunk_packets;

/**
 * @brief Віддати зібрану пачку в чергу до Wi-Fi.
 *
 * Тут і тільки тут міст щось відкидає — і рахує, що саме.
 */
static void chunk_flush(void)
{
    if (s_chunk_len == 0) {
        return;
    }

    /* ⚠️ Рахуємо **тут**, а не в `ws_bridge_send`: там видно лише те, що
     * доїхало, а найбільші пачки — саме ті, що гинуть. Середнє по
     * `ws_bytes/ws_chunks` через це занижене, і саме на ньому будувалось би
     * рішення про розмір пачки (критерій 3 задачі 0018). */
    g_stats.chunks_built++;
    g_stats.chunk_bytes_built += (uint32_t)s_chunk_len;
    if (s_chunk_len > g_stats.chunk_len_max) {
        g_stats.chunk_len_max = (uint32_t)s_chunk_len;
    }

    if (!ws_bridge_has_client()) {
        /* Телефона немає. Це не втрата: пульт говорить лише у відповідь на
         * `PING`, тож сюди потрапляє хіба хвіст кадру, початий за мить до
         * розриву. Рахуємо окремо, щоб не псувати мірку заторів. */
        g_stats.chunks_noclient++;
    } else {
        /* ⚠️ Вільне місце знімаємо **перед** спробою: саме в цю мить черга
         * найповніша. Число відповідає на питання, якого досі не було кому
         * поставити — чи втрати взагалі в черзі. Далеке від нуля означає,
         * що ні, і тоді шукати треба в передаванні. */
        const size_t ring_free = xRingbufferGetCurFreeSize(s_ring);
        if (ring_free < g_stats.ring_free_min) {
            g_stats.ring_free_min = (uint32_t)ring_free;
        }

        if (xRingbufferSend(s_ring, s_chunk, s_chunk_len, 0) != pdTRUE) {
            /* Wi-Fi не встигає. Відкидаємо цілими пакетами — саме заради
             * цього міст і розбирає кадрування (framing.h). */
            g_stats.chunks_dropped++;
            g_stats.tiles_dropped += s_chunk_tiles;
            g_stats.packets_dropped += s_chunk_packets;
        }
    }

    s_chunk_len = 0;
    s_chunk_tiles = 0;
    s_chunk_packets = 0;
}

static void on_uart_packet(void *ctx, uint8_t type, const uint8_t *frame, size_t len)
{
    (void)ctx;

    /* ⚠️ Стежець за швидкістю дивиться на **вантаж**, а не на кадр: обгортка
     * йому ні до чого, а зсуви полів у неї інші. Сюди приходить цілий кадр,
     * тож вантаж — це frame + 5, а його довжина на RUI_FRAME_OVERHEAD менша. */
    if (len >= RUI_FRAME_OVERHEAD) {
        baud_follow_on_packet(type, frame + 5, len - RUI_FRAME_OVERHEAD);
    }

    if (type == RUI_PKT_TILE) {
        g_stats.tiles_in++;
    } else if (type == RUI_PKT_FRAME_END) {
        g_stats.frames_in++;
    }

    if (s_chunk_len + len > sizeof(s_chunk)) {
        chunk_flush();
    }

    memcpy(s_chunk + s_chunk_len, frame, len);
    s_chunk_len += len;
    s_chunk_packets++;
    if (type == RUI_PKT_TILE) {
        s_chunk_tiles++;
    }

    /* `FRAME_END` віддаємо негайно: саме він дозволяє клієнту показати кадр,
     * і затримка тут була б затримкою рівно там, де її видно оком. */
    if (type == RUI_PKT_FRAME_END) {
        chunk_flush();
    }
}

static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t rd[2048];
    int64_t last_byte_us = esp_timer_get_time();
    int64_t last_beat_us = last_byte_us;

    for (;;) {
        const int n = uart_link_read(rd, sizeof(rd), 10);
        const int64_t now = esp_timer_get_time();

        if (n > 0) {
            last_byte_us = now;
            g_stats.uart_bytes += (uint32_t)n;
            rui_scanner_feed(&s_uart_scan, rd, (size_t)n, on_uart_packet, NULL);

            g_stats.packets_ok = s_uart_scan.packets;
            g_stats.crc_errors = s_uart_scan.crc_errors;
            g_stats.oversized = s_uart_scan.oversized;
        } else if ((now - last_byte_us) / 1000 > BRIDGE_SILENCE_RESET_MS) {
            /* ⚠️ Скид розбирача за тишею — обов'язок транспортного шару, і
             * протокол кладе його саме сюди: у кадрування часу немає, воно
             * не знає, скільки минуло між байтами.
             *
             * Без цього пульт, вимкнений посеред плитки, лишає розбирач
             * назавжди застряглим у стані «дочитую вантаж» — і він з'їдає
             * початок потоку після ввімкнення, зокрема `HELLO` для нового
             * телефона. Замасковано повтором вітання: клієнт просто питав би
             * знову й знову, а виглядало б це як мертвий міст. */
            if (s_uart_scan.state != RUI_S_SYNC0) {
                g_stats.silence_resets++;
            }
            rui_scanner_reset(&s_uart_scan);
            last_byte_us = now;
        }

        uart_link_poll_events();

        /* ⚠️ Перемикання швидкості робиться тут, а не в обробнику пакета:
         * там воно скинуло б розбирач посеред його ж роботи. Скинути розбирач
         * після перемикання обов'язково — усе, що в ньому лежало, приймалось
         * на іншій швидкості й на новій означає сміття. */
        if (baud_follow_tick(now)) {
            rui_scanner_reset(&s_uart_scan);
            last_byte_us = now;
        }

        /* Наприкінці кожного читання: дрібна зміна не має чекати, доки
         * набереться повна пачка. */
        chunk_flush();

        /* ⚠️ Знімок купи знімається **тут**, а не в сторожі раз на 100 мс, і
         * це не байдуже. Просідання, заради якого прилад існує, триває стільки,
         * скільки віддається файл сторінки, — десятки мілісекунд. Сторож із
         * періодом 100 мс проходив би повз нього через раз і показував би
         * випадкове число замість найгіршого.
         *
         * Ціна — читання кількох лічильників розподільника, без замка й без
         * обходу блоків; сам прилад обмежує себе одним знімком на мілісекунду. */
        heap_watch_sample(now);

        /* Биття для заміру затримки задачі, що чує ввід. Джерелом часу мусить
         * бути задача, яка процесор отримує **завжди**, — інакше замір
         * замовкне разом із тим, що він міряє. Ця саме така: найвищий із
         * наших пріоритетів і власне ядро. */
        if ((now - last_beat_us) / 1000 >= BRIDGE_HEARTBEAT_MS) {
            last_beat_us = now;
            ws_bridge_heartbeat();
        }
    }
}

static void ws_tx_task(void *arg)
{
    (void)arg;

    for (;;) {
        size_t size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_ring, &size, pdMS_TO_TICKS(200));
        if (!item) {
            continue;
        }

        ws_bridge_send(item, size);
        vRingbufferReturnItem(s_ring, item);
    }
}

/**
 * @brief Сторож мовчання телефона.
 *
 * Найважливіший спосіб помітити, що ввід застарів. Закриття сокета ловить
 * штатний вихід, подія Wi-Fi ловить порожню мережу, а от телефон, який
 * просто винесли за межу зв'язку, не породжує **жодної** події: сокет із
 * боку моста лишається відкритим ще десятки секунд. Саме в цьому випадку
 * клавіша й лишилася б натиснутою.
 *
 * ⚠️ Сторож **відпускає ввід, але не рве сокет.** «Ввід застарів» і
 * «телефона немає» — різні висновки, і другий йому не належить: браузер
 * душить таймери у схованій вкладці, тож згаслий екран легко дає мовчання
 * довше за поріг, а розрив коштував би перепідключення й повного `REFRESH`
 * саме там, де ми міряємо затримку.
 */
static void watchdog_task(void *arg)
{
    (void)arg;

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(BRIDGE_WATCHDOG_PERIOD_MS));

        if (ws_bridge_release_if_silent(BRIDGE_CLIENT_SILENCE_MS)) {
            ESP_LOGW(TAG, "клієнт мовчить понад %d мс — ввід відпущено, сокет лишаю",
                     BRIDGE_CLIENT_SILENCE_MS);
        }

        /* ⚠️ Скаргу на низьку купу друкує саме ця задача, а помічає її
         * `rx_task`. Розділено навмисно: консоль — синхронний запис у UART0 на
         * 115200, тобто ~10 мс блокування на рядок, а `rx_task` не має права
         * чекати взагалі. Друк у ній перетворив би попередження про брак
         * пам'яті на втрату байтів із дроту — тобто на другу ваду поруч із
         * першою. */
        uint32_t heap_left = 0;
        const char *heap_phase = NULL;
        if (heap_watch_take_warning(&heap_left, &heap_phase)) {
            ESP_LOGW(TAG,
                     "вільної купи лишилось %u Б (межа %d) під час «%s» — "
                     "далі міст живе на межі; далі мовчу, число видно в /api/stats",
                     (unsigned)heap_left, BRIDGE_HEAP_WARN_BYTES, heap_phase);
        }
    }
}

void app_main(void)
{
    /* ⚠️ Найперший рядок: стеля купи знімається **до** будь-якого нашого
     * розподілу, інакше «з'їдено» рахувалось би від уже надкушеного. */
    heap_watch_init();

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    const esp_app_desc_t *app = esp_app_get_description();
    ESP_LOGI(TAG, "TX16S Remote UI — міст, збірка %s %s", app->date, app->time);
    heap_watch_mark("старт");

    /* Канал до пульта піднімаємо першим: обнулений `INPUT_STATE` має бути
     * куди слати ще до того, як з'явиться перший телефон. */
    ESP_ERROR_CHECK(uart_link_init());
    heap_watch_mark("приймальний буфер UART");

    /* ⚠️ Перше, що міст каже пульту, — «відпусти все».
     *
     * Перезавантаження моста (кидок живлення, `idf.py flash`, натиснута
     * кнопка) — це теж «втратив телефон», просто з іншого боку: пульт про
     * той розрив не дізнався й досі тримає натиснутим те, що тримав. Один
     * пакет на 20 байтів закриває цей випадок назавжди. */
    uart_link_send_input_release(BRIDGE_LOST_BOOT);

    /* Стан моста готуємо **до** підняття Wi-Fi: інакше подія в проміжку
     * візьме ще не створений замок (знахідка рецензії). */
    ESP_ERROR_CHECK(ws_bridge_init());

    rui_scanner_reset(&s_uart_scan);
    baud_follow_init();
    s_ring = xRingbufferCreate(BRIDGE_WS_RING_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_ring) {
        ESP_LOGE(TAG, "не вистачило пам'яті на чергу до Wi-Fi");
        abort();
    }
    /* «Найменше вільне» мусить починатися зі стелі, інакше нуль у знімку
     * означав би і «черга впиралась», і «жодної пачки ще не було». */
    g_stats.ring_free_min = xRingbufferGetCurFreeSize(s_ring);
    heap_watch_mark("черга до Wi-Fi");

    ESP_ERROR_CHECK(wifi_ap_start());
    heap_watch_mark("Wi-Fi і мережевий стек");
    ESP_ERROR_CHECK(ws_bridge_start());
    heap_watch_mark("сервер HTTP");

    /* Приймання — найвищий пріоритет із наших трьох і власне ядро там, де
     * ядер два. Задачі Wi-Fi і TCP/IP усе одно вищі за нас; це правильно —
     * ми не маємо права заважати їм, як і транспорт у пульті не має права
     * заважати мікшеру. */
    /* ⚠️ Кожен виклик порівнюється з `pdPASS` **окремо**, і це не стиль.
     *
     * Попередній варіант накопичував результати через `ok &=` і не працював
     * зовсім: при відмові `xTaskCreate` віддає не нуль, а
     * `errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY`, тобто **-1**
     * (`projdefs.h:67`), тоді як `pdPASS` — це 1. Побітове «і» дає
     * `1 & -1 == 1`, тож накопичувач лишався успішним завжди, і перевірка
     * нижче не спрацьовувала жодного разу.
     *
     * Найгірше в цьому те, що вада сиділа **всередині** перевірки, доданої
     * саме проти мовчазної відмови: код виглядав захищеним і не був. Будь-яка
     * функція, що позначає помилку від'ємним числом, проходить крізь `&=`
     * непоміченою — шаблону більше немає ніде в проєкті, перевірено пошуком.
     */
    bool ok = true;
    ok = ok && (xTaskCreatePinnedToCore(rx_task, "rui_rx", 4096, NULL, BRIDGE_PRIO_RX, NULL,
                                        BRIDGE_RX_CORE) == pdPASS);
    ok = ok && (xTaskCreate(ws_tx_task, "rui_ws_tx", 4096, NULL, BRIDGE_PRIO_WS_TX, NULL)
                == pdPASS);
    ok = ok && (xTaskCreate(watchdog_task, "rui_wd", 3072, NULL, BRIDGE_PRIO_WATCHDOG, NULL)
                == pdPASS);

    /* Мовчазна відмова — найгірший вид відмови в цьому проєкті. Не вистачило
     * купи на сторож — і міст працює **без головного механізму безпеки**, а в
     * журналі стоїть «міст працює». Краще не стартувати зовсім: це видно
     * одразу й ні на що не схоже. */
    if (!ok) {
        ESP_LOGE(TAG, "не вдалося створити задачі — міст без них небезпечний");
        abort();
    }

    heap_watch_mark("наші три задачі");

    /* ⚠️ Вікно заміру (і разом із ним перевірка на витік) починається **тут**,
     * а не в `heap_watch_init()`. Там воно захопило б яму самого старту —
     * підняття Wi-Fi і сервера, — і «перша хвилина» назавжди лишилась би
     * найглибшою, тобто порівняння з нею не спрацювало б ніколи. */
    heap_watch_window_reset();

    ESP_LOGI(TAG, "міст працює");
}
