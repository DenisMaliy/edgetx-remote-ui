'use strict';
/*
 * Клієнт TX16S Remote UI: оформлення, з'єднання, дотик.
 *
 * Протокол сюди не входить — він у `proto.js`, спільному модулі, який
 * прогоняється тестами під node. Тут лишається те, що без браузера не має
 * сенсу: полотно, палець, таймери, перепідключення.
 *
 * ⚠️ Жодного числа, специфічного для TX16S. Розмір екрана, перелік клавіш і
 * наявність сенсора приходять у HELLO. Літерал 480 тут = помилка.
 */

const P = RemoteUI;

const RECONNECT_MS = 1000;

// ------------------------------------------------------------- малювання ---

const canvas = document.getElementById('screen');
const ctx = canvas.getContext('2d', { alpha: false });
const wrap = document.getElementById('screen-wrap');

let W = 0, H = 0;
let frameBuf = null;   // ImageData на весь екран, накопичується між кадрами

const counters = {
  tiles: 0,
  badTiles: 0,
  outOfBounds: 0,
  beforeHello: 0,
  frames: 0,
};

function resizeTo(w, h) {
  W = w; H = h;
  canvas.width = w;
  canvas.height = h;
  frameBuf = ctx.createImageData(w, h);

  // Непрозорий чорний до першого кадру: createImageData дає прозорий,
  // і без цього перший кадр проступав би крізь порожнечу.
  const d = frameBuf.data;
  for (let i = 3; i < d.length; i += 4) d[i] = 255;

  fitCanvas();
}

/** Полотно вписуємо в екран телефона, зберігаючи співвідношення сторін. */
function fitCanvas() {
  if (!W || !H) return;
  const k = Math.max(0.1, Math.min(wrap.clientWidth / W, wrap.clientHeight / H));
  canvas.style.width = Math.floor(W * k) + 'px';
  canvas.style.height = Math.floor(H * k) + 'px';
}

function onTile(payload) {
  // ⚠️ Плитка до HELLO — не помилка й не втрата.
  //
  // Над TCP пульт вітається сам, тобто перші плитки цілком можуть випередити
  // HELLO, а до нього ми не знаємо розміру екрана й класти їх нікуди. Це
  // прямо передбачено протоколом («просити REFRESH до HELLO безглуздо:
  // клієнт не знає розміру екрана й викине всі плитки»). Лічильник окремий
  // саме тому, що інакше нормальна робота виглядала б як втрата пікселів.
  if (!frameBuf) { counters.beforeHello++; return; }

  const tile = P.decodeTile(payload);
  if (!tile) { counters.badTiles++; return; }

  if (P.blitTile(frameBuf.data, W, H, tile)) counters.tiles++;
  else counters.outOfBounds++;
}

/* Кадр показуємо лише на FRAME_END: до нього картинка неповна. Саме тому
 * клієнт накопичує між кадрами — і саме тому він придатний, щоб подивитись
 * на сторінку, яка оновлюється безперервно. */
function showFrame() {
  if (!frameBuf) return;
  ctx.putImageData(frameBuf, 0, 0);
  counters.frames++;
  veil(null);
}

// ------------------------------------------------------------ з'єднання ----

let ws = null;
let hello = null;
const decoder = new P.Decoder();
const mirror = new P.InputMirror();

let lastHelloAt = 0;
let holdTimer = null, pingTimer = null, reconnectTimer = null;

function veil(text) {
  const el = document.getElementById('veil');
  if (text === null) { el.hidden = true; return; }
  el.hidden = false;
  document.getElementById('veil-text').textContent = text;
}

function chip(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'chip' + (cls ? ' ' + cls : '');
}

function send(frame) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(frame); return true; }
  return false;
}

function connect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;

  veil("під'єднуюсь…");
  chip('link', 'канал…', 'warn');

  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    decoder.reset();
    hello = null;
    veil('вітаюсь із пультом…');
    chip('link', 'канал є', 'warn');
    lastHelloAt = performance.now();

    // ⚠️ Над послідовним портом першим завжди говорить клієнт: пульт мовчить
    // у порожній канал, доки не почує PING (docs/03-protocol.md).
    send(P.encodeFrame(P.PKT_PING));
  };

  ws.onmessage = (ev) => decoder.feed(new Uint8Array(ev.data), onPacket);

  ws.onclose = () => {
    chip('link', 'немає зв’язку', 'bad');
    veil('зв’язок обірвано, перепідключаюсь…');
    stopTimers();
    // Ввід відпускаємо в себе; пульту про це вже сказав міст обнуленим
    // INPUT_STATE — негайно, не чекаючи тайм-ауту.
    mirror.clear();
    pointerId = null;
    scheduleReconnect();
  };

  ws.onerror = () => { try { ws.close(); } catch (e) { /* уже мертвий */ } };
}

function scheduleReconnect() {
  if (!reconnectTimer) reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function stopTimers() {
  clearInterval(holdTimer); holdTimer = null;
  clearInterval(pingTimer); pingTimer = null;
}

function onPacket(type, payload) {
  switch (type) {
    case P.PKT_HELLO: {
      const h = P.parseHello(payload);
      lastHelloAt = performance.now();

      const first = !hello;
      if (!hello || hello.width !== h.width || hello.height !== h.height) {
        resizeTo(h.width, h.height);
      }
      hello = h;

      if (first) {
        chip('link', "зв'язок є", 'ok');
        chip('radio', `${h.target} ${h.fw} · ${h.width}×${h.height}` +
                      (h.hasInputState ? '' : ' · стара прошивка'));
        startTimers();
        veil('чекаю перший кадр…');
        // Тільки тепер REFRESH: до HELLO клієнт не знає, куди класти пікселі.
        send(P.encodeFrame(P.PKT_REFRESH));
      }
      break;
    }

    case P.PKT_TILE:
      onTile(payload);
      break;

    case P.PKT_FRAME_END:
      showFrame();
      break;

    case P.PKT_LOG:
      console.log('[пульт]', new TextDecoder('utf-8').decode(payload));
      break;

    case P.PKT_STATE:
      break;   // текстовий стан — етап 3

    default:
      break;   // невідомі типи ігноруємо: так вимагає сумісність
  }
}

function startTimers() {
  stopTimers();

  // Гілку вибирає **прошивка** — біт3 у HELLO, а не клієнт.
  const period = hello.hasInputState ? P.INPUT_STATE_PERIOD_MS : P.LEGACY_HOLD_PERIOD_MS;
  holdTimer = setInterval(() => {
    const frame = P.holdPacket(mirror, hello.hasInputState);
    if (frame) send(frame);
  }, period);

  // PING — перевірка живого пульта, і більше нічого.
  pingTimer = setInterval(() => {
    send(P.encodeFrame(P.PKT_PING));
    if (performance.now() - lastHelloAt > P.PING_TIMEOUT_MS) {
      chip('link', 'пульт мовчить', 'bad');
      veil('пульт не відповідає…');
      try { ws.close(); } catch (e) { /* уже мертвий */ }
    }
  }, P.PING_PERIOD_MS);
}

// ----------------------------------------------------------------- дотик ---

/**
 * Переклад екранних координат у пікселі пульта.
 *
 * Обрізаємо тут: слати від'ємне безглуздо, а пульт усе одно обрізав би —
 * тільки вже після знакового читання.
 */
function toRadio(ev) {
  const r = canvas.getBoundingClientRect();
  const x = Math.round((ev.clientX - r.left) * W / r.width);
  const y = Math.round((ev.clientY - r.top) * H / r.height);
  return { x: Math.max(0, Math.min(W - 1, x)), y: Math.max(0, Math.min(H - 1, y)) };
}

function sendTouch(event, pt) {
  // ⚠️ Спершу дзеркало, потім перехід — інакше рівень, який піде наступним
  // INPUT_STATE, суперечитиме щойно надісланому переходу.
  mirror.touch(event, pt.x, pt.y);
  send(P.encodeTouch(event, pt.x, pt.y));
}

let pointerId = null;

canvas.addEventListener('pointerdown', (ev) => {
  if (pointerId !== null) return;         // один палець: пульт другого не знає
  if (hello && !hello.hasTouch) return;   // сенсора немає — клавіші дасть етап 3
  pointerId = ev.pointerId;
  canvas.setPointerCapture(ev.pointerId);
  sendTouch(P.TOUCH_DOWN, toRadio(ev));
  ev.preventDefault();
});

canvas.addEventListener('pointermove', (ev) => {
  if (ev.pointerId !== pointerId) return;
  sendTouch(P.TOUCH_MOVE, toRadio(ev));
  ev.preventDefault();
});

function endTouch(ev) {
  if (ev.pointerId !== pointerId) return;
  sendTouch(P.TOUCH_UP, toRadio(ev));
  pointerId = null;
  ev.preventDefault();
}

canvas.addEventListener('pointerup', endTouch);
canvas.addEventListener('pointercancel', endTouch);

/* Вкладку сховали або телефон заблокували — палець на екрані лишатись не має. */
document.addEventListener('visibilitychange', () => {
  if (document.hidden && mirror.down) {
    sendTouch(P.TOUCH_UP, { x: mirror.x, y: mirror.y });
    pointerId = null;
  }
});

// ------------------------------------------------------------- керування ---

document.getElementById('btn-refresh').addEventListener('click', () => {
  send(P.encodeFrame(P.PKT_REFRESH));
});

const info = document.getElementById('info');
document.getElementById('btn-info').addEventListener('click', () => {
  info.hidden = !info.hidden;
  updateInfo();
});

async function updateInfo() {
  if (info.hidden) return;

  const lines = [
    'клієнт',
    `  пакетів        ${decoder.packets}`,
    `  помилок CRC    ${decoder.crcErrors}`,
    `  брехлива LEN   ${decoder.oversized}`,
    `  плиток         ${counters.tiles}`,
    `  битих плиток   ${counters.badTiles}`,
    `  поза екраном   ${counters.outOfBounds}`,
    `  до HELLO       ${counters.beforeHello}`,
    `  кадрів         ${counters.frames}`,
  ];

  if (hello) {
    lines.push(
      `  HELLO 0x${hello.flags.toString(16)}` +
      ` (сенсор ${hello.hasTouch ? '+' : '−'},` +
      ` енкодер ${hello.hasEncoder ? '+' : '−'},` +
      ` INPUT_STATE ${hello.hasInputState ? '+' : '−'})`,
      `  клавіш ${hello.keys.length}, тримерів ${hello.trims}`);
  }
  lines.push('');

  let bridge = 'міст — не відповів';
  try {
    const r = await fetch('/api/stats', { cache: 'no-store' });
    bridge = 'міст\n' + JSON.stringify(await r.json(), null, 1);
  } catch (e) { /* міст міг вимкнути Wi-Fi, або ми не за мостом */ }

  info.textContent = lines.join('\n') + bridge;
}

setInterval(updateInfo, 1000);

/* Частота кадрів — раз на секунду, щоб не смикати сторінку щокадрово. */
let lastFrames = 0;
setInterval(() => {
  chip('fps', (counters.frames - lastFrames) + ' кадр/с');
  lastFrames = counters.frames;
}, 1000);

window.addEventListener('resize', fitCanvas);
window.addEventListener('orientationchange', () => setTimeout(fitCanvas, 200));

connect();
