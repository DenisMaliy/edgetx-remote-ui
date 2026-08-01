'use strict';
/*
 * Тести протоколу на боці браузера. Запуск:  node webui/proto_test.js
 *
 * Головне тут — **еталонні вектори**, ті самі байти, що в
 * `tools/proto_test.py` і в тестах прошивки. Третя реалізація протоколу має
 * право існувати рівно доти, доки сходиться з двома іншими побайтово; усе
 * решта в цьому файлі — перевірки поведінки навколо цього.
 */

const proto = require('./proto.js');

let failed = 0;
let checks = 0;

function check(cond, msg) {
  checks++;
  if (!cond) { failed++; console.log('  ✗ ' + msg); }
}

function hex(u8) {
  return Array.from(u8, (b) => b.toString(16).padStart(2, '0').toUpperCase()).join('');
}

function fromHex(s) {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

// --------------------------------------------------- еталонні вектори ------

console.log('еталонні вектори (спільні з tools/proto_test.py і прошивкою)');

// Порожній PING — вектор із docs/03-protocol.md.
check(hex(proto.encodeFrame(proto.PKT_PING)) === 'E77E86000066 45'.replace(' ', ''),
      'порожній PING: ' + hex(proto.encodeFrame(proto.PKT_PING)));

// HELLO з одним байтом вантажу.
check(hex(proto.encodeFrame(proto.PKT_HELLO, new Uint8Array([0xAA]))) === 'E77E010100AAE4D1',
      'HELLO з одним байтом: ' + hex(proto.encodeFrame(proto.PKT_HELLO, new Uint8Array([0xAA]))));

// INPUT_STATE: keys=0x10, trims=0x02, палець унизу, (100, 200).
// Той самий вектор розбирає C++ (test_input.cpp): тут кодуємо — пульт читає.
const is = proto.encodeInputState(0x10, 0x02, true, 100, 200);
check(hex(is) === 'E77E870D001000000002000000016400C800B36E', 'INPUT_STATE: ' + hex(is));

// Класичний вектор CRC-16/CCITT-FALSE: "123456789" → 0x29B1.
const digits = new TextEncoder().encode('123456789');
check(proto.crc16(digits, 0, digits.length) === 0x29B1,
      'CRC "123456789" = 0x' + proto.crc16(digits, 0, digits.length).toString(16));

// --------------------------------------------------------- кадрування ------

console.log('кадрування');

{
  const got = [];
  const d = new proto.Decoder();
  const a = proto.encodeFrame(proto.PKT_PING);
  const b = proto.encodeFrame(proto.PKT_TILE, new Uint8Array([1, 2, 3]));
  const s = new Uint8Array(a.length + b.length);
  s.set(a); s.set(b, a.length);

  d.feed(s, (t, p) => got.push([t, p.length]));
  check(got.length === 2 && got[0][0] === proto.PKT_PING && got[1][1] === 3,
        'два пакети поспіль');
  check(d.packets === 2 && d.crcErrors === 0, 'лічильники після двох пакетів');
}

{
  // Сміття перед кадром і половинки маркера всередині нього.
  const got = [];
  const d = new proto.Decoder();
  const junk = new Uint8Array([0x00, 0xFF, 0x7E, 0x12, 0xE7, 0x34, 0xAA, 0xE7, 0xE7]);
  const f = proto.encodeFrame(proto.PKT_PING);
  const s = new Uint8Array(junk.length + f.length);
  s.set(junk); s.set(f, junk.length);

  d.feed(s, (t) => got.push(t));
  check(got.length === 1, 'сміття перед кадром не заважає');
  check(d.crcErrors === 0, 'сміття не рахується як помилка CRC');
}

{
  // Маркер усередині вантажу — це нормально, CRC відсіє.
  const got = [];
  const d = new proto.Decoder();
  const payload = new Uint8Array(20);
  for (let i = 0; i < 4; i++) payload.set([0xE7, 0x7E, 0x01, 0x00, 0x00], i * 5);
  const f = proto.encodeFrame(proto.PKT_TILE, payload);

  d.feed(f, (t, p) => got.push([t, p.length]));
  check(got.length === 1 && got[0][1] === 20, 'маркер усередині вантажу не рве кадр');
}

{
  // Будь-яке дроблення потоку: WebSocket ріже кадри як хоче.
  const f = proto.encodeFrame(proto.PKT_TILE, new Uint8Array(300).fill(0x5A));
  for (let chunk = 1; chunk <= 13; chunk++) {
    const got = [];
    const d = new proto.Decoder();
    for (let off = 0; off < f.length; off += chunk) {
      d.feed(f.subarray(off, Math.min(off + chunk, f.length)), (t, p) => got.push(p.length));
    }
    check(got.length === 1 && got[0] === 300, 'дроблення по ' + chunk + ' Б');
  }
}

{
  // Битий CRC — пакет не доходить, лічильник росте.
  const got = [];
  const d = new proto.Decoder();
  const f = proto.encodeFrame(proto.PKT_TILE, new Uint8Array([9, 9, 9]));
  f[f.length - 1] ^= 0xFF;
  d.feed(f, (t) => got.push(t));
  check(got.length === 0 && d.crcErrors === 1, 'битий CRC відсіюється');
}

{
  // Брехлива довжина: ресинхронізація одразу за старшим байтом LEN.
  const got = [];
  const d = new proto.Decoder();
  const liar = new Uint8Array([0xE7, 0x7E, proto.PKT_TILE, 0xFF, 0xFF]);
  const good = proto.encodeFrame(proto.PKT_PING);
  const s = new Uint8Array(liar.length + good.length);
  s.set(liar); s.set(good, liar.length);

  d.feed(s, (t) => got.push(t));
  check(d.oversized === 1, 'брехлива довжина порахована');
  check(got.length === 1, 'наступний цілий пакет не загубився');
}

// -------------------------------------------------------------- плитка -----

console.log('плитка');

{
  // RLE16: 2×2, два кольори.
  const p = new Uint8Array(9 + 6);
  const dv = new DataView(p.buffer);
  dv.setUint16(0, 4, true);   // x
  dv.setUint16(2, 6, true);   // y
  dv.setUint16(4, 2, true);   // w
  dv.setUint16(6, 2, true);   // h
  dv.setUint8(8, proto.TILE_RLE16);
  p.set([2, 0x00, 0xF8], 9);          // 2 × 0xF800 = чистий червоний
  p.set([2, 0x1F, 0x00], 12);         // 2 × 0x001F = чистий синій

  const t = proto.decodeTile(p);
  check(t && t.w === 2 && t.h === 2 && t.x === 4 && t.y === 6, 'заголовок плитки');
  check(t && t.pixels[0] === 0xF800 && t.pixels[3] === 0x001F, 'RLE16 розгорнуто');

  const buf = new Uint8ClampedArray(8 * 8 * 4);
  check(proto.blitTile(buf, 8, 8, t), 'плитка лягла в буфер');

  // Червоний у (4,6): 0xF800 → R=255, G=0, B=0. Саме 255, а не 248 —
  // повторення старших бітів, а не зсув.
  const off = (6 * 8 + 4) * 4;
  check(buf[off] === 255 && buf[off + 1] === 0 && buf[off + 2] === 0 && buf[off + 3] === 255,
        `червоний у (4,6): ${buf[off]},${buf[off + 1]},${buf[off + 2]}`);

  // Синій у (4,7).
  const off2 = (7 * 8 + 4) * 4;
  check(buf[off2] === 0 && buf[off2 + 1] === 0 && buf[off2 + 2] === 255, 'синій у (4,7)');

  // Сусідній піксель лишився недоторканим.
  const off3 = (6 * 8 + 6) * 4;
  check(buf[off3] === 0 && buf[off3 + 3] === 0, 'сусідній піксель не зачеплено');
}

{
  // Білий має бути рівно 255,255,255 — інакше інтерфейс EdgeTX стане сірим.
  const p = new Uint8Array(9 + 3);
  const dv = new DataView(p.buffer);
  dv.setUint16(4, 1, true);
  dv.setUint16(6, 1, true);
  dv.setUint8(8, proto.TILE_RLE16);
  p.set([1, 0xFF, 0xFF], 9);

  const t = proto.decodeTile(p);
  const buf = new Uint8ClampedArray(4);
  proto.blitTile(buf, 1, 1, t);
  check(buf[0] === 255 && buf[1] === 255 && buf[2] === 255,
        `білий: ${buf[0]},${buf[1]},${buf[2]}`);
}

{
  // Сирі пікселі.
  const p = new Uint8Array(9 + 4);
  const dv = new DataView(p.buffer);
  dv.setUint16(4, 2, true);
  dv.setUint16(6, 1, true);
  dv.setUint8(8, proto.TILE_RAW);
  p.set([0x00, 0xF8, 0xE0, 0x07], 9);   // червоний, зелений

  const t = proto.decodeTile(p);
  check(t && t.pixels[0] === 0xF800 && t.pixels[1] === 0x07E0, 'сирі пікселі');
}

{
  // Бита плитка має віддати null, а не впасти й не намалювати сміття.
  const mk = (method, data, w, h) => {
    const p = new Uint8Array(9 + data.length);
    const dv = new DataView(p.buffer);
    dv.setUint16(4, w, true);
    dv.setUint16(6, h, true);
    dv.setUint8(8, method);
    p.set(data, 9);
    return p;
  };

  check(proto.decodeTile(mk(proto.TILE_RLE16, [0, 0x00, 0x00], 1, 1)) === null,
        'лічильник 0 у RLE — бита плитка');
  check(proto.decodeTile(mk(proto.TILE_RLE16, [1, 0x00], 1, 1)) === null,
        'обірвана пара в RLE — бита плитка');
  check(proto.decodeTile(mk(proto.TILE_RLE16, [5, 0x00, 0x00], 1, 1)) === null,
        'RLE довший за плитку — бита');
  check(proto.decodeTile(mk(proto.TILE_RAW, [0x00], 1, 1)) === null,
        'обірвані сирі пікселі — бита плитка');
  check(proto.decodeTile(mk(9, [0], 1, 1)) === null, 'невідомий метод — бита плитка');
  check(proto.decodeTile(new Uint8Array(5)) === null, 'обрізаний заголовок — бита плитка');
}

{
  // Плитка поза екраном не має псувати сусідні рядки.
  const p = new Uint8Array(9 + 3);
  const dv = new DataView(p.buffer);
  dv.setUint16(0, 7, true);   // x = 7 при ширині 8 і плитці 2 — не влазить
  dv.setUint16(4, 2, true);
  dv.setUint16(6, 1, true);
  dv.setUint8(8, proto.TILE_RLE16);
  p.set([2, 0xFF, 0xFF], 9);

  const t = proto.decodeTile(p);
  const buf = new Uint8ClampedArray(8 * 1 * 4);
  check(proto.blitTile(buf, 8, 1, t) === false, 'плитка поза екраном відхилена');
  check(buf.every((v) => v === 0), 'буфер не зачеплено');
}

// ------------------------------------------------------------ HELLO --------

console.log('HELLO');

{
  // Збираємо HELLO так, як його склала б прошивка.
  const nkeys = 2;
  const p = new Uint8Array(13 + nkeys * 17 + 32 + 16);
  const dv = new DataView(p.buffer);
  dv.setUint8(0, 1);
  dv.setUint16(1, 480, true);
  dv.setUint16(3, 272, true);
  dv.setUint8(5, 1);                       // RGB565
  dv.setUint8(6, 0x0B);                    // сенсор + енкодер + INPUT_STATE
  dv.setUint8(7, 8);                       // тримери
  dv.setUint32(8, 0x1234, true);
  dv.setUint8(12, nkeys);

  const enc = new TextEncoder();
  p.set([0x20], 13); p.set(enc.encode('ENTER'), 14);
  p.set([0x21], 30); p.set(enc.encode('EXIT'), 31);
  p.set(enc.encode('X10'), 13 + nkeys * 17);
  p.set(enc.encode('pre-2.12.2'), 13 + nkeys * 17 + 32);

  const h = proto.parseHello(p);
  check(h.width === 480 && h.height === 272, 'розмір екрана з HELLO');
  check(h.hasTouch && h.hasEncoder && h.hasInputState && !h.hasFileOps, 'прапорці розібрано');
  check(h.flags === 0x0B, 'сирий байт прапорців збережено');
  check(h.keys.length === 2 && h.keys[0].name === 'ENTER' && h.keys[1].code === 0x21,
        'перелік клавіш');
  check(h.target === 'X10' && h.fw === 'pre-2.12.2', 'ціль і версія');
  check(h.trims === 8, 'кількість тримерів');
}

{
  // Стара прошивка: біт3 нуль.
  const p = new Uint8Array(13 + 32 + 16);
  new DataView(p.buffer).setUint8(6, 0x03);
  const h = proto.parseHello(p);
  check(!h.hasInputState, 'біт3 нуль — прошивка INPUT_STATE не знає');
}

{
  // ⚠️ Обрізаний HELLO має віддати null, а не половину полів і не виняток:
  // читання за межу DataView вилізло б із обробника пакета і зупинило
  // розбір усього потоку — через кадр, який мав бути просто відкинутий.
  for (const n of [0, 1, 12]) {
    let threw = false, res;
    try { res = proto.parseHello(new Uint8Array(n)); } catch (e) { threw = true; }
    check(!threw && res === null, `HELLO на ${n} Б → null, без винятку`);
  }

  // Рівно мінімум — уже осмислений: лічильники є, переліків просто немає.
  const min = proto.parseHello(new Uint8Array(proto.HELLO_MIN));
  check(min !== null && min.keys.length === 0 && min.target === '',
        'HELLO рівно на 13 Б розбирається');

  // Обрізаний перелік клавіш: беремо стільки, скільки вмістилось.
  const p = new Uint8Array(13 + 17 + 5);
  const dv = new DataView(p.buffer);
  dv.setUint16(1, 320, true);
  dv.setUint16(3, 240, true);
  dv.setUint8(12, 4);                     // обіцяє чотири клавіші
  p.set(new TextEncoder().encode('A'), 14);
  const h = proto.parseHello(p);
  check(h !== null && h.keys.length === 1 && h.keys[0].name === 'A',
        `обіцяно 4 клавіші, вмістилась 1 — узяли ${h ? h.keys.length : '?'}`);
  check(h !== null && h.width === 320 && h.height === 240, 'розмір із обрізаного HELLO цілий');
}

// ------------------------------------------------------ дзеркало вводу -----

console.log('дзеркало вводу і вибір пакета утримання');

{
  const m = new proto.InputMirror();
  check(!m.holding(), 'спочатку не утримується нічого');

  m.key(4, true);
  check(m.keys === 0x10 && m.holding(), 'клавіша 4 у масці');
  m.key(4, false);
  check(m.keys === 0 && !m.holding(), 'клавішу відпущено');

  m.key(99, true);
  check(m.keys === 0, 'клавіша поза 32-бітовою маскою ігнорується');

  m.touch(proto.TOUCH_DOWN, 10, 20);
  check(m.down && m.x === 10 && m.y === 20, 'дотик униз');
  m.touch(proto.TOUCH_MOVE, 11, 21);
  check(m.down && m.x === 11, 'MOVE лише пересуває точку');
  m.touch(proto.TOUCH_UP, 11, 21);
  check(!m.down, 'дотик угору');

  m.key(1, true); m.trim(2, true); m.touch(proto.TOUCH_DOWN, 1, 1);
  m.clear();
  check(m.keys === 0 && m.trims === 0 && !m.down, 'clear відпускає все');
}

{
  // Біт3 виставлений: стан шлеться безумовно, навіть коли не утримується нічого.
  const m = new proto.InputMirror();
  const withBit = proto.holdPacket(m, true);
  check(withBit !== null && withBit[2] === proto.PKT_INPUT_STATE,
        'з бітом3 — INPUT_STATE безумовно');

  // Без біта3: PING тільки поки щось утримується.
  check(proto.holdPacket(m, false) === null, 'без біта3 і без утримання — нічого');
  m.key(0, true);
  const legacy = proto.holdPacket(m, false);
  check(legacy !== null && legacy[2] === proto.PKT_PING, 'без біта3, але утримується — PING');
}

{
  // Дзеркало має віддавати той самий байтовий вигляд, що й пряме кодування.
  const m = new proto.InputMirror();
  m.key(4, true);
  m.trim(1, true);
  m.touch(proto.TOUCH_DOWN, 100, 200);
  check(hex(m.encode()) === hex(proto.encodeInputState(0x10, 0x02, true, 100, 200)),
        'дзеркало кодує так само, як пряма функція');
}

// -------------------------------------------------------------- FRAME_END --

console.log('FRAME_END: чи цілий кадр');

{
  // Вектор, спільний із tools/proto_test.py і test_capture.cpp: FRAME_END із
  // dirtyTiles = 42.
  const fe = proto.encodeFrame(proto.PKT_FRAME_END, new Uint8Array([0x2A, 0x00]));
  check(hex(fe) === 'E77E0302002A009BFB', 'FRAME_END з dirtyTiles=42: ' + hex(fe));

  check(proto.parseFrameEnd(new Uint8Array([0x2A, 0x00])) === 42, 'dirtyTiles=42');
  check(proto.parseFrameEnd(new Uint8Array([0x00, 0x00])) === 0, 'dirtyTiles=0');
  // Little-endian, як усе інше в протоколі.
  check(proto.parseFrameEnd(new Uint8Array([0x01, 0x01])) === 257, 'порядок байтів LE');
  // Хвіст ігнорується — правило сумісності «поля лише в кінець».
  check(proto.parseFrameEnd(new Uint8Array([0x05, 0x00, 0xFF, 0xFF])) === 5,
        'зайвий хвіст не заважає');

  // ⚠️ Порожній вантаж — це «не знаю», а не «нуль». Сплутати їх означає
  // показати зшитий кадр як цілісний, тобто саме той розлам, який лікуємо.
  check(proto.parseFrameEnd(new Uint8Array(0)) === null, 'порожній FRAME_END → null');
  check(proto.parseFrameEnd(new Uint8Array([0x07])) === null, 'один байт → null');

  // Строк очікування: лінійний до стелі, далі стала.
  check(proto.frameWaitMs(0) === 50, 'строк при 0 плитках');
  check(proto.frameWaitMs(10) === 80, 'строк при 10 плитках');
  check(proto.frameWaitMs(60) === 230, 'строк при 60 плитках');
  check(proto.frameWaitMs(67) === 250, 'стеля рівно на 67 плитках');
  check(proto.frameWaitMs(1000) === 250, 'понад стелю строк не росте');
}

// ------------------------------------------------------------ координати ---

console.log('знаковість координат');

{
  // ⚠️ -1 має лишитись -1, а не стати 65535: інакше помилка на піксель вліво
  // відправила б палець у протилежний край екрана.
  const f = proto.encodeTouch(proto.TOUCH_DOWN, -1, -2);
  const dv = new DataView(f.buffer, 5, 5);
  check(dv.getInt16(1, true) === -1 && dv.getInt16(3, true) === -2, 'TOUCH: -1 лишається -1');

  const s = proto.encodeInputState(0, 0, true, -1, -2);
  const dv2 = new DataView(s.buffer, 5, 13);
  check(dv2.getInt16(9, true) === -1 && dv2.getInt16(11, true) === -2,
        'INPUT_STATE: -1 лишається -1');
}

// ------------------------------------------------------------------ вихід --

console.log('');
console.log((failed ? 'ПОМИЛКА' : 'усе гаразд') + `: перевірок ${checks}, невдалих ${failed}`);
process.exit(failed ? 1 : 0);
