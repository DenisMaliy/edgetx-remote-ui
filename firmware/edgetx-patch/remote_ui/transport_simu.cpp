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

#include "capture.h"
#include "geometry.h"
#include "hal/key_driver.h"  // MAX_KEYS — розмір буфера HELLO
#include "hello.h"
#include "input.h"
#include "os/time.h"  // time_get_ms — той самий годинник, що й у читача вводу
#include "protocol.h"
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
uint8_t s_helloPayload[helloMaxSize(MAX_KEYS)];

Decoder s_decoder;

// Обидва дескриптори атомарні, бо їх чіпають двоє: сам потік і деструктор при
// вивантаженні бібліотеки. `exchange(-1)` гарантує, що close() зробить рівно
// один із них — інакше номер, уже перевикористаний кимось іншим, закрили б
// удруге.
std::atomic<int> s_listenFd{-1};
std::atomic<int> s_clientFd{-1};

// Виставляється при вивантаженні бібліотеки — див. Stopper наприкінці файлу.
std::atomic<bool> s_stop{false};

// Прийшов REFRESH — кадр треба закрити FRAME_END, навіть якщо LVGL відтоді
// нічого не малював (docs/03-protocol.md, уточнення 2026-07-26). Прапорець
// ставить розбір пакета й знімає цикл передачі, обидва в цьому ж потоці, тому
// атомарність тут ні до чого.
bool s_refreshPending = false;

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

  // Будь-який пакет із правильним CRC доводить, що клієнт живий, — навіть
  // невідомий нам тип. Саме ця позначка тримає емульований ввід натиснутим:
  // щойно вона застаріє, читач відпустить усе сам (input.h).
  input.onAnyPacket(now);

  switch (type) {
    case PKT_KEY:
      if (length >= 2) {
        input.onKey(payload[0], payload[1] != 0, now);
      }
      break;

    case PKT_ENC:
      if (length >= 1) {
        input.onEncoder(static_cast<int8_t>(payload[0]), now);
      }
      break;

    case PKT_TOUCH:
      if (length >= 5) {
        const int16_t x =
            static_cast<int16_t>(payload[1] | (payload[2] << 8));
        const int16_t y =
            static_cast<int16_t>(payload[3] | (payload[4] << 8));
        input.onTouch(payload[0], x, y, now);
      }
      break;

    case PKT_TRIM:
      if (length >= 2) {
        input.onTrim(payload[0], payload[1] != 0, now);
      }
      break;

    case PKT_REFRESH:
      captureMarkAllDirty();
      // Кадр обов'язково закриється FRAME_END, навіть якщо LVGL відтоді нічого
      // не малював. Інакше клієнт, що під'єднався до нерухомого екрана, дістав
      // би всі плитки й не показав нічого — найтиповіший випадок у житті.
      s_refreshPending = true;
      break;

    case PKT_PING: {
      const size_t helloLen = buildHello(s_helloPayload, sizeof(s_helloPayload));
      const int fd = s_clientFd.load(std::memory_order_relaxed);
      if (helloLen > 0 && fd >= 0) {
        sendPacket(fd, PKT_HELLO, s_helloPayload, helloLen);
      }
      break;
    }

    // Невідомий тип — мовчки повз, як вимагає правило сумісності.
    default:
      break;
  }
}

// Обслуговує одного клієнта до розриву.
void serveClient(int fd)
{
  s_clientFd.store(fd, std::memory_order_relaxed);
  s_decoder.reset();
  s_refreshPending = false;

  // Новий клієнт не відповідає за те, що встиг натиснути попередній.
  inputState().onDisconnect();

  const size_t helloLen = buildHello(s_helloPayload, sizeof(s_helloPayload));
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

  // Новий клієнт не бачив нічого — віддаємо йому весь екран.
  captureMarkAllDirty();

  uint32_t lastFrameSent = captureFrameCount() - 1;
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
    const uint32_t framesBefore = captureFrameCount();

    // Запит на повний кадр знімається тут, до плиток: усе, що REFRESH позначив
    // брудним, піде саме цим проходом (карта вміщає TILE_COUNT плиток, і
    // стільки ж їх дозволено віддати за прохід), а FRAME_END закриє кадр нижче.
    const bool refreshRequested = s_refreshPending;
    s_refreshPending = false;

    int sentTiles = 0;
    TileRef tile;
    while (sentTiles < TILE_COUNT &&
           captureTakeTile(tile, s_tilePixels, TILE_MAX_PIXELS)) {
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

    // FRAME_END не чекає, доки карта спорожніє. Поки людина крутить меню,
    // брудні плитки з'являються швидше, ніж ідуть, і прив'язка «кінець кадру =
    // черга порожня» означала б застиглу картинку саме в русі: клієнт малює в
    // позаекранний буфер і показує його лише на FRAME_END.
    //
    // На REFRESH кадр закривається завжди, незалежно від лічильника: пульт,
    // що стоїть на нерухомому екрані, кадрів не породжує, а клієнт має
    // показати те, що йому щойно надіслали.
    if (framesBefore != lastFrameSent || refreshRequested) {
      if (!sendPacket(fd, PKT_FRAME_END, nullptr, 0)) {
        break;
      }
      lastFrameSent = framesBefore;
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

#endif  // REMOTE_UI && SIMU
