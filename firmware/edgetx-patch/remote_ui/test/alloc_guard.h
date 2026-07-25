/*
 * Сторож динамічної пам'яті для тестів Remote UI.
 *
 * Тільки для тестового бінарника на комп'ютері — у прошивку не потрапляє.
 *
 * Ідея проста: перевизначені глобальні operator new / operator new[] рахують
 * кожне виділення. Поки активна «захищена зона» (об'єкт Guard), виділення
 * додатково рахуються окремим лічильником — і тестовий каркас вважає тест
 * проваленим, якщо цей лічильник ворухнувся.
 *
 * Що сторож не ловить: malloc() з нутрощів printf. Це навмисно — нас цікавить
 * наш код, а не буферизація stdio, яка в прошивці не використовується.
 */

#pragma once

namespace test_alloc {

// Усі виділення за прогін (для довідки у підсумку).
extern unsigned long g_allocTotal;

// Виділення, що трапились усередині захищеної зони. Має лишатись нулем.
extern unsigned long g_allocGuarded;

// Глибина вкладеності захищених зон.
extern int g_guardDepth;

inline unsigned long guardedAllocations()
{
  return g_allocGuarded;
}

inline unsigned long totalAllocations()
{
  return g_allocTotal;
}

// RAII-обгортка: захищена зона на час життя об'єкта.
struct Guard {
  Guard() { ++g_guardDepth; }
  ~Guard() { --g_guardDepth; }

  Guard(const Guard&) = delete;
  Guard& operator=(const Guard&) = delete;
};

}  // namespace test_alloc
