#!/usr/bin/env node
'use strict';
/*
 * Проба браузерного клієнта без браузера.
 *
 * Запускає **той самий** `webui/proto.js`, що виконується на телефоні, через
 * **той самий** WebSocket, і зберігає те, що з нього вийшло, у PNG. Тобто
 * перевіряє весь ланцюг разом: сервер → рукостискання WebSocket →
 * PING → HELLO → REFRESH → плитки → FRAME_END → пікселі → ввід назад.
 *
 * Модульні тести (`webui/proto_test.js`) перевіряють байти, ця проба —
 * ланцюг. Разом вони закривають клієнта до того, як з'явиться залізо.
 *
 * Друга частина проби перевіряє головну вимогу безпеки до моста: втративши
 * телефон, він **негайно** шле пульту обнулений `INPUT_STATE`.
 *
 * Запуск (симулятор EdgeTX і tools/webui_serve.py мають уже працювати):
 *
 *     node tools/webui_probe.js [http://127.0.0.1:8080] [знімок.png]
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const P = require(path.join(__dirname, '..', 'webui', 'proto.js'));

const base = process.argv[2] || 'http://127.0.0.1:8080';
const outPng = process.argv[3] || '/tmp/webui-probe.png';

let failed = 0;
const ok = (cond, msg) => {
  console.log((cond ? '  ✓ ' : '  ✗ ') + msg);
  if (!cond) failed++;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ----------------------------------------------------------------- PNG -----

function png(w, h, rgba) {
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0;   // фільтр None
    Buffer.from(rgba.buffer, rgba.byteOffset + y * w * 4, w * 4).copy(raw, y * (w * 4 + 1) + 1);
  }

  const chunk = (type, data) => {
    const out = Buffer.alloc(8 + data.length + 4);
    out.writeUInt32BE(data.length, 0);
    out.write(type, 4, 'ascii');
    data.copy(out, 8);
    out.writeUInt32BE(
      zlib.crc32(Buffer.concat([Buffer.from(type, 'ascii'), data])) >>> 0, 8 + data.length);
    return out;
  };

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;   // 8 біт на канал
  ihdr[9] = 6;   // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ------------------------------------------------------------- з'єднання ---

/** Обгортка над WebSocket, що поводиться рівно як клієнт у браузері. */
class Probe {
  constructor(url) {
    this.decoder = new P.Decoder();
    this.mirror = new P.InputMirror();
    this.hello = null;
    this.W = 0; this.H = 0; this.buf = null;
    this.tiles = 0; this.bad = 0; this.oob = 0; this.beforeHello = 0; this.frames = 0;

    this.ws = new WebSocket(url);
    this.ws.binaryType = 'arraybuffer';
    this.ws.addEventListener('message', (ev) => this.decoder.feed(new Uint8Array(ev.data),
                                                                 (t, p) => this.onPacket(t, p)));
  }

  open() {
    return new Promise((res, rej) => {
      this.ws.addEventListener('open', res);
      this.ws.addEventListener('error', rej);
    });
  }

  send(frame) {
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(frame);
  }

  close() { try { this.ws.close(); } catch (e) { /* уже мертвий */ } }

  onPacket(type, payload) {
    if (type === P.PKT_HELLO) {
      const h = P.parseHello(payload);
      if (h && !this.hello) {
        this.hello = h;
        this.W = h.width; this.H = h.height;
        this.buf = new Uint8ClampedArray(h.width * h.height * 4);
        for (let i = 3; i < this.buf.length; i += 4) this.buf[i] = 255;
      }
    } else if (type === P.PKT_TILE) {
      // Плитка до HELLO — не втрата: над TCP пульт вітається сам, і перші
      // плитки законно випереджають HELLO, а класти їх іще нікуди.
      if (!this.buf) { this.beforeHello++; return; }
      const t = P.decodeTile(payload);
      if (!t) { this.bad++; return; }
      if (P.blitTile(this.buf, this.W, this.H, t)) this.tiles++;
      else this.oob++;
    } else if (type === P.PKT_FRAME_END) {
      this.frames++;
    }
  }

  /**
   * Рукостискання рівно за протоколом: першим говорить клієнт.
   *
   * ⚠️ `PING` повторюється, а не шлеться один раз. Пульт мовчить у порожній
   * канал, доки не почує клієнта, тож привітальний пакет — єдина подія, яка
   * взагалі запускає розмову; загубився він — і чекати можна вічно.
   */
  async handshake(timeoutMs = 4000, retryMs = 500) {
    const until = Date.now() + timeoutMs;
    let nextPing = 0;
    while (!this.hello && Date.now() < until) {
      if (Date.now() >= nextPing) {
        this.send(P.encodeFrame(P.PKT_PING));
        nextPing = Date.now() + retryMs;
      }
      await sleep(20);
    }
    if (!this.hello) return false;
    this.send(P.encodeFrame(P.PKT_REFRESH));
    return true;
  }

  /** Утримання, як у браузері: раз на 250 мс безумовно. */
  startHold() {
    this.holdTimer = setInterval(() => {
      const f = P.holdPacket(this.mirror, this.hello && this.hello.hasInputState);
      if (f) this.send(f);
    }, P.INPUT_STATE_PERIOD_MS);
  }

  stopHold() { clearInterval(this.holdTimer); }

  /** Скільки кадрів прийшло за `ms` після дії. */
  async framesAfter(ms) {
    const before = this.frames;
    await sleep(ms);
    return this.frames - before;
  }

  async key(code, holdMs = 120) {
    this.mirror.key(code, true);
    this.send(P.encodeKey(code, true));
    await sleep(holdMs);
    this.mirror.key(code, false);
    this.send(P.encodeKey(code, false));
  }

  /**
   * Проведення пальцем.
   *
   * ⚠️ Саме проведення, а не тик у точку. Тик залежить від того, що під
   * пальцем: на порожньому місці він нічого не намалює, і проба сказала б
   * «ввід не дійшов», хоча дійшов. Це та сама пастка, що зіпсувала перший
   * замір затримки в задачі 0010. Проведення прокручує будь-який перелік і
   * не залежить від того, на якому екрані стоїть пульт.
   */
  async swipe(x, y0, y1, steps = 6) {
    this.mirror.touch(P.TOUCH_DOWN, x, y0);
    this.send(P.encodeTouch(P.TOUCH_DOWN, x, y0));
    for (let i = 1; i <= steps; i++) {
      const y = Math.round(y0 + (y1 - y0) * i / steps);
      await sleep(25);
      this.mirror.touch(P.TOUCH_MOVE, x, y);
      this.send(P.encodeTouch(P.TOUCH_MOVE, x, y));
    }
    await sleep(25);
    this.mirror.touch(P.TOUCH_UP, x, y1);
    this.send(P.encodeTouch(P.TOUCH_UP, x, y1));
  }
}

/**
 * Адреса потоку. `claim` — заявка, з якою приходимо (задача 0024): без неї
 * зайнятий міст відповість `{"busy":1}` і закриє сокет.
 */
const wsUrl = (claim) => base.replace(/^http/, 'ws') + '/ws' + (claim ? '?' + claim : '');
const stats = async () => (await fetch(base + '/api/stats', { cache: 'no-store' })).json();

/** Клавіша з переліку HELLO за назвою; жорсткого списку в клієнті бути не може. */
function keyByName(hello, names) {
  for (const want of names) {
    const k = hello.keys.find((k) => k.name.toUpperCase() === want.toUpperCase());
    if (k) return k;
  }
  return hello.keys[0];
}

// ------------------------------------------------------------------ хід ----

async function checkHttp() {
  console.log('\nHTTP: чи віддаються файли сторінки');
  // ⚠️ Перелік має накривати **всі** скрипти сторінки. Пропущений файл — це
  // `RemoteUIWait is not defined` на першому рядку `app.js`, тобто біла
  // сторінка на телефоні при зеленому пробнику. Це єдина автоматична сторожа
  // того, що міст справді віддає клієнт.
  for (const [name, marker] of [['/', '<canvas'], ['/proto.js', 'RemoteUI'],
                                ['/wait.js', 'RemoteUIWait'],
                                ['/panels.js', 'RemoteUIPanels'],
                                ['/app.js', 'RemoteUI'], ['/style.css', '#screen']]) {
    try {
      const r = await fetch(base + name);
      const text = await r.text();
      ok(r.ok && text.includes(marker), `${name} — ${r.status}, ${text.length} Б`);
    } catch (e) {
      ok(false, `${name} — ${e.message}`);
    }
  }
}

async function checkStream() {
  console.log('\nWebSocket: рукостискання, кадри, ввід');

  /* ⚠️ `take=1` — свідома заявка, як натискання кнопки в клієнті. Проба
   * запускається руками й саме для того, щоб поговорити з пультом; чекати в
   * черзі їй нема сенсу, а мовчазного витіснення після 0024 більше немає. */
  const p = new Probe(wsUrl('id=probe-stream&take=1'));
  await p.open();

  const greeted = await p.handshake();
  ok(greeted, greeted
    ? `HELLO: ${p.hello.target} ${p.hello.fw}, ${p.W}×${p.H}, ` +
      `прапорці 0x${p.hello.flags.toString(16)}`
    : 'HELLO не прийшов');
  if (!greeted) { p.close(); return null; }

  p.startHold();
  const h = p.hello;

  ok(h.hasInputState, `біт3 (прошивка розуміє INPUT_STATE): ${h.hasInputState ? 'є' : 'НЕМАЄ'}`);
  ok(h.keys.length > 0,
     `клавіш у HELLO: ${h.keys.length} (${h.keys.map((k) => k.name).join(', ')})`);
  ok(h.hasTouch, `сенсор у HELLO: ${h.hasTouch ? 'є' : 'немає'}`);

  await sleep(1200);   // перший повний кадр після REFRESH
  ok(p.frames > 0, `кадрів (FRAME_END): ${p.frames}`);
  ok(p.tiles > 0, `плиток намальовано: ${p.tiles}`);
  ok(p.bad === 0, `битих плиток: ${p.bad}`);
  ok(p.oob === 0, `плиток поза екраном: ${p.oob}`);
  console.log(`  · плиток до HELLO (норма для TCP, на UART має бути 0): ${p.beforeHello}`);
  ok(p.decoder.crcErrors === 0, `помилок CRC: ${p.decoder.crcErrors}`);
  ok(p.decoder.oversized === 0, `брехливих довжин: ${p.decoder.oversized}`);

  /* Ввід перевіряємо там, де він **справді щось малює**. SYS відкриває
   * налаштування пульта з будь-якого екрана, тобто перемальовує все; на
   * стартовому вікні «Press any key to skip» перше натискання просто закриє
   * його — теж перемальовка. */
  const sysKey = keyByName(h, ['SYS', 'MDL', 'Enter']);
  let after = 0;
  {
    const before = p.frames;
    await p.key(sysKey.code);
    await sleep(900);
    after = p.frames - before;
  }
  ok(after > 0, `клавіша «${sysKey.name}» дійшла до пульта: +${after} кадр(ів)`);

  // Ще раз, щоб напевно опинитись у меню, якщо перше натискання закрило вікно.
  await p.key(sysKey.code);
  await sleep(900);

  {
    const before = p.frames;
    await p.swipe(Math.round(p.W * 0.5), Math.round(p.H * 0.75), Math.round(p.H * 0.30));
    await sleep(900);
    const delta = p.frames - before;
    ok(delta > 0, `проведення пальцем дійшло до пульта: +${delta} кадр(ів)`);
  }

  // Повертаємось, щоб симулятор лишився на головному екрані.
  const rtn = keyByName(h, ['RTN', 'EXIT']);
  await p.key(rtn.code);
  await sleep(400);
  await p.key(rtn.code);
  await sleep(900);

  let painted = 0;
  for (let i = 0; i < p.buf.length; i += 4) {
    if (p.buf[i] || p.buf[i + 1] || p.buf[i + 2]) painted++;
  }
  const total = p.W * p.H;
  ok(painted > total / 100,
     `не чорних пікселів: ${painted} з ${total} (${(100 * painted / total).toFixed(1)}%)`);

  fs.writeFileSync(outPng, png(p.W, p.H, p.buf));
  console.log(`  знімок: ${outPng}`);

  p.stopHold();
  p.close();
  await sleep(300);
  return h;
}

/**
 * Найважливіша вимога до моста: втративши телефон, він **негайно** шле
 * пульту обнулений `INPUT_STATE`, а не чекає тайм-ауту.
 *
 * Перевіряємо обидва способи втратити телефон, бо вони різні за природою:
 * розрив сокета видно одразу, а телефон, винесений за межу зв'язку, не
 * породжує жодної події — сокет лишається відкритим ще десятки секунд, і
 * саме в цьому випадку клавіша лишилася б натиснутою назавжди.
 */
async function checkSafety(hello) {
  console.log('\nбезпека: відпускання вводу при втраті телефона');

  const key = keyByName(hello, ['RTN', 'EXIT']);

  const holdThenLeave = async (how) => {
    const p = new Probe(wsUrl('id=probe-safety&take=1'));
    await p.open();
    if (!await p.handshake()) throw new Error('пульт не привітався');

    // Утримуємо клавішу — саме її не можна лишити натиснутою.
    p.mirror.key(key.code, true);
    p.send(P.encodeKey(key.code, true));
    for (let i = 0; i < 3; i++) {
      p.send(p.mirror.encode());
      await sleep(P.INPUT_STATE_PERIOD_MS);
    }

    const before = await stats();
    if (how === 'close') p.close();
    // how === 'silence': сокет лишаємо відкритим і просто замовкаємо.

    await sleep(1400);   // довше за поріг мовчання 750 мс, із запасом
    const after = await stats();

    p.close();
    await sleep(300);
    return { before, after };
  };

  {
    const { before, after } = await holdThenLeave('close');
    ok(after.session.releases > before.session.releases,
       `розрив сокета → обнулений INPUT_STATE: releases ` +
       `${before.session.releases} → ${after.session.releases}`);
    ok(after.session.lost > before.session.lost,
       `втрату помічено: lost ${before.session.lost} → ${after.session.lost}`);
  }

  {
    const { before, after } = await holdThenLeave('silence');
    ok(after.session.silence_timeouts > before.session.silence_timeouts,
       `мовчання телефона → обнулений INPUT_STATE: silence_timeouts ` +
       `${before.session.silence_timeouts} → ${after.session.silence_timeouts}`);
    ok(after.session.releases > before.session.releases,
       `і ввід відпущено: releases ${before.session.releases} → ${after.session.releases}`);
  }
}

/**
 * Переймання керування: другий телефон забирає пульт **на вимогу**.
 *
 * ⚠️ Ця перевірка з'явилась через рецензію: двійник моста **стверджував**, що
 * витісняє клієнта, а насправді лишав потоки старого — тобто пункт «кілька
 * телефонів одночасно» ніколи не перевірявся й не міг бути перевірений.
 * Діра була не в мості, а в доказовій базі, і це найгірший вид дірки: вона
 * мовчить.
 *
 * ⚠️ **Після задачі 0024 перевірка стала двома.** Мовчазного витіснення
 * більше немає: прибулець без заявки чує «зайнято» й іде в чергу, а забирає
 * пульт лише той, хто сказав `take=1`. Обидва випадки тут, бо саме на межі
 * між ними живе вся вада: клієнт **шле ввід**, і два джерела натискань на
 * пульт із розбитим екраном — спосіб зробити щось несподіване, не побачивши
 * цього.
 *
 * ⚠️ Це заразом єдиний прилад під критерій 2.5 задачі 0024 — «ввід
 * відпускається, коли керування переходить». Він тримає клавішу першим
 * клієнтом і звіряє `session.releases` після переходу.
 */
async function checkTakeover(hello) {
  console.log('\nчерга й переймання керування');

  const key = keyByName(hello, ['RTN', 'EXIT']);

  const first = new Probe(wsUrl('id=probe1'));
  await first.open();
  if (!await first.handshake()) { ok(false, 'перший клієнт не привітався'); return; }

  // --- прибулець без заявки: йому кажуть «зайнято» -------------------------
  const busyBefore = await stats();
  const idle = new Probe(wsUrl('id=probe-idle'));
  await idle.open();
  let busySaid = null;
  idle.ws.addEventListener('message', (ev) => {
    if (typeof ev.data === 'string') busySaid = ev.data;
  });
  const idleGreeted = await idle.handshake();
  await sleep(700);
  const busyAfter = await stats();

  ok(!idleGreeted, 'прибулець без заявки HELLO не отримує — пульт зайнятий');
  ok(busySaid !== null && busySaid.includes('busy'),
     `міст сказав словом, а не мовчазним розривом: ${busySaid}`);
  ok(busyAfter.queue.busy_refused > busyBefore.queue.busy_refused,
     `відмов «зайнято»: ${busyBefore.queue.busy_refused} → ${busyAfter.queue.busy_refused}`);
  ok(busyAfter.lost_by.evicted === busyBefore.lost_by.evicted,
     '⚠️ і головне: першого при цьому НЕ витіснили — це й була гойдалка');
  idle.close();
  await sleep(200);

  // --- переймання на вимогу ------------------------------------------------
  // Перший щось утримує — саме це не можна лишити натиснутим.
  first.mirror.key(key.code, true);
  first.send(P.encodeKey(key.code, true));
  first.send(first.mirror.encode());
  await sleep(300);

  let firstClosed = false;
  first.ws.addEventListener('close', () => { firstClosed = true; });

  const before = await stats();

  const second = new Probe(wsUrl('id=probe2&take=1'));
  await second.open();
  const greeted = await second.handshake();
  await sleep(700);
  const after = await stats();

  ok(greeted, 'другий клієнт із заявкою привітався й дістав HELLO');
  ok(firstClosed, `перший клієнт вигнаний: сокет ${firstClosed ? 'закрито' : 'ЛИШИВСЯ ВІДКРИТИМ'}`);
  ok(after.session.releases > before.session.releases,
     `переймання відпустило ввід: releases ${before.session.releases} → ` +
     `${after.session.releases}`);

  // І головне: після переймання працює саме другий.
  const framesBefore = second.frames;
  second.send(P.encodeFrame(P.PKT_REFRESH));
  await sleep(1200);
  ok(second.frames > framesBefore,
     `другий клієнт отримує кадри: +${second.frames - framesBefore}`);

  first.close();
  second.close();
  await sleep(300);
}

async function main() {
  console.log(`проба клієнта проти ${base}`);

  await checkHttp();
  const hello = await checkStream();
  if (hello) await checkTakeover(hello);
  if (hello) await checkSafety(hello);

  console.log('\n' + (failed ? `ПОМИЛКА: невдалих перевірок ${failed}` : 'усе гаразд'));
  process.exit(failed ? 1 : 0);
}

main();
