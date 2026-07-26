/*
 * Мінімальний каркас тестів для Remote UI.
 *
 * Навмисно свій, а не gtest: залежності тягнути нікуди, а весь каркас —
 * один заголовок на сотню рядків. Цикл «змінив → перевірив» має бути
 * секундним і не вимагати нічого, крім g++.
 *
 * Використання:
 *
 *   TEST(MyCase) {
 *     CHECK(2 + 2 == 4);
 *     CHECK_EQ(crc, 0x29B1);
 *   }
 *
 * Тест вважається проваленим, якщо хоч одна перевірка не справдилась або
 * якщо під час нього сталося виділення динамічної пам'яті.
 */

#pragma once

#include <stddef.h>
#include <stdio.h>

#include <chrono>

#include "alloc_guard.h"

namespace test_harness {

// Стеля кількості тестів. Перевищення видно в підсумку, мовчки не губиться.
constexpr int MAX_TESTS = 128;

struct TestEntry {
  const char* name;
  void (*fn)();
};

inline TestEntry g_tests[MAX_TESTS];
inline int g_testCount = 0;
inline int g_registrationOverflow = 0;

// Провалені перевірки в поточному тесті.
inline int g_failedChecks = 0;

// Скільки виділень пам'яті тест вважає нормою. Нуль для всіх, крім тесту,
// який перевіряє, що сам сторож не зламаний.
inline unsigned long g_expectedAllocations = 0;

// Реєстрація тесту на етапі статичної ініціалізації, без динамічної пам'яті.
struct Registrar {
  Registrar(const char* name, void (*fn)())
  {
    if (g_testCount < MAX_TESTS) {
      g_tests[g_testCount].name = name;
      g_tests[g_testCount].fn = fn;
      ++g_testCount;
    } else {
      ++g_registrationOverflow;
    }
  }
};

inline void reportFailure(const char* file, int line, const char* text)
{
  ++g_failedChecks;
  printf("         %s:%d  не справдилось: %s\n", file, line, text);
}

inline void reportFailureValues(const char* file, int line, const char* text,
                                unsigned long long actual,
                                unsigned long long expected)
{
  ++g_failedChecks;
  printf("         %s:%d  не справдилось: %s (маємо %llu / 0x%llX, "
         "чекали %llu / 0x%llX)\n",
         file, line, text, actual, actual, expected, expected);
}

inline void reportFailureIndex(const char* file, int line, const char* text,
                               size_t index, unsigned actual, unsigned expected)
{
  ++g_failedChecks;
  printf("         %s:%d  не справдилось: %s (байт %zu: маємо 0x%02X, "
         "чекали 0x%02X)\n",
         file, line, text, index, actual, expected);
}

// Прогін усіх зареєстрованих тестів. Повертає код виходу процесу.
inline int runAllTests()
{
  printf("Remote UI: тестів зареєстровано %d\n\n", g_testCount);

  int failedTests = 0;
  const auto started = std::chrono::steady_clock::now();

  for (int i = 0; i < g_testCount; ++i) {
    g_failedChecks = 0;
    g_expectedAllocations = 0;

    const unsigned long allocBefore = test_alloc::guardedAllocations();

    {
      // Захищена зона: будь-яке виділення пам'яті під час тесту помітне.
      test_alloc::Guard guard;
      g_tests[i].fn();
    }

    const unsigned long allocDelta =
        test_alloc::guardedAllocations() - allocBefore;

    if (allocDelta != g_expectedAllocations) {
      ++g_failedChecks;
      printf("         виділень динамічної пам'яті: %lu, дозволено %lu\n",
             allocDelta, g_expectedAllocations);
    }

    if (g_failedChecks == 0) {
      printf("[PASS] %s\n", g_tests[i].name);
    } else {
      printf("[FAIL] %s  (провалених перевірок: %d)\n", g_tests[i].name,
             g_failedChecks);
      ++failedTests;
    }
  }

  const auto finished = std::chrono::steady_clock::now();
  const double seconds =
      std::chrono::duration<double>(finished - started).count();

  printf("\n");
  printf("Пройдено %d з %d, час %.3f с\n", g_testCount - failedTests,
         g_testCount, seconds);
  printf("Виділень динамічної пам'яті в захищених зонах: %lu (усього за "
         "прогін: %lu)\n",
         test_alloc::guardedAllocations(), test_alloc::totalAllocations());

  if (g_registrationOverflow > 0) {
    printf("УВАГА: не влізло тестів: %d, збільш MAX_TESTS\n",
           g_registrationOverflow);
    ++failedTests;
  }

  return failedTests == 0 ? 0 : 1;
}

}  // namespace test_harness

// --- Макроси --------------------------------------------------------------

#define TEST(name)                                                    \
  static void name##_body();                                          \
  static ::test_harness::Registrar name##_registrar(#name, name##_body); \
  static void name##_body()

#define CHECK(cond)                                              \
  do {                                                           \
    if (!(cond)) {                                               \
      ::test_harness::reportFailure(__FILE__, __LINE__, #cond);  \
    }                                                            \
  } while (0)

#define CHECK_EQ(actual, expected)                                          \
  do {                                                                      \
    const unsigned long long checkActual =                                  \
        static_cast<unsigned long long>(actual);                            \
    const unsigned long long checkExpected =                                \
        static_cast<unsigned long long>(expected);                          \
    if (checkActual != checkExpected) {                                     \
      ::test_harness::reportFailureValues(__FILE__, __LINE__,               \
                                          #actual " == " #expected,         \
                                          checkActual, checkExpected);      \
    }                                                                       \
  } while (0)

// Побайтове порівняння з повідомленням про перший розбіжний байт.
#define CHECK_BYTES_EQ(actual, expected, len)                                \
  do {                                                                       \
    const uint8_t* checkA = (actual);                                        \
    const uint8_t* checkB = (expected);                                      \
    const size_t checkLen = (len);                                           \
    for (size_t checkI = 0; checkI < checkLen; ++checkI) {                   \
      if (checkA[checkI] != checkB[checkI]) {                                \
        ::test_harness::reportFailureIndex(__FILE__, __LINE__,               \
                                           #actual " == " #expected, checkI, \
                                           checkA[checkI], checkB[checkI]);  \
        break;                                                               \
      }                                                                      \
    }                                                                        \
  } while (0)
