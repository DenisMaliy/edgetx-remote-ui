/*
 * Точка входу тестового бінарника Remote UI.
 *
 * Самі тести — у test_crc16.cpp і test_protocol.cpp, вони реєструються
 * самі на етапі статичної ініціалізації.
 */

#include "test_harness.h"

int main()
{
  return test_harness::runAllTests();
}
