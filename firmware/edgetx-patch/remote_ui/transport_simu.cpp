/*
 * TX16S Remote UI — транспорт для симулятора: TCP замість UART.
 *
 * Ліцензія: GPLv2 (та сама, що в EdgeTX).
 *
 * На залізі протокол поїде в AUX1 на 921600 бод (етап 2). У симуляторі UART
 * узяти нізвідки, тому той самий байтовий потік віддається в TCP-сокет:
 * протокол і його межі не змінюються, змінюється лише труба.
 *
 * Слухаємо **тільки 127.0.0.1** — це інструмент розробки на своїй машині, а
 * не мережева служба.
 *
 * Порт: 7616 (за замовчуванням), змінна середовища REMOTE_UI_PORT перекриває.
 *
 * Уся робота — у власному потоці. Гачок (menusTask) не робить нічого, крім
 * копіювання й позначення плиток, тому затримка мережі не може загальмувати
 * інтерфейс пульта: у найгіршому разі гальмує цей потік, а плитки лишаються
 * позначеними брудними й підуть пізніше.
 */

#if defined(REMOTE_UI) && defined(SIMU)

#include <atomic>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "baudrate.h"
#include "capture.h"
#include "geometry.h"
#include "hal/key_driver.h"  // MAX_KEYS — розмір буфера HELLO
#include "hello.h"
#include "input.h"
#include "os/time.h"  // time_get_ms — той самий годинник, що й у читача вводу
#include "protocol.h"
#include "remote_ui.h"  // etx_serial_driver_t для гачка 3
#include "tile.h"

namespace remote_ui {

namespace {

constexpr uint16_t DEFAULT_PORT = 7616;

// Скид декодувальника за тишею в каналі. Шар кадрування часу не має
// (docs/03-protocol.md), тому обірваний посеред кадру потік розсинхронізує
// приймач назавжди — доки транспорт не скине його сам.
//
// 100 мс обрано так: найбільший кадр (4103 байти) на 921600 бод іде ~45 мс,
// тобто вдвічі менше. Значення спільне для TCP і для майбутнього UART: воно
// про протокол, а не про трубу.
constexpr uint32_t SILENCE_RESET_MS = 100;

// Пауза, коли слати нема чого. 2 мс — це ~500 проходів на секунду, дрібниця
// проти 60 кадрів, і водночас не крутить процесор даремно.
constexpr uint32_t IDLE_SLEEP_US = 2000;

// Пауза перед повторною спробою запису, коли сокет не приймає байтів. Ціна
// однієї ітерації, зате цикл не з'їдає ядро на неблокувальному сокеті.
constexpr uint32_t WRITE_RETRY_SLEEP_US = 500;

// Скільки байтів забирати з сокета за раз.
constexpr size_t RX_CHUNK = 512;

uint8_t s_payload[MAX_PAYLOAD_SIZE];
uint8_t s_frame[MAX_FRAME_SIZE];
uint16_t s_tilePixels[TILE_MAX_PIXELS];

// HELLO складається в обробнику прийнятого пакета, тобто посеред циклу, який
// саме пише плитки в s_payload. Спільний буфер тут працював би рівно доти,
// доки прийом і передача лишаються строго послідовними — тобто до першої ж
// зміни в циклі. Дешевше дати HELLO власне місце.
//
// Розмір рахується з MAX_KEYS, а не береться зі стелі: коли upstream додасть
// клавіш, буфер виросте сам, а не почне мовчки не вміщати пакет.
uint8_t s_helloPayload[helloMaxSize(MAX_KEYS, BAUD_ALLOWED_COUNT)];

Decoder s_decoder;

// Обидва дескриптори атомарні, бо їх чіпають двоє: сам потік і деструктор при
// вивантаженні бібліотеки. `exchange(-1)` гарантує, що close() зробить рівно
// один із них — інакше номер, уже перевикористаний кимось іншим, закрили б
// удруге.
std::atomic<int> s_listenFd{-1};
std::atomic<int> s_clientFd{-1};

// Виставляється при вивантаженні бібліотеки — див. Stopper наприкінці файлу.
std::atomic<bool> s_stop{false};

// Скільки плиток пішло від останнього FRAME_END. Правило закриття кадру —
// спільне з transport_uart.cpp, пояснення там же; коротко: закриваємо, коли
// карта брудних плиток спорожніла або LVGL закрив свій кадр, і лише якщо від
// минулого FRAME_END справді щось поїхало.
//
// Живе в цьому ж потоці, тому атомарність тут ні до чого.
uint32_t s_tilesSinceFrameEnd = 0;

// REFRESH прийшов, коли тіньового кадру ще не було. Повторюємо, доки не
// застосується: протокол обіцяє, що REFRESH завершується FRAME_END.
bool s_refreshDeferred = false;

// Час береться з EdgeTX, а не з clock_gettime, і це не дрібниця: тайм-аут
// відпускання вводу порівнює позначку, поставлену **тут**, із часом, який
// читає задача пульта (input_edgetx.cpp). Два різні годинники дали б різницю,
// що не означає нічого.
uint32_t nowMs() { return time_get_ms(); }

// Пише все або повідомляє про розрив. Запис блокувальний навмисно: коли
// клієнт не встигає читати, гальмувати має цей потік, а не пульт.
bool writeAll(int fd, const uint8_t* data, size_t len)
{
  size_t sent = 0;
  while (sent < len) {
    const ssize_t n = send(fd, data + sent, len - sent, MSG_NOSIGNAL);
    if (n > 0) {
      sent += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
      if (errno != EINTR) {
        // Сокет блокувальний, тому сюди не дійде; але варто комусь зробити
        // його неблокувальним — і цикл без паузи з'їв би ядро цілком. Коротка
        // пауза коштує затримки в одну ітерацію й рятує від цього назавжди.
        usleep(WRITE_RETRY_SLEEP_US);
      }
      continue;
    }
    return false;
  }
  return true;
}

bool sendPacket(int fd, uint8_t type, const uint8_t* payload, size_t len)
{
  const size_t frameLen = encodeFrame(type, payload, len, s_frame, sizeof(s_frame));
  if (frameLen == 0) {
    return false;  // не мало статись: розміри перевірені static_assert-ами
  }
  return writeAll(fd, s_frame, frameLen);
}

void onPacket(uint8_t type, const uint8_t* payload, size_t length, void* context)
{
  (void)context;

  const uint32_t now = nowMs();
  InputState& input = inputState();

  // ⚠️ Позначку «клієнт живий» ставлять **тільки** пакети, що несуть ввід, і
  // ставить її сам розбір усередині applyInputPacket. Раніше вона стояла тут,
  // до switch, тобто тайм-аут відсувало будь-що — включно з PING, REFRESH і
  // невідомими типами.
  //
  // Чому це змінено (docs/03-protocol.md, «Тайм-аут відпускання»): на етапі 2
  // між телефоном і пультом стоїть міст ESP32. Він власний посередник і цілком
  // може слати PING після того, як телефон від'єднався. При старому правилі
  // клавіша, утримувана в мить розриву Wi-Fi, лишилась би натиснутою назавжди —
  // і людина цього не побачила б, бо екрана немає.
  if (applyInputPacket(input, type, payload, length, now)) {
    return;  // ввід застосовано; відповіді такі пакети не породжують
  }

  switch (type) {
    case PKT_REFRESH:
      // Кадр закриється FRAME_END сам, коли всі позначені плитки поїдуть, — за
      // правилом «карта спорожніла» в циклі передачі. Відмова буває одна:
      // тіньового кадру ще немає; тоді запит відкладається, а не зникає.
      if (!captureMarkAllDirty()) {
        s_refreshDeferred = true;
      }
      break;

    case PKT_PING: {
      const size_t helloLen = buildHello(s_helloPayload, sizeof(s_helloPayload), 0);
      const int fd = s_clientFd.load(std::memory_order_relaxed);
      if (helloLen > 0 && fd >= 0) {
        sendPacket(fd, PKT_HELLO, s_helloPayload, helloLen);
      }
      break;
    }

    case PKT_BAUD_SET: {
      // ⚠️ У TCP поняття «швидкість каналу» беззмістовне, і відповісти на це
      // треба **вголос**, а не мовчазним ігноруванням.
      //
      // Мовчання формально дозволене правилом сумісності, але тут воно
      // неправильне по суті: інструмент, що попросив перемикання, не відрізнив
      // би «транспорт цього не вміє» від «пульт помер». Перше — нормальна
      // відповідь, друге — привід бігти до стенда.
      //
      // Поточна швидкість передається нулем: це і є «не застосовне», і
      // сплутати його з дійсною швидкістю неможливо — нуля в переліку немає й
      // бути не може.
      uint32_t want = 0;
      uint8_t nonce = 0;
      if (!parseBaudSet(payload, length, want, nonce)) {
        break;
      }

      uint8_t report[BAUD_REPORT_SIZE];
      const size_t len = buildBaudReport(report, sizeof(report),
                                         BAUD_NOT_APPLICABLE, nonce, 0, 0,
                                         BAUD_SWITCH_DELAY_MS,
                                         BAUD_REVERT_WINDOW_MS, 0);
      const int fd = s_clientFd.load(std::memory_order_relaxed);
      if (len > 0 && fd >= 0) {
        sendPacket(fd, PKT_BAUD, report, len);
      }
      break;
    }

    // Невідомий тип — мовчки повз, як вимагає правило сумісності. Сюди ж
    // потрапляє й пакет вводу з обрізаним вантажем: applyInputPacket відмовив,
    // а робити з ним більше нема чого.
    default:
      break;
  }
}

// Обслуговує одного клієнта до розриву.
void serveClient(int fd)
{
  s_clientFd.store(fd, std::memory_order_relaxed);
  s_decoder.reset();
  s_tilesSinceFrameEnd = 0;
  s_refreshDeferred = false;

  // Новий клієнт не відповідає за те, що встиг натиснути попередній.
  inputState().onDisconnect();

  const size_t helloLen = buildHello(s_helloPayload, sizeof(s_helloPayload), 0);
  if (helloLen == 0) {
    fprintf(stderr, "Remote UI: HELLO не вліз у %zu байтів — клієнта відпускаю\n",
            sizeof(s_helloPayload));
    s_clientFd.store(-1, std::memory_order_relaxed);
    return;
  }

  if (!sendPacket(fd, PKT_HELLO, s_helloPayload, helloLen)) {
    s_clientFd.store(-1, std::memory_order_relaxed);
    return;
  }

  // Новий клієнт не бачив нічого — віддаємо йому весь екран. Якщо тіньового
  // кадру ще немає, запит відкладається до першого флешу.
  if (!captureMarkAllDirty()) {
    s_refreshDeferred = true;
  }

  // Без «мінус один»: перший же прохід віддасть весь екран, спорожнить карту й
  // тим закриє кадр. Раніше різниця в одиницю була потрібна саме для цього
  // першого FRAME_END; тепер його дає правило «карта спорожніла».
  uint32_t lastFrameSent = captureFrameCount();
  uint32_t lastRxMs = nowMs();
  bool alive = true;

  while (alive && !s_stop.load(std::memory_order_relaxed)) {
    // --- прийом ---
    pollfd pfd = {fd, POLLIN, 0};
    if (poll(&pfd, 1, 0) > 0 && (pfd.revents & (POLLIN | POLLHUP | POLLERR))) {
      uint8_t rx[RX_CHUNK];
      const ssize_t n = recv(fd, rx, sizeof(rx), 0);
      if (n == 0) {
        break;  // клієнт відключився
      }
      if (n < 0) {
        if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) {
          break;
        }
      } else {
        s_decoder.feed(rx, static_cast<size_t>(n), onPacket, nullptr);
        lastRxMs = nowMs();
      }
    } else if (nowMs() - lastRxMs > SILENCE_RESET_MS) {
      // Тиша: якщо декодувальник завис посеред обірваного кадру, звільняємо
      // його. Коли він і так на початку, скид нічого не змінює.
      s_decoder.reset();
      lastRxMs = nowMs();
    }

    // --- передача ---
    //
    // Лічильник кадрів знімається **до** порції плиток: інакше кадр, який
    // завершився вже під час відправлення, порахувався б відправленим, а
    // частина його плиток лишилася б у карті до наступної зміни екрана.
    // Відкладений REFRESH: щойно тіньовий кадр з'явився, застосовуємо його.
    if (s_refreshDeferred && captureMarkAllDirty()) {
      s_refreshDeferred = false;
    }

    const uint32_t framesBefore = captureFrameCount();

    // `drained` — чи карта брудних плиток спорожніла саме цим проходом.
    bool drained = false;

    int sentTiles = 0;
    TileRef tile;
    while (sentTiles < TILE_COUNT) {
      if (!captureTakeTile(tile, s_tilePixels, TILE_MAX_PIXELS)) {
        drained = true;
        break;
      }

      const size_t payloadLen =
          encodeTilePayload(tile, s_tilePixels, s_payload, sizeof(s_payload));

      if (payloadLen == 0) {
        // Скласти пакет не вийшло — це не розрив, а наша помилка. Плитку
        // повертаємо в брудні, інакше зміна зникне тихо.
        captureReturnTile(tile);
        fprintf(stderr, "Remote UI: плитка %ux%u не склалась у пакет\n", tile.w,
                tile.h);
        break;
      }

      if (!sendPacket(fd, PKT_TILE, s_payload, payloadLen)) {
        captureReturnTile(tile);
        alive = false;
        break;
      }

      ++sentTiles;
    }

    if (!alive) {
      break;
    }

    // Цикл міг вийти й за лічильником — тоді карта вже порожня, але `drained`
    // цього не побачив, і кадр закрився б на прохід пізніше.
    //
    // Карта читається один раз на прохід — те саме число вирішує «цілісний» і
    // їде клієнту. Правило дослівно те саме, що в transport_uart.cpp.
    const uint32_t dirtyTiles = captureDirtyCount();
    if (!drained && dirtyTiles == 0) {
      drained = true;
    }

    s_tilesSinceFrameEnd += static_cast<uint32_t>(sentTiles);

    // ⚠️ Дві причини закрити кадр, і потрібна будь-яка з них — але тільки якщо
    // від минулого FRAME_END справді щось поїхало.
    //
    // 1. **Карта спорожніла**: усе, що ми знали про зміни, лежить на дроті.
    //    Саме ця причина закриває кадр після REFRESH — на нерухомому екрані
    //    лічильник кадрів не рухається взагалі.
    // 2. **LVGL закрив кадр**: причина «в русі». Поки людина крутить меню,
    //    брудні плитки з'являються швидше, ніж ідуть, карта не порожніє
    //    ніколи, і без цієї гілки картинка застигла б саме тоді, коли має
    //    рухатись.
    //
    // 3. **Плиток пішло на цілий екран** — запобіжник від клієнта, що шле
    //    REFRESH частіше, ніж карта порожніє.
    //
    // Правило дослівно те саме, що в transport_uart.cpp: транспорти не мають
    // права розходитись ані в тому, **коли** клієнту показувати кадр, ані в
    // тому, **що** вони при цьому кажуть про його цілісність. Причини 2 і 3
    // закривають кадр із неспорожнілою картою — `dirtyTiles` у вантажі й каже
    // клієнту, скільки плиток ще в дорозі (docs/03-protocol.md, 0x03).
    if (s_tilesSinceFrameEnd > 0 &&
        (drained || framesBefore != lastFrameSent ||
         s_tilesSinceFrameEnd >= static_cast<uint32_t>(TILE_COUNT))) {
      uint8_t frameEnd[FRAME_END_PAYLOAD_SIZE];
      buildFrameEnd(frameEnd, dirtyTiles);
      if (!sendPacket(fd, PKT_FRAME_END, frameEnd, sizeof(frameEnd))) {
        break;
      }
      lastFrameSent = framesBefore;
      s_tilesSinceFrameEnd = 0;
    }

    if (sentTiles == 0) {
      usleep(IDLE_SLEEP_US);
    }
  }

  // ⚠️ Найважливіші два рядки файлу. Клієнта більше немає — усе, що він
  // тримав натиснутим, відпускається негайно, а не за тайм-аутом. Тайм-аут
  // лишається другим рубежем: він спрацює навіть тоді, коли цей потік загине
  // й до цього рядка не дійде.
  inputState().onDisconnect();

  s_clientFd.store(-1, std::memory_order_relaxed);
}

void* threadMain(void*)
{
  uint16_t port = DEFAULT_PORT;
  if (const char* env = getenv("REMOTE_UI_PORT")) {
    const int parsed = atoi(env);
    if (parsed > 0 && parsed < 65536) {
      port = static_cast<uint16_t>(parsed);
    }
  }

  const int listenFd = socket(AF_INET, SOCK_STREAM, 0);
  if (listenFd < 0) {
    fprintf(stderr, "Remote UI: не вдалось створити сокет (%s)\n", strerror(errno));
    return nullptr;
  }

  s_listenFd.store(listenFd, std::memory_order_relaxed);

  const int one = 1;
  setsockopt(listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

  sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(port);

  if (bind(listenFd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0 ||
      listen(listenFd, 1) < 0) {
    fprintf(stderr, "Remote UI: порт %u зайнятий (%s)\n", port, strerror(errno));
    // Через exchange, а не store+close: деструктор міг устигнути забрати цей
    // самий дескриптор і закрити його, і тоді другий close дістався б чужому
    // файлу, який уже отримав цей номер.
    const int mine = s_listenFd.exchange(-1, std::memory_order_relaxed);
    if (mine >= 0) {
      close(mine);
    }
    return nullptr;
  }

  fprintf(stderr, "Remote UI: слухаю 127.0.0.1:%u, екран %dx%d, плиток %d\n",
          port, SCREEN_W, SCREEN_H, TILE_COUNT);

  while (!s_stop.load(std::memory_order_relaxed)) {
    const int fd = accept(listenFd, nullptr, nullptr);
    if (fd < 0) {
      if (errno == EINTR && !s_stop.load(std::memory_order_relaxed)) {
        continue;
      }
      break;  // сокет закрито при зупинці — виходимо
    }

    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    fprintf(stderr, "Remote UI: клієнт підключився\n");

    serveClient(fd);

    close(fd);
    fprintf(stderr, "Remote UI: клієнт відключився\n");
  }

  const int leftover = s_listenFd.exchange(-1, std::memory_order_relaxed);
  if (leftover >= 0) {
    close(leftover);
  }
  return nullptr;
}

pthread_t s_thread;
bool s_threadStarted = false;

// Потік піднімається сам, при завантаженні бібліотеки симулятора. Це навмисно:
// окремий виклик ініціалізації означав би ще один гачок у чужому коді, а
// перелік дозволених місць закритий (docs/05-hooks.md).
//
// Потік навмисно **не** від'єднується: його треба буде дочекатися при
// вивантаженні — див. нижче.
struct Starter {
  Starter()
  {
    s_threadStarted = (pthread_create(&s_thread, nullptr, threadMain, nullptr) == 0);
  }
};

Starter s_starter;

// Симулятор EdgeTX буває не тільки окремим виконуваним файлом: Companion
// вантажить `libedgetx-<ціль>-simulator.so` і потім вивантажує її
// (`radio/src/targets/simu/CMakeLists.txt`). Потік, який після dlclose ще
// живий, виконував би код у розмапленій пам'яті.
//
// Тому зупинка робиться до кінця, а не наполовину:
//   1. прапорець зупинки — цикли його бачать;
//   2. закриття слухаючого сокета — accept() виходить з помилкою;
//   3. shutdown клієнтського сокета — інакше потік міг би вічно висіти в
//      блокувальному send() на клієнті, який перестав читати, і жоден
//      прапорець його б звідти не дістав;
//   4. pthread_join — і лише після нього повернення з деструктора.
__attribute__((destructor)) void remoteUiTransportStop()
{
  s_stop.store(true, std::memory_order_relaxed);

  const int listenFd = s_listenFd.exchange(-1, std::memory_order_relaxed);
  if (listenFd >= 0) {
    shutdown(listenFd, SHUT_RDWR);
    close(listenFd);
  }

  // Клієнтський дескриптор не закриваємо — його закриє сам потік; нам треба
  // лише розбудити його з блокувального виклику.
  const int clientFd = s_clientFd.load(std::memory_order_relaxed);
  if (clientFd >= 0) {
    shutdown(clientFd, SHUT_RDWR);
  }

  if (s_threadStarted) {
    pthread_join(s_thread, nullptr);
    s_threadStarted = false;
  }
}

}  // namespace

}  // namespace remote_ui

// --- Гачок 3 в симуляторі ---------------------------------------------------
//
// Порту AUX1 тут не існує: транспорт їде в TCP і піднімається сам. Але
// посилання на цю функцію є в `serial.cpp`, тобто в обох збірках, — тож без неї
// симулятор просто не злінкувався б.
//
// Це не заглушка «щоб компілювалось». Завдяки їй режим порту **видно в
// симуляторі**: його можна вибрати, зберегти й перевірити, що в `radio.yml`
// з'явився рядок `mode: REMOTE_UI` і що він читається назад. Тобто вся
// YAML-частина гачків 1 і 2 перевіряється без пульта.
void remoteUiSetSerialDriver(void* ctx, const etx_serial_driver_t* drv,
                             const etx_serial_port_t* port)
{
  (void)drv;
  (void)port;

  fprintf(stderr,
          "Remote UI: порт переведено в режим Remote UI (%s). У симуляторі це "
          "нічого не змінює — картинка й ввід ідуть у TCP.\n",
          (ctx != nullptr) ? "увімкнено" : "вимкнено");
}

#endif  // REMOTE_UI && SIMU
