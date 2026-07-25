# Точки дотику з кодом EdgeTX

**Це закритий перелік.** Змінювати файли EdgeTX поза цим переліком не можна
без окремого ADR. Кожен зайвий рядок — це конфлікт при кожному `git rebase`
на нову версію EdgeTX.

Прив'язки перевірені по гілці `main` (стан: липень 2026). Номери рядків
орієнтовні — шукайте по назві функції.

---

## 1. Перелік режимів порту

**`radio/src/dataconstants.h`** (~рядок 249), `enum UartModes`

```cpp
enum UartModes {
  UART_MODE_NONE,
  ...
  UART_MODE_EXT_MODULE,
  UART_MODE_REMOTE_UI,        // ← ДОДАТИ ТУТ, перед COUNT
  UART_MODE_COUNT SKIP,
  UART_MODE_MAX SKIP = UART_MODE_COUNT-1
};
```

⚠️ Тільки **в кінець**, перед `UART_MODE_COUNT`. Вставка в середину зсуне
значення всіх наступних режимів.

Приємна деталь: у `radio.yml` режими зберігаються **рядками**, а не числами.
Тому навіть якщо upstream колись зсуне нумерацію — конфіги користувачів
не поламаються.

---

## 2. Назви для YAML

**`radio/src/storage/yaml/yaml_datastructs_funcs.cpp`** (~рядки 2647 і 2657)

Два окремі масиви — довгі й короткі назви:

```cpp
{  UART_MODE_REMOTE_UI, "MODE_REMOTE_UI"  },   // у першій таблиці
{  UART_MODE_REMOTE_UI, "REMOTE_UI"  },        // у другій
```

---

## 3. Прив'язка драйвера

**`radio/src/serial.cpp`**

`serialSetCallBacks()` (~рядок 176) — нова гілка:

```cpp
#if defined(REMOTE_UI)
  case UART_MODE_REMOTE_UI:
    remoteUiSetSerialDriver(ctx, drv);
    break;
#endif
```

`serialSetupPort()` (~рядок 284) — швидкість:

```cpp
#if defined(REMOTE_UI)
  case UART_MODE_REMOTE_UI:
    params.baudrate = REMOTE_UI_BAUDRATE;   // 2000000
    break;
#endif
```

---

## 4. Фільтр доступності режиму в меню

**`radio/src/gui/gui_common.cpp`** (~рядок 525), функція перевірки режимів

Дозволити режим лише на портах, які його тягнуть (AUX1 та VCP):

```cpp
#if defined(REMOTE_UI)
  if (mode == UART_MODE_REMOTE_UI && port_nr != SP_AUX1 && port_nr != SP_VCP)
    return false;
#endif
```

---

## 5. Захоплення екрана

**`radio/src/gui/colorlcd/lcd.cpp`**, функція `flushLcd()` (~рядок 72)

Один виклик **на самому початку функції**, до перевірки `direct_mode`:

```cpp
#if defined(REMOTE_UI)
  remoteUiNotifyFlush(area);   // тільки підказка, не обов'язкова
#endif
```

Основний механізм — порівняння буферів — викликається з фонової задачі
й у чужі файли не лізе взагалі.

---

## 6. Емуляція клавіш

**`radio/src/keys.cpp`**, функція `keysPollingCycle()` (~рядок 502)

```cpp
  uint32_t keys_input = readKeys();
#if defined(REMOTE_UI)
  keys_input |= remoteUiGetKeys();
  trims_input |= remoteUiGetTrims();     // після рядка з READ_TRIMS
#endif
```

Так ми отримуємо всю логіку EdgeTX безкоштовно: коротке й довге натискання,
автоповтор, блокування клавіш.

---

## 7. Емуляція енкодера й сенсора

**`radio/src/gui/colorlcd/LvglWrapper.cpp`**

У `rotaryDriverRead()` (~рядок 255):

```cpp
  rotenc_t newPos = rotaryEncoderGetValue();
#if defined(REMOTE_UI)
  newPos += remoteUiGetEncoderDelta();
#endif
```

У `touchDriverRead()` (~рядок 180): перед читанням фізичного сенсора
перевірити чергу віддалених подій:

```cpp
#if defined(REMOTE_UI)
  if (remoteUiPopTouch(data)) return;
#endif
```

⚠️ Зверніть увагу на умову `if (!isBacklightEnabled())` у цій функції —
вона глушить сенсор при вимкненій підсвітці. Для віддаленого вводу її треба
обійти, інакше застосунок не «розбудить» пульт.

---

## 8. Збірка

**`radio/src/CMakeLists.txt`** — одна умовна вставка:

```cmake
if(REMOTE_UI)
  add_subdirectory(remote_ui)
  add_definitions(-DREMOTE_UI)
endif()
```

**`radio/cmake/…`** — опція `option(REMOTE_UI "Remote UI over serial" OFF)`.

---

## 9. Переклади

Рядок назви режиму у `STR_AUX_SERIAL_MODES`. Мінімум — англійська.

---

## Підсумок

| Файл | Рядків змінено |
|---|---|
| `dataconstants.h` | 1 |
| `yaml_datastructs_funcs.cpp` | 2 |
| `serial.cpp` | 8 |
| `gui_common.cpp` | 4 |
| `lcd.cpp` | 3 |
| `keys.cpp` | 4 |
| `LvglWrapper.cpp` | 6 |
| `CMakeLists.txt` + опція | 5 |
| переклади | 1 |
| **Разом** | **~34 рядки в 9 файлах** |

Усе інше — нові файли в `radio/src/remote_ui/`, яких upstream не торкається.

## Як тримати це в порядку

```
commit A: нові файли        ← при rebase не конфліктує ніколи
commit B: ці 34 рядки       ← при rebase конфліктує рідко і дрібно
```

`/rebase-check` перевіряє гілку на свіжому upstream і повідомляє, якщо
щось із цього переліку зсунулось.
