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

/**
 * Свіжий примірник клієнта на заглушці DOM.
 *
 * @param keep сховище з попереднього примірника — тобто «та сама людина
 *        відкрила сторінку вдруге». Без нього сховище порожнє: перший у
 *        житті візит (задача 0025).
 */
function makeApp(keep) {
  const els = {};
  const el = (id) => {
    if (!els[id]) {
      els[id] = {
        id, hidden: false, textContent: '', innerHTML: '', title: '',
        className: '', dataset: {}, style: {}, value: '', type: '',
        width: 16, height: 9, clientWidth: 800, clientHeight: 500,
        // ⚠️ Панелі мають ненульовий розмір: `fitCanvas` читає саме
        // `offsetWidth`/`offsetHeight`, і без них уся її арифметика — `NaN`.
        // Доти це нікому не заважало (вона виходила одразу, поки немає
        // розміру екрана), а від задачі 0025 вона працює й до `HELLO`.
        offsetWidth: 110, offsetHeight: 44,
        classList: { add() {}, remove() {}, toggle() {} },
        contains: () => false,
        focus() {},
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

  const store = keep || new Map();

  const g = {
    document: { getElementById: el, createElement: () => el('_tmp'),
                addEventListener() {}, hidden: false, activeElement: null },
    window: { addEventListener() {} },
    performance: { now: () => g.__now },
    WebSocket: function () { this.readyState = 0; this.close = () => {}; },
    location: { host: 'x' },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    fetch: async () => { throw new Error('моста немає'); },
    setInterval: () => 0, clearInterval: () => {},
    // ⚠️ Затримки записуються, а не викидаються: питання «чи клієнт узагалі
    // збирається повертатись» інакше не поставити — таймер тут не спрацьовує
    // ніколи, і про намір говорить лише сам факт його заведення.
    setTimeout: (fn, ms) => { g.__timers.push(ms); return 1; },
    clearTimeout: () => {},
    __timers: [],
    TextDecoder: require('util').TextDecoder,
    console,
    __now: 1000,
    __el: el,
    __store: store,
  };
  g.WebSocket.OPEN = 1;
  Object.assign(global, g);

  const mod = { exports: {} };
  for (const f of ['proto.js', 'wait.js', 'panels.js', 'app.js']) {
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

// ------------------------------- 6. утримання тримається до останнього ----
//
// ⚠️ Вада, від якої цей розділ стереже: клавішу можуть тримати двома руками —
// пальцем на панелі й стрілкою на клавіатурі. Прапорець замість множини
// тримачів зняв би її першим же відпусканням, і пульт лишився б із клавішею,
// яку ніхто вже не тримає, — або навпаки, назавжди натиснутою.

console.log('утримання знімається лише останнім тримачем (критерій 4.1)');
{
  const ctx = makeApp();
  const { held, holdBegin, holdEnd, releaseAllHeld } = ctx.app;

  const key = { kind: 'key', code: 7 };
  holdBegin(key, 'pad');
  check(held.size === 1, 'перший тримач почав утримання');
  holdBegin(key, 'kbd');
  check(held.size === 1, 'другий тримач не завів другого утримання');

  holdEnd(key, 'pad');
  check(held.size === 1, 'перший відпустив — клавіша ще тримається');
  holdEnd(key, 'kbd');
  check(held.size === 0, 'відпустив останній — і тільки тепер знято');

  holdBegin(key, 'pad');
  releaseAllHeld();
  check(held.size === 0, 'releaseAllHeld знімає все — розрив, blur, ховання');
}

// -------------------- 7. намір виконується тим, що пульт справді має -------

console.log('намір бере клавішу або енкодер — за тим, що назвав HELLO');
{
  const ctx = makeApp();
  const { held, counters, intentHold, buildPanels, getIntents } = ctx.app;

  // Такий набір клавіш віддає TX16S: стрілок немає, є енкодер.
  const hello = {
    width: 480, height: 272, flags: 0x0b,
    hasTouch: true, hasEncoder: true, hasInputState: true,
    keys: [
      { code: 0, name: 'RTN' }, { code: 1, name: 'Enter' },
      { code: 2, name: 'PAGE<' }, { code: 3, name: 'PAGE>' },
      { code: 4, name: 'MDL' }, { code: 5, name: 'TELE' },
      { code: 6, name: 'SYS' },
    ],
  };
  buildPanels(hello);
  const intents = getIntents();

  check(intents.up && intents.up.kind === 'enc' && intents.up.steps === -1,
        'стрілок немає — «вгору» виконує клацання енкодера');
  check(intents.select && intents.select.code === 1, '«вибрати» — це Enter');
  check(intents.back && intents.back.code === 0, '«назад» — це RTN');
  check(intents.left === null && intents.right === null,
        '⚠️ вліво-вправо не виконує ніхто: сторінки під них вішати заборонено');

  const encBefore = counters.encClicks;
  intentHold('up', true, 'kbd:ArrowUp');
  check(counters.encClicks === encBefore + 1,
        'перше клацання йде негайно, решту доставить розгін');
  intentHold('up', false, 'kbd:ArrowUp');
  check(held.size === 0, 'відпустили — розгін спинився');

  const keysBefore = counters.keyPresses;
  intentHold('page-prev', true, 'kbd:PageUp');
  check(counters.keyPresses === keysBefore + 1, 'сторінка — це справжня клавіша');
  check(held.size === 1, 'і вона тримається, доки тримають');
  intentHold('page-prev', false, 'kbd:PageUp');

  // Намір, якого пульт виконати не може, не має тихо робити щось інше.
  intentHold('left', true, 'kbd:ArrowLeft');
  check(held.size === 0, 'намір без виконавця не робить нічого');

  // Перебудова панелі не має лишати натиснутого: кнопки зникнуть разом зі
  // своїми обробниками, і відпускати буде нікому.
  intentHold('page-next', true, 'pad:page-next');
  check(held.size === 1, 'клавішу тримають');
  buildPanels(Object.assign({}, hello, { keys: hello.keys.slice(0, 3) }));
  check(held.size === 0, 'перебудова панелі відпустила все');
}

// ------------------------------------- 8. черга: господар один -------------
//
// ⚠️ Вада, від якої стереже цей розділ, — **гойдалка** (задача 0024): клієнт
// після будь-якого закриття сокета вертався через секунду, зокрема й після
// того, як його свідомо витіснили. Разом із витісненням на боці моста це
// давало двох клієнтів, що по черзі виганяють одне одного без кінця.
//
// Тут перевіряється саме те, що вирішує обгортка: з чим вона приходить до
// моста і чи збирається повертатись.

console.log('черга: витіснений чекає, а не вертається сам (задача 0024)');
{
  const ctx = makeApp();

  // Свій WebSocket, який запам'ятовує адресу й дає дотягтись до обробників.
  const urls = [];
  global.WebSocket = function (url) {
    urls.push(url);
    this.readyState = 1;      // OPEN: вітання клієнта має куди піти
    this.close = () => {};
    this.send = () => {};
    global.__ws = this;
  };
  global.WebSocket.OPEN = 1;

  const { connect, onBridgeSays, gate, queueState } = ctx.app;

  connect();
  const first = urls[urls.length - 1];
  check(/[?&]id=[a-z0-9]+/.test(first), `перше під'єднання називає себе: ${first}`);
  check(!/take=1|resume=1/.test(first),
        'і нічого не вимагає: звичайне відкриття сторінки');

  // --- були господарем, сокет помер: вертаємось і забираємо своє ------------
  global.__ws.onopen();
  global.__ws.onclose();
  check(queueState().wasOwner === true, 'ми були господарем — це запам’ятано');
  const timersAfterLoss = ctx.g.__timers.filter((ms) => ms === 1000).length;
  check(timersAfterLoss >= 1, 'після справжнього обриву клієнт збирається назад');

  connect();
  check(/resume=1/.test(urls[urls.length - 1]),
        `повернення заявлене як своє: ${urls[urls.length - 1]}`);

  // --- сказали «зайнято»: чекаємо мовчки ------------------------------------
  global.__ws.onopen();
  onBridgeSays('{"busy":1}');
  const st = queueState();
  check(st.queued === true, 'слово моста «зайнято» ставить нас у чергу');
  check(st.wasOwner === false,
        '⚠️ і знімає право забрати своє: свого слота в нас більше немає');
  check(ctx.el('veil-buttons').hidden === false, 'вікно черги з кнопкою видно');
  check(/Є активне підключення/.test(ctx.el('veil-text').textContent),
        `напис називає причину: «${ctx.el('veil-text').textContent}»`);

  const before = ctx.g.__timers.filter((ms) => ms === 1000).length;
  global.__ws.onclose();
  check(ctx.g.__timers.filter((ms) => ms === 1000).length === before,
        '⚠️ і головне: перепідключення НЕ заводиться — це й була гойдалка');

  // --- дві кнопки, і забирає тільки друга ------------------------------------
  gate('confirm');
  check(/буде відключений/.test(ctx.el('veil-text').textContent),
        `попередження називає наслідок: «${ctx.el('veil-text').textContent}»`);
  check(ctx.el('btn-take-yes').hidden === false && ctx.el('btn-take').hidden === true,
        'на другому кроці видно «Підключитись», а не «Перейняти керування»');

  gate('ask');
  check(queueState().queued === true, '«Скасувати» лишає нас у черзі');

  // ⚠️ Подвійне натискання «Підключитись» не має лишати двох живих сокетів:
  // із тим самим іменем вони витісняли б одне одного через міст без кінця —
  // гойдалка з **одного** клієнта, знайдена рецензією.
  const stale = global.__ws;
  let staleClosed = false;
  stale.close = () => { staleClosed = true; };

  connect({ take: true });
  check(staleClosed === true, 'нове під’єднання гасить попередній сокет');
  check(stale.onclose === null,
        '⚠️ і знімає з нього обробники — інакше його ж onclose завів би ще одне');
  check(/take=1/.test(urls[urls.length - 1]),
        `переймання заявлене прямо: ${urls[urls.length - 1]}`);
  check(!/resume=1/.test(urls[urls.length - 1]),
        'і не прикидається поверненням свого');
  check(queueState().queued === false, 'із черги вийшли');
}

// ------------------------- 9. місце під екран до першого кадру -------------
//
// ⚠️ Перевіряється обчислення, а не стилі. Стилі тут однаково нічого не
// доводять — заглушка DOM віддає ті самі числа всім елементам, — а розкладка
// стоїть або падає саме на цих числах: скільки місця під зображення й звідки
// воно взялось. Геометрію живої сторінки міряє `tools/webui_built_check.py`.

console.log('місце під екран тримається до першого кадру (задача 0025)');
{
  const ctx = makeApp();
  const { screenBox, knownSize, resizeTo } = ctx.app;

  check(knownSize().w === 0 && knownSize().h === 0,
        'перший у житті візит: пульта ще не бачили');

  // ⚠️ Пропорція не вигадується: беремо все вільне місце. Вигадана виглядала
  // б як правда — і на чужому пульті була б неправдою (критерій 1.3).
  const first = screenBox(600, 300);
  check(first.w === 600 && first.h === 300,
        `без пригаданого — усе вільне місце: ${first.w}×${first.h}`);

  // Пульт привітався. Тепер розмір справжній, і він же лягає в сховище.
  resizeTo(320, 240);
  const live = screenBox(600, 300);
  check(live.w === 400 && live.h === 300,
        `з HELLO працює пропорція пульта: ${live.w}×${live.h}`);
  check(ctx.g.__store.get('remoteui.screen.w') === '320'
        && ctx.g.__store.get('remoteui.screen.h') === '240',
        'розмір записано в сховище');

  // Друге відкриття сторінки: те саме сховище, з'єднання ще немає.
  const again = makeApp(ctx.g.__store);
  check(again.app.knownSize().w === 320 && again.app.knownSize().h === 240,
        'розмір пригадано з минулого сеансу');
  const before = again.app.screenBox(600, 300);
  check(before.w === live.w && before.h === live.h,
        `до з'єднання місце те саме, що після: ${before.w}×${before.h} `
        + `проти ${live.w}×${live.h}`);

  // ⚠️ Пригадане — не істина, а здогад про **той самий** пульт. Прийшов
  // HELLO з іншими числами — старшим є він (критерій 1.5).
  again.app.resizeTo(128, 72);
  const other = again.app.screenBox(600, 300);
  check(other.w === 533 && other.h === 300,
        `інший пульт перебудував розкладку під себе: ${other.w}×${other.h}`);
  check(again.g.__store.get('remoteui.screen.w') === '128'
        && again.g.__store.get('remoteui.screen.h') === '72',
        'і пригадане оновилось під нього');
}

/* ⚠️ Сміття у сховищі — не вигадка про зловмисника, а вада, яку знайшла
 * рецензія коду й заміряла на живій сторінці: `parseInt` брав префікс, і
 * «999999999999» давало колонку на 33 мільйони пікселів — чорна порожнеча без
 * панелей і без напису. Найгірше, що з неї **не було виходу**: значення
 * лишається у сховищі, і кожне наступне відкриття дає те саме, доки не
 * почистити дані сайту. На телефоні наосліп цього не роблять. */
console.log('сміття у сховищі не ламає розкладки (рецензія 0025)');
for (const [w, h] of [['999999999999', '1'], ['12abc', '9'], ['-480', '272'],
                      ['480.5', '272'], ['', ''], ['0', '0']]) {
  const store = new Map([['remoteui.screen.w', w], ['remoteui.screen.h', h]]);
  const ctx = makeApp(store);
  const box = ctx.app.screenBox(600, 300);
  check(ctx.app.knownSize().w === 0 && ctx.app.knownSize().h === 0
        && box.w === 600 && box.h === 300,
        `«${w}»×«${h}» не пригадується: ${box.w}×${box.h}`);
}

// ----------------------------------------------------------------------------

console.log(`\nперевірок ${checks}, невдалих ${failed}`);
process.exit(failed ? 1 : 0);
