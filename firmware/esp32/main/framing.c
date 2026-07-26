/**
 * @file framing.c
 * @brief Кадрування Remote UI на боці моста. Пояснення — у framing.h.
 */

#include "framing.h"

#include <string.h>

uint16_t rui_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc = rui_crc16_update(crc, data[i]);
    }
    return crc;
}

size_t rui_build(uint8_t *out, uint8_t type, const uint8_t *payload, size_t len)
{
    if (len > RUI_MAX_PAYLOAD) {
        return 0;
    }

    out[0] = RUI_MARKER0;
    out[1] = RUI_MARKER1;
    out[2] = type;
    out[3] = (uint8_t)(len & 0xFFu);
    out[4] = (uint8_t)((len >> 8) & 0xFFu);
    if (len && payload) {
        memcpy(out + 5, payload, len);
    }

    /* CRC рахується по TYPE + LEN + PAYLOAD. Обидва байти LEN входять у тому
     * порядку, в якому йдуть на дроті (docs/03-protocol.md). */
    const uint16_t crc = rui_crc16(out + 2, len + 3);
    out[5 + len] = (uint8_t)(crc & 0xFFu);
    out[6 + len] = (uint8_t)((crc >> 8) & 0xFFu);

    return RUI_FRAME_OVERHEAD + len;
}

size_t rui_build_input_state_zero(uint8_t *out)
{
    static const uint8_t zeros[RUI_INPUT_STATE_PAYLOAD] = {0};
    return rui_build(out, RUI_PKT_INPUT_STATE, zeros, sizeof(zeros));
}

void rui_scanner_reset(rui_scanner_t *s)
{
    s->pos = 0;
    s->need = 0;
    s->crc = 0xFFFFu;
    s->got_crc = 0;
    s->state = RUI_S_SYNC0;
}

void rui_scanner_feed(rui_scanner_t *s, const uint8_t *data, size_t len, rui_packet_cb cb,
                      void *ctx)
{
    for (size_t i = 0; i < len; ++i) {
        const uint8_t b = data[i];

        switch (s->state) {
        case RUI_S_SYNC0:
            if (b == RUI_MARKER0) {
                s->frame[0] = b;
                s->pos = 1;
                s->state = RUI_S_SYNC1;
            }
            break;

        case RUI_S_SYNC1:
            if (b == RUI_MARKER1) {
                s->frame[1] = b;
                s->pos = 2;
                s->crc = 0xFFFFu;
                s->state = RUI_S_TYPE;
            } else if (b == RUI_MARKER0) {
                /* E7 E7 7E — другий байт сам виявився початком маркера. */
                s->pos = 1;
            } else {
                s->pos = 0;
                s->state = RUI_S_SYNC0;
            }
            break;

        case RUI_S_TYPE:
            s->frame[2] = b;
            s->crc = rui_crc16_update(s->crc, b);
            s->pos = 3;
            s->state = RUI_S_LEN0;
            break;

        case RUI_S_LEN0:
            s->frame[3] = b;
            s->crc = rui_crc16_update(s->crc, b);
            s->need = b;
            s->pos = 4;
            s->state = RUI_S_LEN1;
            break;

        case RUI_S_LEN1:
            s->frame[4] = b;
            s->crc = rui_crc16_update(s->crc, b);
            s->need |= (size_t)b << 8;
            if (s->need > RUI_MAX_PAYLOAD) {
                /* Брехлива довжина: стільки байтів не читаємо й не
                 * пропускаємо — ресинхронізація починається одразу за
                 * старшим байтом LEN. Так само робить розбирач у прошивці
                 * й у tools/remote_ui_proto.py. */
                s->oversized++;
                s->pos = 0;
                s->state = RUI_S_SYNC0;
            } else {
                s->pos = 5;
                s->state = s->need ? RUI_S_PAYLOAD : RUI_S_CRC0;
            }
            break;

        case RUI_S_PAYLOAD:
            s->frame[s->pos++] = b;
            s->crc = rui_crc16_update(s->crc, b);
            if (--s->need == 0) {
                s->state = RUI_S_CRC0;
            }
            break;

        case RUI_S_CRC0:
            s->frame[s->pos++] = b;
            s->got_crc = b;
            s->state = RUI_S_CRC1;
            break;

        case RUI_S_CRC1:
            s->frame[s->pos++] = b;
            s->got_crc |= (uint16_t)b << 8;
            if (s->got_crc == s->crc) {
                s->packets++;
                if (cb) {
                    cb(ctx, s->frame[2], s->frame, s->pos);
                }
            } else {
                s->crc_errors++;
            }
            s->pos = 0;
            s->state = RUI_S_SYNC0;
            break;

        default:
            s->pos = 0;
            s->state = RUI_S_SYNC0;
            break;
        }
    }
}
