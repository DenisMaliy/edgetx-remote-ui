/*
 * TX16S Remote UI — пакет HELLO: опис заліза для клієнта.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * Це єдиний наш файл (крім гачка), який звертається до API EdgeTX: розмір
 * екрана, перелік клавіш, кількість тримерів, назва цілі, версія. Так і
 * задумано — специфіка конкретного пульта не пишеться в код, а питається в
 * EdgeTX і віддається клієнту (CLAUDE.md).
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

namespace remote_ui {

// Прапорці в полі «прапорці» пакета HELLO.
constexpr uint8_t HELLO_FLAG_TOUCH = 0x01;
constexpr uint8_t HELLO_FLAG_ENCODER = 0x02;
constexpr uint8_t HELLO_FLAG_FILE_OPS = 0x04;

// Біт3: прошивка розуміє пакет `0x87 INPUT_STATE`.
//
// ⚠️ Це не властивість заліза, а версія прошивки, тому біт не має жодного
// `#if`: код, який його виставляє, і код, який застосовує `INPUT_STATE`,
// зібрані з одного дерева. Нуль означає стару прошивку — таку, де правило
// «тайм-аут відсувають лише пакети, що несуть ввід» ще не діє, і де ввід
// доводиться утримувати `PING`-ами (docs/03-protocol.md, розділ «Як клієнт
// дізнається, що прошивка це вміє»).
constexpr uint8_t HELLO_FLAG_INPUT_STATE = 0x08;

// Формати пікселя.
constexpr uint8_t HELLO_PIXFMT_RGB565 = 1;
constexpr uint8_t HELLO_PIXFMT_MONO1 = 2;

// Довжини текстових полів, доповнюваних нулями.
constexpr size_t HELLO_KEY_NAME_LEN = 16;
constexpr size_t HELLO_TARGET_LEN = 32;
constexpr size_t HELLO_VERSION_LEN = 16;

// Стеля розміру HELLO за розкладкою docs/03-protocol.md: 13 байтів сталої
// частини + запис на кожну клавішу + назва цілі + версія. Виведена з
// `maxKeys`, а не прибита числом: перелік клавіш належить EdgeTX і може
// вирости при оновленні. Викликач бере звідси розмір буфера — інакше
// `buildHello()` одного дня почав би тихо повертати 0.
constexpr size_t helloMaxSize(size_t maxKeys)
{
  return 13 + maxKeys * (1 + HELLO_KEY_NAME_LEN) + HELLO_TARGET_LEN +
         HELLO_VERSION_LEN;
}

// Складає PAYLOAD пакета HELLO за docs/03-protocol.md.
// Повертає довжину або 0, якщо в буфері не вистачило місця.
size_t buildHello(uint8_t* out, size_t outSize);

}  // namespace remote_ui
