/**
 * @file wifi_ap.h
 * @brief Точка доступу моста.
 */

#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t wifi_ap_start(void);

/**
 * @brief Вимкнути Wi-Fi через `delay_ms`, з окремої задачі.
 *
 * Із затримкою й не тут, бо команда приходить по тому самому Wi-Fi, який ми
 * вимикаємо: без затримки відповідь не встигла б доїхати.
 */
void wifi_ap_stop_deferred(uint32_t delay_ms);
