/*
 * Сторож динамічної пам'яті — перевизначення глобальних operator new/delete.
 *
 * Тільки для тестового бінарника на комп'ютері.
 */

#include "alloc_guard.h"

#include <stdlib.h>

#include <new>

namespace test_alloc {

unsigned long g_allocTotal = 0;
unsigned long g_allocGuarded = 0;
int g_guardDepth = 0;

// Спільний облік для всіх форм operator new.
static void countAllocation()
{
  ++g_allocTotal;
  if (g_guardDepth > 0) {
    ++g_allocGuarded;
  }
}

}  // namespace test_alloc

// --- Звичайні форми -------------------------------------------------------

void* operator new(std::size_t size)
{
  test_alloc::countAllocation();
  void* p = malloc(size != 0 ? size : 1);
  if (p == nullptr) {
    throw std::bad_alloc();
  }
  return p;
}

void* operator new[](std::size_t size)
{
  return ::operator new(size);
}

void* operator new(std::size_t size, const std::nothrow_t&) noexcept
{
  test_alloc::countAllocation();
  return malloc(size != 0 ? size : 1);
}

void* operator new[](std::size_t size, const std::nothrow_t& tag) noexcept
{
  return ::operator new(size, tag);
}

void operator delete(void* p) noexcept
{
  free(p);
}

void operator delete[](void* p) noexcept
{
  free(p);
}

void operator delete(void* p, std::size_t) noexcept
{
  free(p);
}

void operator delete[](void* p, std::size_t) noexcept
{
  free(p);
}

void operator delete(void* p, const std::nothrow_t&) noexcept
{
  free(p);
}

void operator delete[](void* p, const std::nothrow_t&) noexcept
{
  free(p);
}

// --- Форми з вирівнюванням (C++17) ----------------------------------------
//
// Наш код їх не вживає, але без них лишалася б лазівка повз лічильник.

void* operator new(std::size_t size, std::align_val_t alignment)
{
  test_alloc::countAllocation();

  const std::size_t align = static_cast<std::size_t>(alignment);
  void* p = nullptr;
  if (posix_memalign(&p, align, size != 0 ? size : align) != 0) {
    throw std::bad_alloc();
  }
  return p;
}

void* operator new[](std::size_t size, std::align_val_t alignment)
{
  return ::operator new(size, alignment);
}

void operator delete(void* p, std::align_val_t) noexcept
{
  free(p);
}

void operator delete[](void* p, std::align_val_t) noexcept
{
  free(p);
}

void operator delete(void* p, std::size_t, std::align_val_t) noexcept
{
  free(p);
}

void operator delete[](void* p, std::size_t, std::align_val_t) noexcept
{
  free(p);
}
