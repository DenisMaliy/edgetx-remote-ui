'use strict';
/*
 * Тести **обгортки** браузерного клієнта. Запуск:  node webui/app_test.js
 *
 * ⚠️ Навіщо цей файл існує. `webui/app.js` не був покритий нічим — рецензія
 * 0019 назвала це боргом, а рецензія 0020 знайшла в ньому справжню ваду очима:
 * при розриві зв'язку обгортка гасила таймер, але не низку очікування в
 * автоматі, і запас стелі переживав розрив. Наслідок був тихий — перший
 * неповний кадр після кожного перепідключення показувався зшитим і рахувався
 * «за строком», тобто домішка в той самий лічильник, на якому стоять рішення
 * про очікування.
 *
 * Ні `wait_test.js`, ні `wait_crosscheck.py` цього не бачили **за побудовою**:
 * обидва дивляться на автомат, а вада була в обгортці навколо нього.
 *
 * Правил показу тут немає — вони перевіряються там, де живуть. Тут рівно те,
 * що без браузера не має сенсу: чи правильно обгортка кличе автомат, рахує
 * причини й міряє затримку.
 */

const fs = require('fs');
const path = require('path');

let failed = 0;
let checks = 0;

function check(cond, msg) {
  checks++;
  if (!cond) { failed++; console.log('  ✗ ' + msg); }
}

// ------------------------------------------------------------ заглушка ----
//
// Рівно стільки DOM, скільки чіпає `app.js`. Свідомо тупа: якщо клієнт почне
// вимагати більше, тест упаде голосно, а не мовчки перевірятиме порожнечу.

function makeApp() {
  const els = {};
  const el = (id) => {
    if (!els[id]) {
      els[id] = {
        id, hidden: false, textContent: '', innerHTML: '', title: '',
        className: '', dataset: {}, style: {}, value: '',
        width: 16, height: 9, clientWidth: 800, clientHeight: 500,
        addEventListener() {}, appendChild() {}, setPointerCapture() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 480, height: 272 }),
        getContext: () => ({
          createImageData: (w, h) => ({
            width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
          putImageData() {},
        }),
      };
    }
    return els[id];
  };

  const g = {
    document: { getElementById: el, createElement: () => el('_tmp'),
                addEventListener() {}, hidden: false },
    window: { addEventListener() {} },
    performance: { now: () => g.__now },
    WebSocket: function () { this.readyState = 0; this.close = () => {}; },
    location: { host: 'x' },
    fetch: async () => { throw new Error('моста немає'); },
    setInterval: () => 0, clearInterval: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
    TextDecoder: require('util').TextDecoder,
    console,
    __now: 1000,
    __el: el,
  };
  g.WebSocket.OPEN = 1;
  Object.assign(global, g);

  const mod = { exports: {} };
  for (const f of ['proto.js', 'wait.js', 'app.js']) {
    const src = fs.readFileSync(path.join(__dirname, f), 'utf8');
    // Три скрипти браузера ділять один глобальний простір — так їх і виконуємо.
    // `module` бачить лише останній: саме він і має ниточку для тестів.
    new Function('module', 'exports', src)(
      f === 'app.js' ? mod : { exports: null }, null);
  }
  // proto.js і wait.js у не-модульній гілці кладуть себе в глобальний простір.
  return { app: mod.exports, el, g };
}

/** Зсунути годинник заглушки. */
function at(ctx, ms) { global.__now = ms; ctx.g.__now = ms; }

// -------------------------------------------- 1. розрив обриває низку -----

console.log('розрив зв\'язку обриває низку очікування (знахідка рецензії 0020)');
{
  const ctx = makeApp();
  const { policy, onFrameEnd, forgetPendingFrame, resizeTo } = ctx.app;

  // ⚠️ Без кадрового буфера `onFrameEnd` виходить одразу, і тест перевіряв би
  // порожнечу. Піднімаємо екран тією ж функцією, що й клієнт після HELLO.
  resizeTo(480, 272);
  policy.setBaud(2625000);

  const frameEnd = (dirty) => onFrameEnd(
    new Uint8Array([dirty & 0xFF, (dirty >> 8) & 0xFF]));

  at(ctx, 1000);
  frameEnd(90);
  check(policy.pending === true, 'неповний кадр почав низку очікування');

  forgetPendingFrame();
  check(policy.pending === false,
        'розрив обриває низку в автоматі, а не лише таймер');

  // І головне — наслідок: після повернення запас стелі цілий, тобто перший же
  // неповний кадр чекає, а не показується «за строком».
  const before = ctx.app.counters.framesTimeout;
  at(ctx, 60000);
  frameEnd(90);
  check(ctx.app.counters.framesTimeout === before,
        'кадр після повернення не пішов у «показано за строком»');
  check(policy.pending === true, 'нова низка почалась із повним запасом');
}

// ------------------------------------ 2. причина показу обов'язкова -------

console.log('показ без відомої причини падає голосно');
{
  const ctx = makeApp();
  ctx.app.resizeTo(480, 272);
  let threw = false;
  try { ctx.app.showFrame('такої причини немає'); } catch (e) { threw = true; }
  check(threw, 'невідома причина кидає виняток, а не мовчить із NaN');

  const names = Object.values(ctx.app.WHY_COUNTER);
  check(new Set(names).size === names.length,
        'кожна причина має власний лічильник, без збігів');
  check(names.length === 6, `причин рівно шість, а не ${names.length}`);
}

// ---------------------------------------- 3. вимірювач затримки чесний ----

console.log('вимірювач затримки відкидає кадр, що був у дорозі');
{
  const ctx = makeApp();
  const { latency, noteInput, latencyStats, LATENCY_FLOOR_MS } = ctx.app;
  ctx.app.resizeTo(480, 272);
  ctx.app.resetFrameCounters();

  at(ctx, 10000);
  noteInput('touch', { phase: 'down', x: 10, y: 10 });
  check(latency.pendingSource === 'touch', 'заявка на відповідь заведена');

  // Кадр, який прилетів швидше за один період опитування сенсора, відповіддю
  // бути не може: він уже був у дорозі. Відкидаємо його, а заявку лишаємо.
  at(ctx, 10000 + LATENCY_FLOOR_MS - 1);
  ctx.app.showFrame('whole');
  check(latency.early === 1, 'ранній кадр відкинуто й порахувано окремо');
  check(latencyStats('touch') === null, 'зразка з нього не вийшло');
  check(latency.pendingSource === 'touch',
        'заявка лишилась: чекаємо на справжню відповідь');

  at(ctx, 10000 + LATENCY_FLOOR_MS + 20);
  ctx.app.showFrame('whole');
  const s = latencyStats('touch');
  check(s !== null && s.n === 1, 'справжня відповідь дала рівно один зразок');
  check(s !== null && s.med === LATENCY_FLOOR_MS + 20,
        `зразок дорівнює справжній затримці: ${s && s.med}`);
  check(latency.pendingSource === null, 'заявку знято');
}

// ------------------------------------------- 4. джерело вводу в панелі ----

console.log('панель каже, чим керували і яке правило діє (критерій 1.4)');
{
  const ctx = makeApp();
  at(ctx, 20000);
  check(/вводу не було/.test(ctx.app.sourceLabel()),
        'на старті — вводу не було');

  ctx.app.noteInput('touch', { phase: 'down', x: 100, y: 100 });
  ctx.app.noteInput('touch', { phase: 'move', x: 100, y: 160 });
  const drag = ctx.app.sourceLabel();
  check(/протяг/.test(drag) && /НЕ чекаю/.test(drag),
        `протяг названий і правило видно: «${drag}»`);

  ctx.app.noteInput('enc');
  const enc = ctx.app.sourceLabel();
  check(/енкодер/.test(enc) && /чекаю/.test(enc) && !/НЕ чекаю/.test(enc),
        `енкодер названий і правило видно: «${enc}»`);

  ctx.app.noteInput('touch', { phase: 'down', x: 10, y: 10 });
  ctx.app.noteInput('touch', { phase: 'up', x: 11, y: 11 });
  check(/тик/.test(ctx.app.sourceLabel()), 'тик відрізняється від протягу');
}

// -------------------------------- 5. вимикач правила для сліпого досліду --

console.log('вимикач правила протягу доступний сценарію і гасить лічильники');
{
  const ctx = makeApp();
  check(typeof global.window.remoteUiDragRule === 'function'
        || typeof globalThis.remoteUiDragRule === 'function',
        'window.remoteUiDragRule існує — інакше сліпий дослід недосяжний');

  const fn = global.window.remoteUiDragRule || globalThis.remoteUiDragRule;
  check(fn(false) === false, 'вимикається');
  check(ctx.app.policy.dragRule === false, 'автомат про це знає');
  check(fn(true) === true, 'вмикається назад');
}

// ----------------------------------------------------------------------------

console.log(`\nперевірок ${checks}, невдалих ${failed}`);
process.exit(failed ? 1 : 0);
