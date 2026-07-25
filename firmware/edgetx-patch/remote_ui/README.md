# `remote_ui/` — код Remote UI для EdgeTX

Нижній шар протоколу: перетворення потоку байтів на пакети й назад.
Ані картинки, ані стиснення, ані вводу тут немає — це наступні кроки плану.

Формат кадру описаний у [`docs/03-protocol.md`](../../../docs/03-protocol.md):

```
0xE7 0x7E  TYPE  LEN(2, LE)  PAYLOAD  CRC(2, LE)
```

CRC — CRC-16/CCITT-FALSE по `TYPE + LEN + PAYLOAD` (маркер і сам CRC у
розрахунок не входять).

## Файли

| Файл | Що в ньому |
|---|---|
| `crc16.h` / `crc16.cpp` | CRC-16/CCITT-FALSE, побітово, без таблиці |
| `protocol.h` / `protocol.cpp` | константи кадру, коди пакетів, `encodeFrame()`, потоковий `Decoder` |
| `CMakeLists.txt` | статична бібліотека для майбутнього `add_subdirectory(remote_ui)` |
| `test/` | свій каркас тестів і самі тести (у прошивку не потрапляють) |

Правила, яких код дотримується:

- **Нуль залежностей від EdgeTX** — жодного його заголовка, збирається
  звичайним `g++`.
- **Нуль динамічної пам'яті** — ні `new`, ні `malloc`, ні контейнерів.
  Уся пам'ять або статична, або приходить ззовні викликом.
- **Нуль специфіки пульта** — роздільність, клавіші й сенсор цього шару не
  стосуються, вони живуть у `PAYLOAD`.
- **Усе під `REMOTE_UI`** — без цього визначення обидва `.cpp` дають порожні
  об'єктні файли (`.text`, `.data`, `.bss` — нулі).

Ціна на пульті: `sizeof(remote_ui::Decoder)` = **4120 байт** ОЗП (з них 4096 —
буфер найбільшого `PAYLOAD`), найбільший кадр `MAX_FRAME_SIZE` = **4103 байти**.
Декодувальник заводиться один раз статично, а не на стеку.

## Як зібрати й прогнати тести

З кореня репозиторію, одна команда:

```sh
cd firmware/edgetx-patch/remote_ui && mkdir -p ../../../build && \
g++ -std=c++17 -Wall -Wextra -fsanitize=address,undefined -DREMOTE_UI -I. \
    crc16.cpp protocol.cpp \
    test/alloc_guard.cpp test/test_crc16.cpp test/test_protocol.cpp test/test_main.cpp \
    -o ../../../build/remote_ui-tests \
&& ../../../build/remote_ui-tests
```

Перевірка попереджень окремо — та сама команда без санітайзерів
(попереджень має бути **нуль**, компілятор має мовчати):

```sh
cd firmware/edgetx-patch/remote_ui && mkdir -p ../../../build && \
g++ -std=c++17 -Wall -Wextra -DREMOTE_UI -I. \
    crc16.cpp protocol.cpp \
    test/alloc_guard.cpp test/test_crc16.cpp test/test_protocol.cpp test/test_main.cpp \
    -o ../../../build/remote_ui-tests-nosan
```

Стан на 2026-07-26 (g++ 16.1.1, `-fsanitize=address,undefined`):

- тестів — **21**, пройдено 21;
- час прогону — **≈0.03 с** (збірка з нуля разом із прогоном — **0.94 с**);
- код повернення — **0** (успіх), **1** — якщо провалилась хоч одна перевірка;
- виділень динамічної пам'яті в захищених зонах — **1**, і це навмисне
  виділення в тесті `AllocGuardDetectsAllocation`, який доводить, що сторож
  не зламаний.

## Про тести

Каркас свій, `test/test_harness.h`, сто рядків на макросах: `TEST(Ім'я)`,
`CHECK`, `CHECK_EQ`, `CHECK_BYTES_EQ`. Зовнішніх залежностей немає навмисно —
цикл «змінив → перевірив» має бути секундним.

`test/alloc_guard.cpp` перевизначає глобальні `operator new` / `operator new[]`
(усі форми, включно з вирівняними) і рахує виділення. Каркас вмикає облік на
час кожного тесту й провалює тест, якщо пам'ять виділялась. Тобто нуль
динамічної пам'яті перевіряється не лише в одному місці, а в кожному тесті
кодування й декодування.

Що доводять тести:

| Тест | Властивість |
|---|---|
| `CrcKnownAnswer` | `"123456789"` → `0x29B1`, тобто це справді CCITT-FALSE |
| `ProtocolConstantsMatchSpec` | `MAX_PAYLOAD_SIZE`/`FRAME_OVERHEAD` прибиті до чисел зі специфікації, а не лишені символічними |
| `CrcEmptyInputIsSeed`, `CrcIncrementalMatchesBulk`, `CrcSeedContinuesStream` | побайтовий підрахунок (декодувальник) збігається з блоковим (кодувальник) |
| `EncodeGoldenFrame` | точний вигляд кадру на дроті, до байта |
| `EncodeRejectsBadArguments` | кодувальник не пише за межі буфера й відмовляє чесно |
| `RoundTrip*` | туди й назад: 0, 1 і 4096 байт |
| `UnknownPacketTypeDelivered` | невідомі типи не фільтруються (правило сумісності) |
| `ByteByByteFeeding` | кадр по одному байту за виклик |
| `SplitAtEveryOffset` | розрив у **кожній** можливій позиції |
| `MarkerInsidePayload` | `E7 7E` у даних нічого не ламає |
| `GarbageBeforeFrame` | сміття перед кадром, зокрема `E7 E7 7E` |
| `CorruptedCrcRecovers` | битий пакет викидається, наступний **розбирається** |
| `TruncatedFrameDoesNotHang` | обіцяно 4096, прийшло 10 — не зависає й не виходить за буфер |
| `OversizedLenRejectedImmediately` | `LEN` понад стелю → ресинхронізація одразу за `LEN` |
| `RandomGarbageDoesNotCrash` | 1 МБ псевдовипадкових байтів, зерно 12345 |
| `NoHeapAllocations`, `AllocGuardDetectsAllocation` | нуль динамічної пам'яті і доказ, що сторож працює |

## Як це підключається до EdgeTX

Поки що ніяк — це «коміт A» за правилом двох комітів (`CLAUDE.md`): тільки
нові файли, жодного рядка в чужих файлах. `CMakeLists.txt` уже готовий до
`add_subdirectory(remote_ui)`, але сама вставка — пункт 8 з
[`docs/05-hooks.md`](../../../docs/05-hooks.md) і окрема задача.
