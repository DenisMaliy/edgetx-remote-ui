# `remote_ui/` — код Remote UI для EdgeTX

Протокол, захоплення екрана, стиснення й емульований ввід.

Формат кадру описаний у [`docs/03-protocol.md`](../../../docs/03-protocol.md):

```
0xE7 0x7E  TYPE  LEN(2, LE)  PAYLOAD  CRC(2, LE)
```

CRC — CRC-16/CCITT-FALSE по `TYPE + LEN + PAYLOAD` (маркер і сам CRC у
розрахунок не входять).

## Файли

| Файл | Що в ньому |
|---|---|
| `remote_ui.h` | **єдине**, що бачить код EdgeTX: оголошення всіх гачків |
| `crc16.h` / `crc16.cpp` | CRC-16/CCITT-FALSE, побітово, без таблиці |
| `protocol.h` / `protocol.cpp` | константи кадру, коди пакетів, `encodeFrame()`, потоковий `Decoder` |
| `geometry.h` | роздільність (з `LCD_W`/`LCD_H`) і сітка плиток |
| `rle16.h` / `rle16.cpp` | стиснення RLE16 і зворотне перетворення для тестів |
| `tile.h` / `tile.cpp` | PAYLOAD пакета `TILE`, вибір «сире чи стиснуте» |
| `capture.h` / `capture.cpp` | тіньовий кадр, бітова карта брудних плиток, сам гачок |
| `input.h` / `input.cpp` | емульований ввід: клавіші, тримери, енкодер, сенсор, **тайм-аут відпускання**, повний стан (`0x87`) і розбір пакетів вводу |
| `input_edgetx.cpp` | прив'язка вводу до EdgeTX: годинник, `keysGetSupported()`, кількість тримерів |
| `hello.h` / `hello.cpp` | пакет `HELLO`: опис заліза, взятий з API EdgeTX |
| `transport_simu.cpp` | TCP замість UART — тільки для симулятора (`#if defined(SIMU)`) |
| `transport_uart.cpp` | транспорт на залізі: AUX1 через драйвер EdgeTX, DMA на передачу, **власна задача** (`#if !defined(SIMU)`) |
| `CMakeLists.txt` | дописує наші `.cpp` у список `SRC` EdgeTX |
| `test/` | свій каркас тестів і самі тести (у прошивку не потрапляють) |

## Як усе працює разом

```
LVGL -> flushLcd() -> remoteUiOnFlush()        задача menusTask, найнижчий пріоритет
                          |  memcpy у тіньовий кадр + біти «брудна»
                          v
                     бітова карта плиток
                          |
                          |  окремий потік (у симуляторі) або задача (на пульті)
                          v
              captureTakeTile -> RLE16 -> encodeFrame -> TCP/UART
```

⚠️ **На пульті транспорт крутить власна задача FreeRTOS з пріоритетом 2** —
на щабель вище за `menusTask` (1) і на два нижче за мікшер (4). Мікшер і звук
витісняють її завжди, тобто радіоканалу вона зачепити не може; малювання вона
витісняє сама, тому кадри не застигають у русі. Чому не службова задача
таймерів (там теж пріоритет 2) — на початку `transport_uart.cpp`: у ній
крутиться `per10ms()` разом з опитуванням клавіш і телеметрія раз на 2 мс.

Задача **нікого не чекає**: буфер передачі зайнятий — цього проходу нічого не
йде, плитки лишаються брудними. Це та сама плавна деградація, що в
`docs/02-architecture.md`.

⚠️ **Коли клієнту показувати кадр.** `FRAME_END` іде, коли від минулого
`FRAME_END` поїхала хоч одна плитка **і** або карта брудних плиток спорожніла,
або LVGL закрив свій кадр. Перша причина закриває кадр після `REFRESH` і після
великої перемальовки — вони не влазять в одну пачку й ідуть десятком проходів;
друга не дає картинці застигнути в русі, коли карта не порожніє ніколи.

Прив'язати закриття до самого `REFRESH` було б помилкою, і дорогою: кадр
закрився б **першою** пачкою, тобто раніше за плитки, які мав закрити, і
клієнт показав би десяту частину екрана. Правило однакове в обох транспортах —
розходитись у тому, коли показувати кадр, вони права не мають.

Ввід іде назустріч і в інший бік підмішується не в інтерфейс, а в **драйвери**:

```
TCP/UART -> Decoder -> applyInputPacket -> InputState (клавіші, тримери,
                                            енкодер, сенсор)
                            |
        keysPollingCycle() -+-> keys_input |= remoteUiGetKeys()      10 мс
        rotaryDriverRead() -+-> newPos     += remoteUiGetEncoderOffset()
                            +-> remoteUiAddEncoderDt(&rotencDt)
        touchDriverRead()  -+-> remoteUiPopTouch()                   LVGL
```

Тому довге натискання, автоповтор і прискорення енкодера рахує сам EdgeTX —
ми їх не програмуємо. Прискоренню, щоправда, треба підказати час: воно
рахується з проміжку між клацаннями, і без нашого `remoteUiAddEncoderDt()`
EdgeTX вважав би, що ручку крутять нескінченно швидко.

І тому ж **тиша в каналі відпускає все сама**: перевіряє це той бік, який ввід
читає, тож навіть мертвий потік транспорту не лишить клавішу натиснутою.
⚠️ Відсувають тайм-аут лише пакети, що **несуть ввід** — `KEY`, `ENC`,
`TOUCH`, `TRIM`, `INPUT_STATE`. `PING` і `REFRESH` його не відсувають: міст
ESP32 — власний посередник і може слати `PING` після того, як телефон
від'єднався.

Гачок не стискає й не передає нічого: він працює в задачі, яку мікшер витісняє
будь-коли. Черга — це бітова карта фіксованого розміру, тому переповнитись і
мовчки загубити зміну вона не може: плитка просто лишається позначеною
брудною.

Правила, яких код дотримується:

- **Нуль залежностей від EdgeTX** — жодного його заголовка, збирається
  звичайним `g++`.
- **Нуль динамічної пам'яті** — ні `new`, ні `malloc`, ні контейнерів.
  Уся пам'ять або статична, або приходить ззовні викликом.
- **Нуль специфіки пульта** — роздільність, клавіші й сенсор цього шару не
  стосуються, вони живуть у `PAYLOAD`.
- **Усе під `REMOTE_UI`** — без цього визначення `.cpp` дають порожні
  об'єктні файли (`.text`, `.data`, `.bss` — нулі).

Винятки з першого правила рівно два, обидва свідомі: `hello.cpp` питає в
EdgeTX опис заліза (`keysGetSupported`, `keysGetLabel`, `LCD_W/LCD_H`,
`FLAVOUR`, `VERSION`), а `geometry.h` бере з `board.h` роздільність. Це не
залежність від внутрішньої логіки, а те саме «нуль специфіки пульта в коді»:
числа питаються в EdgeTX, а не пишуться руками.

## Ціна на пульті

Числа зняті з карти пам'яті збірки `X10`/`RADIO_TX16S`, а не пораховані на
папері (задача 0009). Порівняння тієї самої збірки з `REMOTE_UI` і без:

| Пам'ять | без | з | різниця | вільно після |
|---|---|---|---|---|
| Флеш | 1558.09 КБ | 1562.20 КБ | **+4.11 КБ** | 485.80 КБ із 2048 |
| Внутрішня ОЗП (`.bss` + `.ram`) | 67 088 Б | 82 064 Б | **+14 976 Б** | 114 544 Б із 196 608 |
| CCM (`.data` + стеки) | 50 936 Б | 52 984 Б | **+2 048 Б** | 12 552 Б із 65 536 |
| SDRAM | 3 404 032 Б | 3 665 152 Б | **+261 120 Б** | ~4.5 МБ із 8 МБ |

Куди що лягло — за символами, а не за наміром:

| Що | Скільки | Де | Чому саме там |
|---|---|---|---|
| `s_shadow` — тіньовий кадр 480×272 | 261 120 Б | **SDRAM** `0xD02BF900` | у 192 КБ внутрішньої не влізло б узагалі |
| `s_txBuf` — буфер передачі | 4 103 Б | **`.ram`** `0x20011D5C` | ⚠️ між `_sram` і `_eram`, інакше DMA мовчки вимикається |
| `s_decoder` | 4 120 Б | `.bss` | 4096 із них — буфер найбільшого `PAYLOAD` |
| `s_payload` | 4 096 Б | `.bss` | |
| `s_tilePixels` | 2 048 Б | `.bss` | плитка 32×32 |
| `s_taskStack` | 2 048 Б | **CCM** | стек задачі транспорту, як у всіх задач EdgeTX |
| `s_helloPayload` | 333 Б | `.bss` | |
| `s_dirty` — бітова карта плиток | 20 Б | `.bss` | 135 плиток |

Усе статичне, жодного `malloc`.

⚠️ Найтісніше — **CCM**: 80.85% зайнято, 12.25 КБ вільно. Наш стек з'їв 2 КБ з
них. Це та пам'ять, куди EdgeTX кладе `.data` і стеки всіх задач.

## Як зібрати й прогнати тести

З кореня репозиторію, одна команда. Роздільність тестам задається прапорцями:
EdgeTX тут немає, а `geometry.h` без нього не знає розміру екрана.

```sh
cd firmware/edgetx-patch/remote_ui && mkdir -p ../../../build && \
g++ -std=c++17 -Wall -Wextra -fsanitize=address,undefined \
    -DREMOTE_UI -DREMOTE_UI_STANDALONE -DREMOTE_UI_LCD_W=480 -DREMOTE_UI_LCD_H=272 -I. \
    crc16.cpp protocol.cpp rle16.cpp tile.cpp capture.cpp input.cpp \
    test/alloc_guard.cpp test/test_crc16.cpp test/test_protocol.cpp \
    test/test_rle16.cpp test/test_capture.cpp test/test_input.cpp test/test_main.cpp \
    -o ../../../build/remote_ui-tests \
&& ../../../build/remote_ui-tests
```

Перевірка попереджень окремо — та сама команда без санітайзерів
(попереджень має бути **нуль**, компілятор має мовчати):

```sh
cd firmware/edgetx-patch/remote_ui && mkdir -p ../../../build && \
g++ -std=c++17 -Wall -Wextra \
    -DREMOTE_UI -DREMOTE_UI_STANDALONE -DREMOTE_UI_LCD_W=480 -DREMOTE_UI_LCD_H=272 -I. \
    crc16.cpp protocol.cpp rle16.cpp tile.cpp capture.cpp input.cpp \
    test/alloc_guard.cpp test/test_crc16.cpp test/test_protocol.cpp \
    test/test_rle16.cpp test/test_capture.cpp test/test_input.cpp test/test_main.cpp \
    -o ../../../build/remote_ui-tests-nosan
```

Стан на 2026-07-26 (g++ 16.1.1, `-fsanitize=address,undefined`):

- тестів — **96**, пройдено 96;
- ті самі 96 проходять на 480×272, 320×240, 800×480 і **212×64** — координати
  в тестах сенсора виводяться з `SCREEN_W`/`SCREEN_H`, а не пишуться числами,
  інакше на низькому екрані вони обрізались би по межі й тест падав би не
  через код;
- час прогону — **≈0.04 с**;
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
| `RleSolidBlockCollapses` | однотонна плитка 32×32 → 15 байтів замість 2048 |
| `RleCounterStopsAt255` | серія довша за 255 ріжеться на дві пари |
| `RleWithoutRepeatsGrows` | без повторів RLE **більший** за сире — і чесно про це каже |
| `RleRoundTripMixedRuns` | туди-назад на суміші смуг і шуму |
| `RleDecodeRejectsBrokenStream` | довжина не кратна 3, лічильник 0, переповнення |
| `TileChoosesRleForFlatArea`, `TileFallsBackToRawOnNoise` | **правило «стиснуте більше за сире → шлемо сире»** |
| `TileEdgeSizeIsHandled` | нижній ряд плиток заввишки 16, а не 32 |
| `CaptureGridMatchesScreen` | сітка рахується з `LCD_W`/`LCD_H`, а не з констант |
| `CaptureMarksOnlyTouchedTiles` | брудними стають рівно накриті плитки |
| `CaptureDeliversWhatWasFlushed`, `CapturePlacesPixelsAtRightOffset` | пікселі лягають туди, куди слід, із правильним кроком рядка |
| `CaptureClipsAreasOutsideScreen` | область за межами екрана обрізається, а не пише в чужу пам'ять |
| `CaptureKeepsTilesDirtyWhenTransportIsSlow` | **повільний транспорт не губить змін** |
| `CaptureDoesNotStarveOtherTiles` | одна плитка, що блимає щокадру, не заступає решту |
| `CaptureRefusesSmallDestination` | замалий буфер — відмова, і плитка при цьому не витрачається |
| `CaptureMarkAllDirtyRefusesBeforeFirstFlush` | **до першого флешу нічого не позначається брудним**: у SDRAM лежить сміття від попереднього вмикання, і віддати його клієнту як картинку не можна |
| ⚠️ `InputSilenceReleasesKeys`, `…Trims`, `…Touch` | **тиша в каналі відпускає все сама**, без жодного пакета |
| ⚠️ `InputDisconnectReleasesKeysAndTrims`, `…Touch`, `InputDisconnectBeatsLatch` | розрив відпускає все **негайно**, не чекаючи тайм-ауту |
| ⚠️ `InputReconnectReleasesTouchOfPreviousClient`, `InputReconnectDropsUnseenTouch` | новий клієнт не успадковує натиснуте попереднім |
| ⚠️ `InputStateDoesNotEatUnreadKeyLatch`, `…TrimLatch` | **повтор стану не з'їдає защіпнутого натискання**, якого читач ще не забрав |
| ⚠️ `InputStateDoesNotSwallowUnreadTouchDown` | те саме для сенсора: непрочитаний натиск не стирається рівнем |
| ⚠️ `InputStateReleasesTouchWhenProducerSeesTimeoutFirst`, `InputTouchUpReleasesWhenProducerSeesTimeoutFirst` | **писар побачив тайм-аут раніше за читача** — найгірший порядок подій; без епохи дотик залипав би назавжди |
| `InputStateTouchWorksAfterProducerTimeout` | і після того розчищення наступний дотик доходить як звичайний |
| `InputStateHealsStuckKey`, `InputStateHealsStuckTouch` | **втрачене «відпущено» лікується за один період** — те, чого тайм-аут не ловить |
| `InputStateRaisesMissedPress`, `InputStateSynthesisesTouchDown` | втрачене «натиснуто» рівень теж лікує |
| `InputStateIsIdempotent` | той самий пакет двічі не додає ні засувки, ні переходу — тому номера послідовності в ньому й немає |
| `InputStateReleaseUsesLastKnownPoint` | синтетичне відпускання бере останню відому точку, а не координати пакета |
| `InputStateLevelActsAsMove`, `InputStateLevelAddsNoExtraTransition` | рівень «унизу» лікує втрачений `MOVE` і при цьому переходом не стає |
| `InputStateLeavesEncoderAlone` | енкодера пакет не чіпає: рівень замість накопичення дав би фантомний оберт |
| ⚠️ `InputStateShortPayloadIsIgnored` | обрізаний пакет не застосовується **і тайм-аут не відсуває** |
| `InputStateLongPayloadAppliesHead` | пакет від новішого клієнта: перші 13 байтів беруться, хвіст ігнорується |
| ⚠️ `InputPingDoesNotHoldInput` | **PING ввід не тримає** — тайм-аут відсувають лише пакети, що несуть ввід |
| `InputStateKeepsKeyHeld` | а `INPUT_STATE` тримає: він і є годинником тайм-ауту |
| `InputEncoderReportsTimeBetweenClicks`, `InputEncoderDtSurvivesDisconnect` | час між клацаннями для прискорення ручки; перше клацання після паузи — чисте |
| `InputEncoderZeroStepStillProvesClientAlive` | `ENC:0` нічого не рухає, але доводить, що клієнт живий (на цьому тримається проба на точкову втрату) |
| `InputShortPressSurvivesBetweenPolls` | натискання коротше за 10 мс не губиться між опитуваннями клавіш |
| `InputIgnoresKeyOutsideMask` | код клавіші поза маскою відкидається, а не псує сусідні біти |
| `InputTouchTapIsNotLost`, `InputTouchHoldsPressBetweenPolls` | тик і довге натискання доходять обидва |
| `InputTouchIdleLeavesHardwareAlone` | **без віддаленого дотику сенсор лишається фізичним** |
| `InputTouchHistoryOverflowEndsReleased` | читач, що відстав, доганяє й не застрягає «натиснутим» |
| `InputEncoderAccumulates`, `InputEncoderSurvivesDisconnect` | енкодер віддає **положення**, а не приріст: фантомного оберту назад немає |
| `InputTimeWrapDoesNotRelease` | переповнення лічильника мілісекунд (раз на 49 днів) нічого не відпускає |

## Як це підключається до EdgeTX

Сам код у дерево EdgeTX не копіюється — туди веде symlink
`upstream/edgetx/radio/src/remote_ui`. Тобто примірник нашого коду один, і він
у git цього репозиторію.

```sh
tools/patch-apply.sh     # symlink + гачки з patches/hooks.patch
tools/patch-revert.sh    # назад; після нього дерево EdgeTX чисте
tools/patch-update.sh    # зняти правлені в upstream гачки назад у патч
```

У чужих файлах — **88 доданих рядків у дев'яти файлах**, усі під `REMOTE_UI`,
усі — вставки: вилучених і змінених рядків нуль. Перелік закритий, це всі
дев'ять пунктів [`docs/05-hooks.md`](../../../docs/05-hooks.md);
`patch-update.sh` не дасть знятися патчу, у якому є щось поза ним.

Що без прапорця нічого не змінилось — доведено найсильнішим доступним способом:
`firmware.bin`, зібраний із цього дерева з `REMOTE_UI=NO`, **побайтово
збігається** зі збіркою з чистого дерева EdgeTX (той самий `stamp.h`, щоб
позначка часу не заступила порівняння).
