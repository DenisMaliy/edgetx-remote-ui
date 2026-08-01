'use strict';
/*
 * Протокол Remote UI на боці браузера: кадрування, CRC, HELLO, RLE16, ввід.
 *
 * Третій примірник протоколу. Перший — C++ у прошивці пульта
 * (`firmware/edgetx-patch/remote_ui/`), другий — Python
 * (`tools/remote_ui_proto.py`), третій цей. Саме те, що три незалежні
 * реалізації сходяться **на однакових байтах**, і робить
 * `docs/03-protocol.md` специфікацією, а не переказом коду.
 *
 * Тому файл навмисно не знає ні про DOM, ні про WebSocket: його прогоняє
 * `proto_test.js` під node тими самими еталонними векторами, що й
 * `tools/proto_test.py`. Оформлення живе в `app.js`.
 */

(function (root) {

// ------------------------------------------------------------- константи ---

const MARKER0 = 0xE7;
const MARKER1 = 0x7E;

const PKT_HELLO = 0x01;
const PKT_TILE = 0x02;
const PKT_FRAME_END = 0x03;
const PKT_STATE = 0x04;
const PKT_LOG = 0x05;
const PKT_BAUD = 0x06;

const PKT_KEY = 0x81;
const PKT_ENC = 0x82;
const PKT_TOUCH = 0x83;
const PKT_REFRESH = 0x84;
const PKT_TRIM = 0x85;
const PKT_PING = 0x86;
const PKT_INPUT_STATE = 0x87;
const PKT_BAUD_SET = 0x88;

const TILE_RAW = 0;
const TILE_RLE16 = 1;

const TOUCH_DOWN = 0;
const TOUCH_MOVE = 1;
const TOUCH_UP = 2;

const HELLO_FLAG_TOUCH = 0x01;
const HELLO_FLAG_ENCODER = 0x02;
const HELLO_FLAG_FILE_OPS = 0x04;
const HELLO_FLAG_INPUT_STATE = 0x08;

const MAX_PAYLOAD = 4096;
const FRAME_OVERHEAD = 7;

/* Періодичності — з docs/03-protocol.md. Міняються лише разом із тайм-аутом
 * відпускання в прошивці (1000 мс). */
const PING_PERIOD_MS = 2000;
const PING_TIMEOUT_MS = 5000;
const INPUT_STATE_PERIOD_MS = 250;

/* Запасний шлях для прошивки без біта3: утримання тримається на PING, і лише
 * поки щось справді утримується. */
const LEGACY_HOLD_PERIOD_MS = 250;

/* --- скільки чекати доїзду решти плиток (docs/03-protocol.md, 0x03) --------
 *
 * `FRAME_END` каже, скільки плиток лишилось незасланими. При N > 0 картинка в
 * буфері зшита щонайменше з двох митей — це і є розлам, який людина бачить на
 * гортанні. Чекаємо доїзду, але зі строком: плитки можуть не доїхати взагалі.
 *
 * T_wait(N) = BASE + PER_TILE × N, але не більше MAX від **початку поточної
 * низки очікування** (див. `waitingSince` в `app.js` — там же й пояснення,
 * чому не від останнього показаного кадру: після паузи на нерухомому екрані
 * запас був би вже вичерпаний, і перше ж гортання показалось би без
 * очікування, тобто головний випадок задачі лишився б не покритим).
 *
 * PER_TILE = 3 мс — одна плитка наскрізно в найгіршому **заміряному** стані
 * каналу: 400 Б (важкий замір 392 Б, округлено вгору) при 123 КБ/с до
 * телефона, тобто 3.18 мс. Беремо гірше з баченого навмисно: показати зарано
 * = той самий розлам, заради якого все це робиться; показати запізно = кілька
 * зайвих мілісекунд у кадрі, який однаково чекав.
 *
 * BASE = 50 мс — те, що від N не залежить: пачка, вже віддана DMA (4103 Б на
 * 2 625 000 = 15.6 мс), прохід транспорту (2 мс), пачка моста (1805 Б ≈ 7 мс),
 * відправлення у Wi-Fi (≈5 мс), цикл подій браузера (≈16 мс).
 *
 * MAX = 250 мс ≈ 2.5 повних кадри дроту. До N ≈ 67 строк накриває навіть
 * найгірший бачений стан каналу — тобто спрацьовує лише при справжній втраті,
 * а не через нетерплячість. Понад 67 плиток залишку буває при зміні сторінки
 * цілком, де застигла картинка помітніша за один зшитий кадр, і там стеля
 * обрізає очікування свідомо.
 *
 * ⚠️ Числа початкові й підлягають замірам. Вони живуть тут, в одному місці,
 * саме тому, що людина цілком може попросити «чекай менше».
 */
const FRAME_WAIT_BASE_MS = 50;
const FRAME_WAIT_PER_TILE_MS = 3;
const FRAME_WAIT_MAX_MS = 250;

function frameWaitMs(dirtyTiles) {
  return Math.min(FRAME_WAIT_MAX_MS,
                  FRAME_WAIT_BASE_MS + FRAME_WAIT_PER_TILE_MS * dirtyTiles);
}

// ------------------------------------------------------------------ CRC ----

/** Таблиця CRC-16/CCITT-FALSE: поліном 0x1021, початок 0xFFFF, без рефлексії. */
const CRC_TABLE = (function () {
  const t = new Uint16Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i << 8;
    for (let j = 0; j < 8; j++) {
      c = (c & 0x8000) ? ((c << 1) ^ 0x1021) & 0xFFFF : (c << 1) & 0xFFFF;
    }
    t[i] = c;
  }
  return t;
})();

function crc16(buf, off, len) {
  let crc = 0xFFFF;
  const end = off + len;
  for (let i = off; i < end; i++) {
    crc = ((crc << 8) ^ CRC_TABLE[((crc >> 8) ^ buf[i]) & 0xFF]) & 0xFFFF;
  }
  return crc;
}

// ------------------------------------------------------------ збирання -----

function encodeFrame(type, payload) {
  const body = payload || new Uint8Array(0);
  if (body.length > MAX_PAYLOAD) throw new Error('вантаж понад стелю');

  const out = new Uint8Array(FRAME_OVERHEAD + body.length);
  out[0] = MARKER0;
  out[1] = MARKER1;
  out[2] = type;
  out[3] = body.length & 0xFF;
  out[4] = (body.length >> 8) & 0xFF;
  out.set(body, 5);

  // CRC по TYPE + LEN + PAYLOAD; обидва байти LEN — у тому порядку, в якому
  // йдуть на дроті.
  const crc = crc16(out, 2, body.length + 3);
  out[5 + body.length] = crc & 0xFF;
  out[6 + body.length] = (crc >> 8) & 0xFF;
  return out;
}

/* Окремі функції на кожен пакет, а не «зібрати руками на місці»: два
 * інструменти вже колись розійшлися в дрібницях, і саме тому весь протокол
 * живе в одному модулі. */

function encodeKey(code, pressed) {
  return encodeFrame(PKT_KEY, new Uint8Array([code & 0xFF, pressed ? 1 : 0]));
}

function encodeEnc(steps) {
  const p = new Uint8Array(1);
  new DataView(p.buffer).setInt8(0, Math.max(-128, Math.min(127, steps)));
  return encodeFrame(PKT_ENC, p);
}

function encodeTouch(event, x, y) {
  const p = new Uint8Array(5);
  const dv = new DataView(p.buffer);
  dv.setUint8(0, event & 0xFF);
  // ⚠️ Знакові. Пульт обрізає координати по екрану, і -1 при знаковому
  // читанні стає нулем, а при беззнаковому — 65535, тобто протилежним краєм
  // екрана: помилка на піксель вліво відправила б палець управо.
  dv.setInt16(1, x, true);
  dv.setInt16(3, y, true);
  return encodeFrame(PKT_TOUCH, p);
}

function encodeTrim(index, pressed) {
  return encodeFrame(PKT_TRIM, new Uint8Array([index & 0xFF, pressed ? 1 : 0]));
}

function encodeInputState(keys, trims, down, x, y) {
  const p = new Uint8Array(13);
  const dv = new DataView(p.buffer);
  dv.setUint32(0, keys >>> 0, true);
  dv.setUint32(4, trims >>> 0, true);
  dv.setUint8(8, down ? 1 : 0);
  dv.setInt16(9, x, true);
  dv.setInt16(11, y, true);
  return encodeFrame(PKT_INPUT_STATE, p);
}

// --------------------------------------------------- потоковий розбирач ----

/**
 * Дзеркало `Decoder` із прошивки й із remote_ui_proto.py.
 *
 * Той самий порядок ресинхронізації: після відкинутого кадру пошук маркера
 * продовжується з байта, наступного за цим кадром, а вже прочитаний вантаж
 * повторно на маркер не переглядається (docs/03-protocol.md).
 */
class Decoder {
  constructor() {
    this.buf = new Uint8Array(0);
    this.packets = 0;
    this.crcErrors = 0;
    this.oversized = 0;
  }

  /** Забути недочитаний кадр. Викликає транспорт після тиші в каналі. */
  reset() { this.buf = new Uint8Array(0); }

  feed(chunk, onPacket) {
    let buf;
    if (this.buf.length === 0) {
      buf = chunk;
    } else {
      buf = new Uint8Array(this.buf.length + chunk.length);
      buf.set(this.buf);
      buf.set(chunk, this.buf.length);
    }

    let pos = 0;
    for (;;) {
      let start = -1;
      for (let i = pos; i + 1 < buf.length; i++) {
        if (buf[i] === MARKER0 && buf[i + 1] === MARKER1) { start = i; break; }
      }
      if (start < 0) {
        // Маркера немає: лишаємо останній байт — раптом він половина маркера.
        this.buf = buf.slice(Math.max(pos, buf.length - 1));
        return;
      }
      pos = start;

      if (buf.length - pos < FRAME_OVERHEAD) { this.buf = buf.slice(pos); return; }

      const type = buf[pos + 2];
      const len = buf[pos + 3] | (buf[pos + 4] << 8);

      if (len > MAX_PAYLOAD) {
        // Брехлива довжина: стільки байтів не читаємо й не пропускаємо —
        // ресинхронізація починається одразу за старшим байтом LEN.
        this.oversized++;
        pos += 5;
        continue;
      }

      const total = FRAME_OVERHEAD + len;
      if (buf.length - pos < total) { this.buf = buf.slice(pos); return; }

      const got = buf[pos + 5 + len] | (buf[pos + 6 + len] << 8);
      if (got === crc16(buf, pos + 2, len + 3)) {
        this.packets++;
        onPacket(type, buf.subarray(pos + 5, pos + 5 + len));
      } else {
        this.crcErrors++;
      }
      pos += total;
    }
  }
}

// ---------------------------------------------------------------- HELLO ----

function cstr(buf, from, n) {
  const raw = buf.subarray(from, from + n);
  const end = raw.indexOf(0);
  return new TextDecoder('utf-8').decode(end < 0 ? raw : raw.subarray(0, end));
}

/** Найкоротший `HELLO`, який ще має сенс: самі лічильники, без переліків. */
const HELLO_MIN = 13;

/**
 * Читаємо рівно ті поля, які знаємо: решта — запас на сумісність.
 *
 * ⚠️ Обрізаний `HELLO` віддає `null`, а не половину полів. Читання за межу
 * `DataView` кидає виняток, і той виліз би з обробника пакета — тобто
 * побитий кадр, який мав бути просто відкинутий, зупинив би розбір усього
 * потоку. Ширина екрана з обрізаного пакета до того ж була б випадковою.
 *
 * @return {object|null}
 */
// --- Швидкість каналу (ADR-0005) -------------------------------------------

// Вердикти пакета `0x06 BAUD`. Дзеркало `remote_ui::BaudVerdict`.
const BAUD_ACCEPTED = 0;
const BAUD_UNSUPPORTED = 1;
const BAUD_BUSY = 2;
const BAUD_NOT_APPLICABLE = 3;
const BAUD_REVERTED = 4;

const BAUD_VERDICT_TEXT = {
  0: 'перемикаюсь…',
  1: 'пульт не знає такої швидкості',
  2: 'уже триває перемикання',
  3: 'для цього з\'єднання швидкість не має сенсу',
  4: 'не вдалося — пульт повернувся сам',
};

// ⚠️ `nonce` потрібен, щоб не сплутати відповідь на стару команду з
// відповіддю на нову. Людина тисне двічі, перше підтвердження запізнюється —
// і без nonce клієнт вирішив би, що домовився про друге.
function encodeBaudSet(baud, nonce) {
  const p = new Uint8Array(5);
  new DataView(p.buffer).setUint32(0, baud >>> 0, true);
  p[4] = nonce & 0xFF;
  return encodeFrame(PKT_BAUD_SET, p);
}

// Розбирає `0x06 BAUD`. Коротший вантаж — null: половина числа гірша за жодне.
function parseBaud(p) {
  if (p.length < 16) return null;
  const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  return {
    verdict: dv.getUint8(0),
    nonce: dv.getUint8(1),
    target: dv.getUint32(2, true),
    current: dv.getUint32(6, true) || null,
    switchDelayMs: dv.getUint16(10, true),
    revertWindowMs: dv.getUint16(12, true),
    reverts: dv.getUint16(14, true),
    text: BAUD_VERDICT_TEXT[dv.getUint8(0)] || 'невідома відповідь',
  };
}

// ------------------------------------------------------------ FRAME_END ----

/**
 * Розбирає `0x03 FRAME_END`.
 *
 * @return {number|null} `dirtyTiles` — скільки плиток лишилось незасланими на
 *   мить закриття кадру; `null`, якщо ознаки в пакеті немає.
 *
 * ⚠️ `null` — не помилка, а відповідь: так шле прошивка до задачі 0019.
 * Порожній `FRAME_END` лишається легальним назавжди, і клієнт на нього
 * поводиться рівно як досі — показує негайно. Запасна гілка навмисно збігається
 * з чинною поведінкою: помилка в бік «показав» коштує розламу, помилка в бік
 * «чекаю» — мертвої картинки.
 *
 * Окремого біта в `HELLO` немає навмисно, і з тієї самої причини, з якої його
 * немає в перемикача швидкості: два сигнали про одну річ рано чи пізно
 * розійдуться, а вірити однаково довелося б дроту. Біт3 знадобився лише тому,
 * що `INPUT_STATE` — пакет **від** клієнта, і гілку треба обрати до першого
 * доказу; тут доказ приходить у тому самому пакеті, тлумачення якого й
 * змінюється.
 */
function parseFrameEnd(p) {
  if (!p || p.length < 2) return null;
  return p[0] | (p[1] << 8);
}

function parseHello(p) {
  if (p.length < HELLO_MIN) return null;

  const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  const h = {
    version: dv.getUint8(0),
    width: dv.getUint16(1, true),
    height: dv.getUint16(3, true),
    pixfmt: dv.getUint8(5),
    flags: dv.getUint8(6),
    trims: dv.getUint8(7),
    keymask: dv.getUint32(8, true),
    keys: [],
    target: '',
    fw: '',
  };

  const nkeys = dv.getUint8(12);
  let pos = 13;
  for (let i = 0; i < nkeys && pos + 17 <= p.length; i++) {
    h.keys.push({ code: p[pos], name: cstr(p, pos + 1, 16) });
    pos += 17;
  }

  h.target = cstr(p, pos, 32); pos += 32;
  h.fw = cstr(p, pos, 16); pos += 16;

  // --- Швидкість каналу (ADR-0005) ---------------------------------------
  //
  // Хвіст, якого стара прошивка не шле. Його відсутність — не помилка, а
  // відповідь: перемикання ця прошивка не вміє. Тому все під перевіркою
  // довжини.
  //
  // ⚠️ Нуль у полі швидкості означає «поняття не застосовне» (TCP у
  // симуляторі, USB CDC), а не швидкість нуль: нуля в переліку немає й бути
  // не може.
  h.baudCurrent = null;
  h.baudHome = null;
  h.baudList = [];
  if (pos + 9 <= p.length) {
    h.baudCurrent = dv.getUint32(pos, true) || null;
    h.baudHome = dv.getUint32(pos + 4, true) || null;
    const n = dv.getUint8(pos + 8);
    pos += 9;
    for (let i = 0; i < n && pos + 4 <= p.length; i++) {
      h.baudList.push(dv.getUint32(pos, true));
      pos += 4;
    }
  }
  // Єдине джерело правди про підтримку перемикання — непорожній перелік.
  // Окремого біта прапорців немає навмисно: два сигнали про одну річ рано чи
  // пізно розійдуться.
  h.canSwitchBaud = h.baudList.length > 0;

  // Розібрані прапорці лежать поруч із сирим байтом, а не замість нього:
  // сире значення потрібне, щоб побачити біт, якого ця версія ще не знає.
  h.hasTouch = !!(h.flags & HELLO_FLAG_TOUCH);
  h.hasEncoder = !!(h.flags & HELLO_FLAG_ENCODER);
  h.hasFileOps = !!(h.flags & HELLO_FLAG_FILE_OPS);
  h.hasInputState = !!(h.flags & HELLO_FLAG_INPUT_STATE);
  return h;
}

// ----------------------------------------------------------------- плитка --

/**
 * Розібрати плитку.
 * @return {{x,y,w,h,method,pixels:Uint16Array}} або null, якщо плитка бита.
 *
 * Саме null, а не виняток: одна зіпсута плитка не має валити клієнта — так
 * само, як `rle16_decode` у Python.
 */
function decodeTile(p) {
  if (p.length < 9) return null;

  const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  const t = {
    x: dv.getUint16(0, true),
    y: dv.getUint16(2, true),
    w: dv.getUint16(4, true),
    h: dv.getUint16(6, true),
    method: dv.getUint8(8),
    pixels: null,
  };
  if (t.w === 0 || t.h === 0) return null;

  const data = p.subarray(9);
  const need = t.w * t.h;
  const px = new Uint16Array(need);

  if (t.method === TILE_RAW) {
    if (data.length < need * 2) return null;
    for (let i = 0; i < need; i++) px[i] = data[i * 2] | (data[i * 2 + 1] << 8);
  } else if (t.method === TILE_RLE16) {
    // RLE16: послідовність пар [лічильник:1][піксель:2], лічильник 1…255.
    if (data.length % 3) return null;
    let o = 0, k = 0;
    while (o + 2 < data.length) {
      const cnt = data[o];
      if (cnt === 0) return null;
      const v = data[o + 1] | (data[o + 2] << 8);
      o += 3;
      if (k + cnt > need) return null;
      for (let j = 0; j < cnt; j++) px[k++] = v;
    }
    if (k !== need) return null;
  } else {
    return null;
  }

  t.pixels = px;
  return t;
}

/**
 * Покласти плитку в повноекранний буфер RGBA.
 * @return true, якщо поклали; false, якщо плитка не вміщається.
 *
 * Перевірка меж тут не педантизм: бита довжина або розбіжність у розмірі
 * екрана інакше попсували б сусідні рядки, і виглядало б це як вада малювання.
 */
function blitTile(dstRGBA, dstW, dstH, tile) {
  if (tile.x + tile.w > dstW || tile.y + tile.h > dstH) return false;

  for (let row = 0; row < tile.h; row++) {
    let di = ((tile.y + row) * dstW + tile.x) * 4;
    let si = row * tile.w;
    for (let col = 0; col < tile.w; col++, si++, di += 4) {
      const v = tile.pixels[si];
      const r = (v >> 11) & 0x1F;
      const g = (v >> 5) & 0x3F;
      const b = v & 0x1F;
      // Повторення старших бітів, а не множення: 0x1F має давати рівно 255.
      dstRGBA[di] = (r << 3) | (r >> 2);
      dstRGBA[di + 1] = (g << 2) | (g >> 4);
      dstRGBA[di + 2] = (b << 3) | (b >> 2);
      dstRGBA[di + 3] = 255;
    }
  }
  return true;
}

// ------------------------------------------------------- дзеркало вводу ----

/**
 * Власний ввід клієнта — джерело для INPUT_STATE.
 *
 * ⚠️ Порядок обов'язковий: спершу оновити дзеркало, потім слати перехід.
 * Тоді рівень на дроті ніколи не суперечить раніше надісланому переходу.
 */
class InputMirror {
  constructor() { this.keys = 0; this.trims = 0; this.down = false; this.x = 0; this.y = 0; }

  key(code, pressed) {
    if (code >= 32) return;   // у 32-бітову маску не влазить — пульт теж відкине
    const bit = 1 << code;
    this.keys = pressed ? (this.keys | bit) : (this.keys & ~bit);
  }

  trim(index, pressed) {
    if (index >= 32) return;
    const bit = 1 << index;
    this.trims = pressed ? (this.trims | bit) : (this.trims & ~bit);
  }

  touch(event, x, y) {
    this.x = x; this.y = y;
    if (event === TOUCH_DOWN) this.down = true;
    else if (event === TOUCH_UP) this.down = false;
    // MOVE лише пересуває точку — рівень від нього не змінюється.
  }

  clear() { this.keys = 0; this.trims = 0; this.down = false; }

  /**
   * Чи утримується хоч що-небудь.
   *
   * Потрібне лише запасному шляху для старої прошивки. Енкодера тут немає й
   * бути не може — він накопичувальний, рівня в нього не існує.
   */
  holding() { return !!(this.keys || this.trims || this.down); }

  /** Пакет будується в момент відправлення, а не зі знятого раніше знімка. */
  encode() { return encodeInputState(this.keys, this.trims, this.down, this.x, this.y); }
}

/**
 * Що слати, щоб пульт не відпустив ввід. Вибирає **прошивка**, а не клієнт —
 * біт3 у HELLO (docs/03-protocol.md, таблиця в розділі 0x87).
 *
 * @return кадр або null.
 */
function holdPacket(mirror, fwInputState) {
  if (fwInputState) return mirror.encode();
  return mirror.holding() ? encodeFrame(PKT_PING) : null;
}

// ---------------------------------------------------------------- вихід ----

const RemoteUI = {
  MARKER0, MARKER1,
  PKT_HELLO, PKT_TILE, PKT_FRAME_END, PKT_STATE, PKT_LOG, PKT_BAUD,
  PKT_KEY, PKT_ENC, PKT_TOUCH, PKT_REFRESH, PKT_TRIM, PKT_PING, PKT_INPUT_STATE, PKT_BAUD_SET,
  TILE_RAW, TILE_RLE16,
  TOUCH_DOWN, TOUCH_MOVE, TOUCH_UP,
  HELLO_FLAG_TOUCH, HELLO_FLAG_ENCODER, HELLO_FLAG_FILE_OPS, HELLO_FLAG_INPUT_STATE,
  MAX_PAYLOAD, FRAME_OVERHEAD,
  PING_PERIOD_MS, PING_TIMEOUT_MS, INPUT_STATE_PERIOD_MS, LEGACY_HOLD_PERIOD_MS,
  FRAME_WAIT_BASE_MS, FRAME_WAIT_PER_TILE_MS, FRAME_WAIT_MAX_MS, frameWaitMs,
  parseFrameEnd,
  HELLO_MIN,
  crc16, encodeFrame, encodeKey, encodeEnc, encodeTouch, encodeTrim, encodeInputState,
  BAUD_ACCEPTED, BAUD_UNSUPPORTED, BAUD_BUSY, BAUD_NOT_APPLICABLE, BAUD_REVERTED,
  encodeBaudSet, parseBaud,
  Decoder, parseHello, decodeTile, blitTile, InputMirror, holdPacket,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = RemoteUI;   // node: proto_test.js
} else {
  root.RemoteUI = RemoteUI;    // браузер: app.js
}

})(typeof globalThis !== 'undefined' ? globalThis : this);
