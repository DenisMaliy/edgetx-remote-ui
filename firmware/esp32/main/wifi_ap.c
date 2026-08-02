/**
 * @file wifi_ap.c
 * @brief Точка доступу моста: телефон під'єднується прямо до нього.
 *
 * Саме точка доступу, а не клієнт домашньої мережі: пульт беруть у поле, де
 * ніякого Wi-Fi немає. Мережа закрита паролем навмисно — через неї керують
 * пультом, і відкрита означала б, що будь-хто поруч може натискати клавіші.
 */

#include "wifi_ap.h"

#include <string.h>

#include "bridge_cfg.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ws_bridge.h"

/* ⚠️ Не «wifi»: цю мітку вже займає драйвер Wi-Fi самої ESP-IDF, і наші рядки
 * досі були від його рядків невідрізнимі. Наслідок не косметичний — заглушити
 * балакучий драйвер (`esp_log_level_set("wifi", …)` — той самий прийом, яким у
 * `ws_bridge_start` глушиться `httpd_ws`) означало б заглушити разом із ним
 * і «точка доступу піднята», і «телефон у мережі». */
static const char *TAG = "rui_wifi";

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;

    if (id == WIFI_EVENT_AP_STACONNECTED) {
        const wifi_event_ap_staconnected_t *e = (const wifi_event_ap_staconnected_t *)data;
        ESP_LOGI(TAG, "телефон у мережі: " MACSTR, MAC2STR(e->mac));
    } else if (id == WIFI_EVENT_AP_STADISCONNECTED) {
        const wifi_event_ap_stadisconnected_t *e = (const wifi_event_ap_stadisconnected_t *)data;
        ESP_LOGW(TAG, "станція вийшла з мережі: " MACSTR, MAC2STR(e->mac));

        /* ⚠️ Подія приходить на **будь-яку** станцію, а не тільки на ту, чий
         * сокет ми тримаємо. У мережі цілком може бути й ноутбук із
         * відкритою `/api/stats` — саме так буде на замірах. Закрита кришка
         * ноутбука не сміє рвати робочий сеанс телефона.
         *
         * Тому діємо тільки коли не лишилось **нікого**: тоді сокет, який ми
         * тримаємо, свідомо мертвий. Проміжні випадки за 750 мс добере
         * сторож мовчання, і це його робота, а не наша. */
        wifi_sta_list_t stations;
        if (esp_wifi_ap_get_sta_list(&stations) == ESP_OK && stations.num > 0) {
            ESP_LOGI(TAG, "у мережі лишилось %d — сеанс не чіпаю", stations.num);
            return;
        }

        /* Сокет беремо до рішення й гасимо саме його: поки ми питали список
         * станцій, міг під'єднатися вже інший телефон. */
        const int fd = ws_bridge_client_fd();
        if (fd >= 0) {
            ws_bridge_client_lost(fd, BRIDGE_LOST_WIFI);
        }
    }
}

esp_err_t wifi_ap_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(
        esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));

    wifi_config_t cfg = {
        .ap =
            {
                .ssid = BRIDGE_WIFI_SSID,
                .ssid_len = strlen(BRIDGE_WIFI_SSID),
                .password = BRIDGE_WIFI_PASS,
                .channel = BRIDGE_WIFI_CHANNEL,
                .max_connection = 2,
                .authmode = WIFI_AUTH_WPA2_PSK,
                .pmf_cfg = {.required = false},
            },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* ⚠️ Обидва виклики — тільки після esp_wifi_start().
     *
     * Заощадження живлення вимкнене свідомо: воно приспало б приймач між
     * маячками й додало десятки мілісекунд до кожного натискання. Ми міряємо
     * затримку від дотику до зміни на екрані, і платити нею за струм, що йде
     * від пульта, безглуздо. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(BRIDGE_WIFI_TX_POWER));

    int8_t power = 0;
    esp_wifi_get_max_tx_power(&power);

    /* Друкуємо цілими: дробове форматування в newlib-nano є не на кожній
     * цілі, і повідомлення мовчки перетворилося б на «%f». */
    ESP_LOGI(TAG, "точка доступу «%s», канал %d, потужність %d чверток дБм (%d.%d дБм)",
             BRIDGE_WIFI_SSID, BRIDGE_WIFI_CHANNEL, power, power / 4, (power % 4) * 25);
    ESP_LOGI(TAG, "сторінка: http://192.168.4.1/");

    return ESP_OK;
}

static void wifi_stop_task(void *arg)
{
    const uint32_t delay_ms = (uint32_t)(uintptr_t)arg;
    vTaskDelay(pdMS_TO_TICKS(delay_ms));

    ESP_LOGW(TAG, "вимикаю Wi-Fi на вимогу; увімкнути назад — перезавантаженням моста");
    esp_wifi_stop();

    vTaskDelete(NULL);
}

void wifi_ap_stop_deferred(uint32_t delay_ms)
{
    xTaskCreate(wifi_stop_task, "wifi_off", 2560, (void *)(uintptr_t)delay_ms, 5, NULL);
}
