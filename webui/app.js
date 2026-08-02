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
const Wait = RemoteUIWait;

const RECONNECT_MS = 1000;

/** Як часто повторювати вітальний `PING`, доки пульт не відповів `HELLO`. */
const GREET_RETRY_MS = 500;

/* --- лікування плиток, які відкинув міст ---------------------------------
 *
 * ⚠️ Ухвалене «міст відкинув — клієнт потім попросить REFRESH» саме по собі
 * не працює: ніхто не питає. `FRAME_END` доходить, клієнт показує кадр як
 * цілісний, а пульт ту плитку більше не надішле — він шле **лише зміни**.
 * Прямокутник зі старими пікселями лишається на екрані назавжди.
 *
 * Тому просимо самі, за лічильником `packets_dropped` із `/api/stats`. Але
 * не одразу: доки екран рухається, наступні кадри однаково перемалюють те
 * місце, а `REFRESH` на 20–40 КБ у той самий затор зробить лише гірше.
 * Чекаємо, доки картинка **вгамується**, і придушуємо частоту — інакше
 * `REFRESH` годуватиме сам себе.
 */
const BRIDGE_POLL_MS = 1000;
const QUIET_BEFORE_REFRESH_MS = 500;
const REFRESH_MIN_GAP_MS = 2000;

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
  autoRefresh: 0,   // скільки разів просили REFRESH через втрати на мості

  // Звідки взявся кожен показаний кадр — взаємовиключні причини, сума
  // дорівнює `frames` рівно.
  framesWhole: 0,     // FRAME_END сказав dirtyTiles = 0: кадр цілісний
  framesTimeout: 0,   // решта не доїхала за строк — показали як є
  framesTooMany: 0,   // чекати не було сенсу: залишок більший за поріг
  framesNoWait: 0,    // очікування вимкнене кнопкою — режим «як було»
  framesDrag: 0,      // палець веде: рух важливіший за шов (задача 0020)
  framesLegacy: 0,    // прошивка без ознаки повноти (до задачі 0019)
};

/* Причина показу з автомата → лічильник. ⚠️ Таблицею, а не ланцюжком `if`:
 * нова причина без рядка тут упаде голосно (кадр не порахується жодною
 * причиною, і рівність «сума = кадрів» розсиплеться на очах), а не приховає
 * себе під чужим числом. */
const WHY_COUNTER = {
  [Wait.WHY_WHOLE]: 'framesWhole',
  [Wait.WHY_TIMEOUT]: 'framesTimeout',
  [Wait.WHY_TOO_MANY]: 'framesTooMany',
  [Wait.WHY_OFF]: 'framesNoWait',
  [Wait.WHY_DRAG]: 'framesDrag',
  [Wait.WHY_LEGACY]: 'framesLegacy',
};

/**
 * Обнулити все, що описує **показ кадрів**, — при зміні режиму очікування.
 *
 * ⚠️ Гасяться рівно ті лічильники, що входять у рівність «сума причин =
 * `frames`», плюс мітки плашки. Пропустити мітки означало б показати на одну
 * секунду від'ємну частоту; пропустити `frames` — розсипати рівність, яку
 * стереже тест. Решта лічильників (плитки, CRC, розриви) описують **канал**,
 * а не режим показу, і обнуляти їх було б стиранням доказів.
 */
function resetFrameCounters() {
  counters.frames = 0;
  counters.framesWhole = 0;
  counters.framesTimeout = 0;
  counters.framesTooMany = 0;
  counters.framesNoWait = 0;
  counters.framesDrag = 0;
  counters.framesLegacy = 0;
  lastFrames = 0;
  lastWhole = 0;
  latencyReset();
}

/* ⚠️ Очікування перемикається кнопкою — це **прилад**, а не налаштування.
 *
 * Розлам і ривки неможливо порівняти по пам'яті: режими треба побачити
 * поспіль, на тому самому меню. Клієнт живе в бінарнику моста, тож без цієї
 * кнопки кожне порівняння коштувало б перепрошивання.
 *
 * Режимів три, і третій з'явився не з міркування, а з заміру на живому пульті
 * (2026-08-01, критерій 4.1 задачі 0019). Замір показав, що поріг вимикає
 * очікування саме на великих перемальовках — тобто рівно там, де шов видно
 * оком, — і що `показано за строком` не трапляється **жодного разу** ні при
 * якому порозі. Тобто загроза, від якої поріг ставили («безперервне гортання
 * дасть ~4 кадр/с зшитими кадрами»), на цьому залізі не справдилась.
 *
 * `ЗАВЖДИ` — чесна протилежність до `ВИМК`: порога немає зовсім, від патології
 * лишається сама стеля очікування. Ціна заміряна: 8.8 → 6.6 показів/с на
 * гортанні за те, що жоден показаний кадр не зшитий. Що з цього краще —
 * вирішує око, і саме для цього тут три положення, а не два.
 *
 * ⚠️ Самі числа режимів лежать у `wait.js` (`Wait.WAIT_OFF` і далі): тут
 * лишились тільки написи, які бачить людина.
 *
 * ⚠️ Режими описані **таблицею**, а не трьома ланцюжками `if` у трьох місцях.
 * Наступна ручка (стеля очікування вже названа кандидатом) додала б четверте
 * положення, і `% 3` зламався б **мовчки**: режим просто став би недосяжним,
 * без жодної ознаки. */
const WAIT_MODES = [
  { label: () => 'чек: вимк',
    title: () => 'очікування вимкнене — показ на кожному FRAME_END, '
               + 'як до задачі 0019' },
  { label: (n) => n === null ? 'чек: до ?' : `чек: до ${n}`,
    title: (n) => n === null
      ? 'чекаю цілого кадру; поріг буде відомий після HELLO'
      : `чекаю цілого кадру, поки в дорозі не більше ${n} плиток` },
  { label: () => 'чек: завжди',
    title: () => 'чекаю цілого кадру завжди, порога немає — '
               + `зупиняє лише стеля ${P.FRAME_WAIT_MAX_MS} мс` },
];

/* ⚠️ Типове — `завжди`, і поріг лишається тільки як положення кнопки.
 *
 * Рішення людини за критерієм 4.1 задачі 0019, ухвалене **після** заміру на
 * живому пульті, а не замість нього. Поріг як важіль закритий числами:
 * `dirtyTiles` не наближається до розміру сітки (135) в жодному режимі
 * керування — стеля залишку близько 96, бо пульт устигає віддати решту ще до
 * закриття кадру. Заміряні максимуми: гортання енкодером неспішно 83, швидко
 * 94, перескок сторінки 89.
 *
 * Наслідок, який варто знати, перш ніж знову крутити це число: **режими
 * керування не розділяються порогом** — вони лежать в одному діапазоні 80–96.
 * Будь-який поріг від 100 тотожний `завжди`, будь-який менший ріже гортання
 * разом зі зміною сторінки. Проміжного числа немає в даних.
 *
 * Ціна, названа людиною і записана свідомо: на протягу пальцем по сенсору
 * очікування додає ривків. Важіль, який це розв'язав, — не число, а джерело
 * вводу, і він живе в `wait.js` (задача 0020): поріг лишається як був, а
 * протяг пальцем його просто обходить. */

/* ⚠️ Автомат очікування живе в `wait.js` — окремому модулі **без DOM**. Тут
 * лишається сама обгортка: таймер, лічильники, написи.
 *
 * Причина винесення названа рецензією 0019: `app.js` не покритий нічим, і
 * дзеркальність із клієнтом ПК трималась на читанні очима. Тепер правила
 * перевіряються двічі — `webui/wait_test.js` каже, що правило те, а
 * `tools/wait_crosscheck.py` — що правило одне на два клієнти. */
const policy = new Wait.FrameWaitPolicy({ mode: Wait.WAIT_ALWAYS, baud: null });

/* --- скільки головний потік зайнятий нами ---------------------------------
 *
 * ⚠️ Прилад, без якого «міст не встигає віддати» неможливо відрізнити від
 * «клієнт не встигає забрати». Різниця не тонка: приймання і малювання тут
 * живуть в **одному** потоці, і поки він рахує, сокет ніхто не вичитує —
 * вікно TCP зачиняється, міст упирається в нас і викидає пачки. Знадвору це
 * виглядає точно як вузький ефір.
 *
 * Задача 0018 заміряла 123 КБ/с у телефон проти 155 КБ/с потреби і мало не
 * записала винним Wi-Fi. Стенд `tools/webui_bench.js` показав, що розбір і
 * малювання плиток коштують у сто разів менше, ніж потрібно, — отже, якщо
 * винен клієнт, то `putImageData` або стек браузера, а їх без браузера не
 * поміряти. Оце їх і міряє.
 *
 * `busy` читати як частку секунди: 1.0 означає, що головний потік не
 * простоював зовсім.
 */
const prof = {
  msFeed: 0,    // весь обробник повідомлення WebSocket, разом із плитками
  msTile: 0,    // з нього — decodeTile + blitTile
  msDraw: 0,    // putImageData
  bytes: 0,
  // Останній зведений знімок за секунду — його показує панель.
  last: { feed: 0, tile: 0, draw: 0, kbs: 0, busy: 0 },
};

/* --- скільки чекає палець --------------------------------------------------
 *
 * ⚠️ Прилад, який закриває борг 4.2 задачі 0019, і закрити його могло **лише**
 * тут. `tools/input_check.py` міряє затримку до дроту, а очікування, яке ми
 * щойно вимкнули для протягу, лежить **після** дроту — тобто саме те, що
 * відчуває палець, той інструмент не бачить за побудовою.
 *
 * Що вважаємо затримкою: від миті, коли клієнт **надіслав** ввід, до миті,
 * коли він **показав** наступний кадр. Береться найстаріший ввід, на який ще
 * не відповіли кадром, а не останній: питання буквально «скільки минуло, доки
 * моя дія з'явилась на екрані», а найсвіжіший ввід занизив би відповідь тим
 * сильніше, чим гірше йдуть справи.
 *
 * Дотик і енкодер рахуються **окремо** — у них тепер різні правила показу, і
 * зведене число ховало б рівно ту різницю, заради якої задача існує.
 *
 * ⚠️ **Нижня межа обов'язкова, інакше прилад систематично занижує.** Кадр,
 * який уже був у дорозі, коли ми надіслали ввід, відповіддю на нього не є —
 * пульт перемальовує й сам (телеметрія, таймери), і такий кадр дав би зразок у
 * кілька мілісекунд. Зсув при цьому **несиметричний**: у режимі протягу кадри
 * йдуть частіше, тобто хибних дешевих зразків там більше — і саме порівняння
 * «дотик проти енкодера» отримало б домішку на свою користь.
 *
 * Межа не вигадана: LVGL опитує сенсор раз на 30 мс
 * (`LV_INDEV_DEF_READ_PERIOD`, `colorlcd/lv_conf.h:105`), тож швидше за один
 * період відповіді не буває взагалі. Усе, що прийшло раніше, — чужий кадр, і
 * чекаємо далі, а не зараховуємо. Скільки таких відкинуто — видно в панелі:
 * прилад, який мовчки щось викидає, довіри не вартий.
 */
const LATENCY_KEEP = 600;   // зразків на джерело; більше нема сенсу тримати
const LATENCY_FLOOR_MS = 30;

const latency = {
  pendingAt: 0,
  pendingSource: null,   // null = усі надіслані вводи вже відповіли кадром
  early: 0,              // кадрів, відкинутих як «був у дорозі»
  samples: { touch: [], enc: [], key: [] },
};

function latencyReset() {
  latency.pendingSource = null;
  latency.early = 0;
  latency.samples = { touch: [], enc: [], key: [] };
}

function latencyNoteInput(source, now) {
  if (latency.pendingSource !== null) return;   // вже чекаємо на старіший
  latency.pendingSource = source;
  latency.pendingAt = now;
}

function latencyNoteShown(now) {
  if (latency.pendingSource === null) return;

  const dt = now - latency.pendingAt;
  // Кадр був у дорозі ще до нашого вводу — відповіддю бути не міг. Заявку
  // **не** знімаємо: чекаємо на справжню відповідь.
  if (dt < LATENCY_FLOOR_MS) { latency.early++; return; }

  const a = latency.samples[latency.pendingSource];
  if (a) {
    a.push(dt);
    if (a.length > LATENCY_KEEP) a.shift();
  }
  latency.pendingSource = null;
}

/** Медіана, 90-й відсоток і максимум. ⚠️ Розкид обов'язковий: середнє тут
 *  бреше — саме хвіст і є те, що людина називає ривком. */
function latencyStats(source) {
  const a = latency.samples[source];
  if (!a || !a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const at = (q) => s[Math.min(s.length - 1, Math.floor(q * s.length))];
  return { n: s.length, med: at(0.5), p90: at(0.9), max: s[s.length - 1] };
}

function resizeTo(w, h) {
  // Буфер зараз підміниться на порожній — відкладений показ малював би вже не
  // той кадр, якого чекав. Разом із кадром помирає й низка очікування.
  forgetPendingFrame();

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

  const t0 = performance.now();
  const tile = P.decodeTile(payload);
  if (tile) {
    if (P.blitTile(frameBuf.data, W, H, tile)) counters.tiles++;
    else counters.outOfBounds++;
  } else {
    counters.badTiles++;
  }
  prof.msTile += performance.now() - t0;
}

/* Кадр показуємо лише на FRAME_END: до нього картинка неповна. Саме тому
 * клієнт накопичує між кадрами — і саме тому він придатний, щоб подивитись
 * на сторінку, яка оновлюється безперервно. */
function showFrame(why) {
  if (!frameBuf) return;
  const t0 = performance.now();
  ctx.putImageData(frameBuf, 0, 0);
  const t1 = performance.now();
  prof.msDraw += t1 - t0;

  counters.frames++;
  // ⚠️ Причина обов'язкова. Без цієї перевірки невідома причина дала б
  // `counters[undefined] = NaN` — тобто рівність «сума причин = кадрів»
  // розсипалась би мовчки, а в браузері її не стереже жоден тест.
  const which = WHY_COUNTER[why];
  if (!which) throw new Error('показ кадру без відомої причини: ' + why);
  counters[which]++;
  policy.noteShown();
  latencyNoteShown(t1);

  lastFrameAt = t1;
  veil(null);
}

/* --- кадр, який ще не цілий ------------------------------------------------
 *
 * `FRAME_END` каже, скільки плиток лишилось незасланими. Пульт закриває кадр за
 * подією «LVGL домалював», не чекаючи, доїхали всі плитки чи ні, — і на великій
 * перемальовці лишається приблизно половина. Показаний такий кадр — це картинка,
 * зшита з двох митей: на гортанні списку людина бачить дві рамки виділення
 * одночасно.
 *
 * Момент закриття кадру в прошивці навмисний і не міняється (без нього картинка
 * застигала б саме тоді, коли рухається). Міняється те, що клієнт про кадр
 * **знає**: при N > 0 він чекає доїзду решти, і показує зшите лише тоді, коли
 * решта не доїхала за строк.
 */
let pendingFrameTimer = null;

/* ⚠️ Дві різні дії, і плутати їх не можна.
 *
 * `cancelPendingFrame()` — тільки таймер. Кличеться в звичайному ході
 * `onFrameEnd()`, де низка очікування **живе далі**: скинути там її запас
 * означало б зробити стелю нескінченною.
 *
 * `forgetPendingFrame()` — таймер **і** низка. Кличеться там, де чекати вже
 * нема на що: розмір екрана змінився або з'єднання померло.
 *
 * Другого не було, і клієнт ПК від браузера через це розходився (`forget_pending`
 * там кликав автомат, тут — ні): `pending` переживав розрив, і перший же
 * неповний кадр після перепідключення йшов зшитим у лічильник «за строком».
 */
function cancelPendingFrame() {
  if (pendingFrameTimer !== null) {
    clearTimeout(pendingFrameTimer);
    pendingFrameTimer = null;
  }
}

function forgetPendingFrame() {
  cancelPendingFrame();
  policy.forget();
}

function onFrameEnd(payload) {
  // FRAME_END до HELLO законний рівно так само, як плитка до нього: над TCP
  // пульт вітається сам. Класти пікселі нікуди, тож і чекати нема на що —
  // інакше звели б таймер, який за 50–250 мс покличе показ у порожнечу.
  if (!frameBuf) return;

  // ⚠️ Усе рішення — в одному виклику автомата. Розкладка правил, числа й
  // обґрунтування живуть у `wait.js`; тут лишається виконання: скасувати
  // старий таймер, показати або завести новий.
  const r = policy.decide(P.parseFrameEnd(payload), performance.now());

  cancelPendingFrame();

  if (r.show) {
    // ⚠️ Показ **у цьому ж обробнику**, без setTimeout і без
    // requestAnimationFrame: будь-яке відкладання тут — це чисте зростання
    // затримки від дотику до зміни на екрані.
    showFrame(r.why);
    return;
  }

  pendingFrameTimer = setTimeout(() => {
    pendingFrameTimer = null;
    showFrame(Wait.WHY_TIMEOUT);
  }, r.waitMs);

  // Плитки тим часом як лягали у frameBuf, так і лягають: накопичення не
  // спиняється ніколи, чекає лише показ.
}

// ------------------------------------------------------------ з'єднання ----

let ws = null;
let hello = null;
const decoder = new P.Decoder();
const mirror = new P.InputMirror();

let lastHelloAt = 0;
let lastFrameAt = 0;
let holdTimer = null, pingTimer = null, greetTimer = null, reconnectTimer = null;

/* Стан моста: тягнеться раз на секунду й використовується і для лікування
 * втрачених плиток, і для панелі стану — щоб не питати двічі. */
let bridgeStats = null;
let lastDropped = null;
let lastRefreshAt = 0;

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

// ------------------------------------------------- швидкість каналу --------
//
// ⚠️ Клієнт тут нічого не вирішує і нічого не знає наперед. Перелік приходить
// із пульта в `HELLO`, бо залежить від тактової шини конкретного пульта;
// дозволяє швидкість теж пульт; виконує перемикання міст. Наша справа —
// показати, що є, і передати вибір.
//
// Через це ж у файлі немає жодного числа швидкості: вписане тут, воно одного
// дня розійшлося б із прошивкою й показувало б людині неіснуючий вибір.

let baudNonce = 0;
let baudPending = null;   // на що чекаємо відповіді
let baudNoteTimer = null;

/* --- клієнт сам просить найшвидше, що пульт дозволяє ----------------------
 *
 * ⚠️ Не зручність, а лікування стабільного джерела хибних скарг. Після
 * **кожного** перезапуску стенда пульт удома на 921 600 (запобіжник відкоту
 * 0017 працює як має), а на цій швидкості строк очікування спрацьовує на
 * кожному третьому кадрі й людина бачить шви. Заміряно 2026-08-02: 137 цілих
 * кадрів зі 137 на 2 625 000 проти 75 зі 121 на 921 600. Тобто «знову рве» —
 * це швидкість, а не регресія, і поки перемикач треба чіпати рукою, цю
 * плутанину доводиться щоразу розплутувати наново.
 *
 * ⚠️ Прапорець **один**, і знімає його лише підтверджений успіх.
 *
 * Спокуслива пара «спробував на цьому з'єднанні» + «відмовили — більше не
 * прошу» має дірку, і вона не теоретична: спроба може не закінчитись **жодним
 * звітом** — обрив WebSocket, перезавантаження моста, кинутий Wi-Fi. Тоді
 * «відмовили» не спрацьовує ніколи, а «на цьому з'єднанні» обнуляється кожним
 * перепідключенням, тобто раз на секунду. Виходить рівно той цикл
 * «прошу → відкат → прошу», який забороняє критерій 5.2.
 *
 * Тому прапорець ставиться **в мить спроби**, а не за її наслідком. Правило
 * читається так: «одна спроба на завантаження сторінки, доки вона не
 * вдалася». Успіх його знімає — і після наступного розриву, коли пульт знову
 * вдома на 921 600, клієнт попросить ще раз.
 */
let baudAutoAsked = false;

function maybeAskBestBaud(h) {
  if (baudAutoAsked) return;

  if (!h.canSwitchBaud || !h.baudList.length || !h.baudCurrent) return;
  const best = Math.max.apply(null, h.baudList);
  if (best <= h.baudCurrent) return;   // уже на найшвидшій — просити нічого

  baudNonce = (baudNonce % 255) + 1;
  baudPending = { target: best, nonce: baudNonce, auto: true };
  baudAutoAsked = true;   // ⚠️ у мить спроби, а не за її наслідком — див. вище
  send(P.encodeBaudSet(best, baudNonce));
  // ⚠️ Автоматична спроба видима так само, як ручна: мовчазне перемикання
  // швидкості — це число, яке людина читає з плашки й на яке спирає замір.
  baudNote(`прошу ${baudLabel(best)} сам…`);
}

function baudLabel(v) {
  return v >= 1000000 ? `${(v / 1000000).toFixed(v % 1000000 ? 2 : 0)} Мбод`
                      : `${Math.round(v / 1000)}k`;
}

function baudNote(text, cls) {
  chip('link', text, cls);
  clearTimeout(baudNoteTimer);
  // Напис не висить вічно: за кілька секунд повертаємо звичайний стан зв'язку,
  // інакше «перемикаюсь…» лишилось би на екрані назавжди.
  baudNoteTimer = setTimeout(() => {
    chip('link', ws && ws.readyState === WebSocket.OPEN ? "зв'язок є" : 'немає зв\'язку',
         ws && ws.readyState === WebSocket.OPEN ? 'ok' : 'bad');
  }, 4000);
}

function baudRefresh(h) {
  const wrap = document.getElementById('baud-wrap');
  const sel = document.getElementById('baud');

  // Порожній перелік — єдина ознака «перемикання тут немає». Ховаємо орган
  // керування цілком: показувати непрацездатну випадайку гірше, ніж не
  // показувати нічого.
  if (!h.canSwitchBaud) { wrap.hidden = true; return; }

  wrap.hidden = false;
  const want = h.baudList.join(',');
  if (sel.dataset.list !== want) {
    sel.dataset.list = want;
    sel.innerHTML = '';
    for (const b of h.baudList) {
      const o = document.createElement('option');
      o.value = String(b);
      o.textContent = baudLabel(b) + (b === h.baudHome ? ' (базова)' : '');
      sel.appendChild(o);
    }
  }
  if (h.baudCurrent) sel.value = String(h.baudCurrent);
}

function onBaudPick(ev) {
  const target = Number(ev.target.value);
  if (!target || (hello && hello.baudCurrent === target)) return;

  baudNonce = (baudNonce % 255) + 1;
  baudPending = { target, nonce: baudNonce };
  send(P.encodeBaudSet(target, baudNonce));
  baudNote(`перемикаю на ${baudLabel(target)}…`);
}

function onBaudReport(payload) {
  const r = P.parseBaud(payload);
  if (!r) return;

  // Чужий nonce — відповідь на стару команду, що доїхала із запізненням.
  // Прийняти її за свою означало б розійтися з пультом, не помітивши цього.
  // ⚠️ Виняток — незапрошений звіт (nonce 0): його ніхто не замовляв, і саме
  // ним пульт повідомляє, що дослід провалився.
  if (r.nonce !== 0 && baudPending && r.nonce !== baudPending.nonce) return;

  if (r.verdict === P.BAUD_ACCEPTED) {
    // Ще не успіх: пульт лише пообіцяв. Успіхом буде наступний HELLO із
    // новою поточною швидкістю.
    baudNote(`пульт перемикається на ${baudLabel(r.target)}…`);
    return;
  }

  baudPending = null;

  // ⚠️ Невдале перемикання показане як невдале. Мовчазне повернення на
  // попередню швидкість без напису — вада, а не поведінка: людина натиснула,
  // нічого не змінилось, і вона не знає чому.
  baudNote(r.text, 'bad');
  if (hello && hello.baudCurrent) {
    document.getElementById('baud').value = String(hello.baudCurrent);
  }
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
    policy.setBaud(null);   // швидкість знову невідома — і поріг разом із нею
    // ⚠️ Протухла заявка не переживає розрив: інакше сторонній HELLO з новою
    // швидкістю після перепідключення був би визнаний «нашим успіхом».
    baudPending = null;
    // Поріг щойно став невідомим: без цього кнопка лишалась би з числом
    // мертвого з'єднання — на 921600 показувала б 45 замість 16.
    waitLabel();
    veil('вітаюсь із пультом…');
    chip('link', 'канал є', 'warn');
    lastHelloAt = performance.now();
    startGreeting();
  };

  /* ⚠️ Увесь розбір і все малювання відбуваються **тут**, у головному
   * потоці. Поки цей обробник виконується, браузер сокет не вичитує — тому
   * час, витрачений тут, безпосередньо звужує вікно TCP. Саме це й міряємо. */
  ws.onmessage = (ev) => {
    const data = new Uint8Array(ev.data);
    const t0 = performance.now();
    decoder.feed(data, onPacket);
    prof.msFeed += performance.now() - t0;
    prof.bytes += data.length;
  };

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
  clearInterval(greetTimer); greetTimer = null;

  // Відкладений неповний кадр помирає разом зі з'єднанням: решта плиток уже не
  // доїде ніколи, а показувати зшите поверх завіси «зв'язок обірвано» —
  // брехати про те, що зв'язок є. Низка очікування помирає разом із ним —
  // інакше її запас переживе розрив і з'їсть перший кадр після повернення.
  forgetPendingFrame();
}

/**
 * Вітання, яке повторюється, доки пульт не відповість.
 *
 * ⚠️ Одного `PING` замало, і це не педантизм. Над послідовним портом пульт
 * мовчить у порожній канал, доки не почує клієнта, — тобто **єдиний**
 * привітальний пакет є єдиною подією, яка взагалі запускає розмову. Якщо він
 * загубиться (пульт ще вантажиться, завада на дроті, побитий CRC), клієнт
 * чекатиме вічно, показуючи «вітаюсь із пультом…», і виглядатиме це як
 * несправний міст.
 *
 * Ціна повтору — 7 байтів раз на пів секунди, і лише доки не прийшов `HELLO`.
 */
function startGreeting() {
  let tries = 0;
  const beat = () => {
    if (hello) { clearInterval(greetTimer); greetTimer = null; return; }
    tries++;
    send(P.encodeFrame(P.PKT_PING));

    // Через кілька спроб перестаємо бути ввічливими й кажемо, де шукати.
    if (tries === 6) {
      veil('пульт не відповідає — перевір дроти й живлення');
      chip('link', 'пульт мовчить', 'bad');
    }
  };

  clearInterval(greetTimer);
  greetTimer = setInterval(beat, GREET_RETRY_MS);
  beat();   // перший — одразу, без очікування
}

function onPacket(type, payload) {
  switch (type) {
    case P.PKT_BAUD:
      onBaudReport(payload);
      break;

    case P.PKT_HELLO: {
      const h = P.parseHello(payload);
      if (!h) break;   // обрізаний HELLO — відкинути, а не вгадувати розмір
      lastHelloAt = performance.now();

      const first = !hello;
      if (!hello || hello.width !== h.width || hello.height !== h.height) {
        resizeTo(h.width, h.height);
      }
      hello = h;
      // Поріг автомата рахується зі швидкості, а вона міняється на ходу.
      policy.setBaud(h.baudCurrent);
      baudRefresh(h);
      // Поріг залежить від швидкості каналу, а вона міняється на ходу —
      // напис на кнопці має їхати за нею.
      waitLabel();

      // Успіхом перемикання вважається саме це: пульт назвав нову поточну
      // швидкість у HELLO. Обіцянка в підтвердженні успіхом не була.
      if (baudPending && h.baudCurrent === baudPending.target) {
        baudNote(`швидкість каналу: ${baudLabel(h.baudCurrent)}`, 'ok');
        // Успіх — і тільки він — знімає заборону просити знову. Після
        // наступного розриву пульт буде вдома на базовій, і попросити треба.
        if (baudPending.auto) baudAutoAsked = false;
        // ⚠️ Лічильники й затримки гасяться разом зі швидкістю: критерії 3.2 і
        // 3.3 вимагають чисел **окремо** на кожній швидкості, а панель, яка і є
        // артефактом доказу, мовчки показала б суміш двох.
        resetFrameCounters();
        // ⚠️ Після зміни швидкості просимо картинку наново: плитки, що були в
        // дорозі в мить перемикання, доїхати не могли, а пульт шле **лише
        // зміни** — прямокутник зі старими пікселями лишився б назавжди.
        send(P.encodeFrame(P.PKT_REFRESH));
        baudPending = null;
      }

      if (first) {
        chip('link', "зв'язок є", 'ok');
        chip('radio', `${h.target} ${h.fw} · ${h.width}×${h.height}` +
                      (h.hasInputState ? '' : ' · стара прошивка'));
        startTimers();
        veil('чекаю перший кадр…');
        // Тільки тепер REFRESH: до HELLO клієнт не знає, куди класти пікселі.
        send(P.encodeFrame(P.PKT_REFRESH));
        maybeAskBestBaud(h);
      }
      break;
    }

    case P.PKT_TILE:
      onTile(payload);
      break;

    case P.PKT_FRAME_END:
      onFrameEnd(payload);
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

/* Один вхід для всього вводу, який клієнт надсилає **сам**.
 *
 * ⚠️ Периодичний `INPUT_STATE` сюди не входить і входити не має: він повторює
 * рівень, а не є новою дією людини. Інакше палець, покладений на екран і
 * забутий там, нескінченно подовжував би вікно «не чекати» — уже після того,
 * як усе зупинилось. Те саме про `PING` і `REFRESH`. */
function noteInput(source, opts) {
  const now = performance.now();
  policy.noteInput(source, now, opts);
  latencyNoteInput(source, now);
}

function sendTouch(event, pt) {
  // ⚠️ Спершу дзеркало, потім перехід — інакше рівень, який піде наступним
  // INPUT_STATE, суперечитиме щойно надісланому переходу.
  mirror.touch(event, pt.x, pt.y);
  send(P.encodeTouch(event, pt.x, pt.y));
  noteInput(Wait.SRC_TOUCH, {
    phase: event === P.TOUCH_DOWN ? 'down'
         : event === P.TOUCH_UP ? 'up' : 'move',
    x: pt.x, y: pt.y,
  });
}

let pointerId = null;

canvas.addEventListener('pointerdown', (ev) => {
  // ⚠️ Тільки основна кнопка. Палець і стилус дають `button === 0` так само,
  // як ліва кнопка миші, а права зайнята клавішею «назад» — без цієї умови
  // вона слала б у пульт ще й дотик у ту саму точку.
  if (ev.button !== 0) return;
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
  // Мишу браузер віддає одним `pointerId` на всі кнопки, тож без перевірки
  // відпускання правої закривало б дотик, початий лівою.
  if (ev.button !== 0 && ev.type === 'pointerup') return;
  if (ev.pointerId !== pointerId) return;
  sendTouch(P.TOUCH_UP, toRadio(ev));
  pointerId = null;
  ev.preventDefault();
}

canvas.addEventListener('pointerup', endTouch);
canvas.addEventListener('pointercancel', endTouch);

/* --- Права кнопка миші як «назад» ------------------------------------------
 *
 * ⚠️ Код клавіші **не вписаний числом**: правило проєкту — специфіки
 * конкретного пульта в коді нуль рядків. Клавіша шукається за міткою, яку
 * пульт сам назвав у `HELLO` (`keysGetLabel`). На TX16S це `RTN`, на інших
 * цілях мітка може бути інша — тому перелік, а не одне слово.
 *
 * Не знайшлась — кнопка просто нічого не робить: вигадувати код навмання
 * означало б натиснути на чужому пульті випадкову клавішу.
 */
const BACK_KEY_LABELS = ['RTN', 'EXIT'];

function backKeyCode() {
  if (!hello) return null;
  const k = hello.keys.find((k) => BACK_KEY_LABELS.includes(k.name));
  return k ? k.code : null;
}

function sendBack(pressed) {
  const code = backKeyCode();
  if (code === null) return;
  mirror.key(code, pressed);       // рівень — інакше повтор стану її «відпустить»
  send(P.encodeKey(code, pressed));
  noteInput(Wait.SRC_KEY);
}

canvas.addEventListener('pointerdown', (ev) => {
  if (ev.button !== 2) return;
  sendBack(true);
  ev.preventDefault();
});

function releaseBack(ev) {
  if (ev.button !== 2) return;
  sendBack(false);
  ev.preventDefault();
}

canvas.addEventListener('pointerup', releaseBack);
canvas.addEventListener('pointercancel', releaseBack);

/* Меню правої кнопки над картинкою тільки заважає: клавіша пульта тут
 * важливіша за пункт «зберегти зображення». */
canvas.addEventListener('contextmenu', (ev) => ev.preventDefault());

/* --- Колесо миші як енкодер ------------------------------------------------
 *
 * Напрямок узгоджений із клієнтом ПК (`tools/test_client.py:642`): колесо вниз
 * — це `+1`, вгору — `-1`. Розійтись тут не можна, бо людина ганяє те саме
 * меню то з вікна ПК, то з браузера.
 *
 * ⚠️ **Одна подія колеса = рівно одне клацання, хоч би що казала `deltaY`.**
 * Це не спрощення, а виправлення: величина `deltaY` **не** означає кількості
 * зарубок і в різних браузерах різна за побудовою — Firefox шле `deltaMode`
 * «рядки» з `deltaY = 3`, Chrome — «пікселі» з `deltaY ≈ 100`. Переклад
 * «стільки одиниць — стільки клацань» перетворював одну зарубку на два-три
 * клацання в одному пакеті, і меню стрибало через пункти.
 *
 * Накопичення лишилось **тільки для тачпада**: він сипле десятками дрібних
 * дельт на один рух пальцем, і там одна подія клацанням бути не може.
 *
 * Енкодер лишається **накопичувальним приростом**, а не рівнем, і в повтор
 * стану (`INPUT_STATE`) не входить — записане рішення від 2026-07-26: втрата
 * клацання коштує клацання, а рівень дав би фантомний оберт назад. */
const WHEEL_NOTCH_PX = 25;   // від цього модуля дельта вважається зарубкою
const WHEEL_TRACKPAD_PX = 40;  // скільки дрібних пікселів складають клацання

let wheelAcc = 0;

canvas.addEventListener('wheel', (ev) => {
  if (hello && !hello.hasEncoder) return;
  ev.preventDefault();          // інакше сторінка поїде під картинкою

  const dir = Math.sign(ev.deltaY);
  if (!dir) return;

  // deltaMode 1/2 — рядки й сторінки: вже дискретні, це зарубка.
  // deltaMode 0 з великим модулем — класичне колесо, теж зарубка.
  if (ev.deltaMode !== 0 || Math.abs(ev.deltaY) >= WHEEL_NOTCH_PX) {
    wheelAcc = 0;               // зарубка перебиває недобрані пікселі тачпада
    send(P.encodeEnc(dir));
    noteInput(Wait.SRC_ENC);
    return;
  }

  // Дрібні дельти — тачпад. Тільки тут має сенс складати.
  wheelAcc += ev.deltaY;
  if (Math.abs(wheelAcc) >= WHEEL_TRACKPAD_PX) {
    wheelAcc -= Math.sign(wheelAcc) * WHEEL_TRACKPAD_PX;
    send(P.encodeEnc(Math.sign(ev.deltaY)));
    noteInput(Wait.SRC_ENC);
  }
}, { passive: false });

/* Вкладку сховали або телефон заблокували — палець на екрані лишатись не має.
 * Те саме стосується правої кнопки: `pointerup` над схованою вкладкою може не
 * прийти зовсім, а утримана клавіша пульта — це вже не косметика. */
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) return;
  if (mirror.down) {
    sendTouch(P.TOUCH_UP, { x: mirror.x, y: mirror.y });
    pointerId = null;
  }
  const back = backKeyCode();
  if (back !== null && (mirror.keys & (1 << back))) sendBack(false);
});

// ------------------------------------------------------------- керування ---

document.getElementById('baud').addEventListener('change', onBaudPick);

document.getElementById('btn-refresh').addEventListener('click', () => {
  send(P.encodeFrame(P.PKT_REFRESH));
});

/* Перемикач очікування. Напис показує **поточний стан**, а не дію: людина зі
 * стенда має бачити, у якому режимі вона зараз, не тиснучи нічого. */
const btnWait = document.getElementById('btn-wait');

function waitLabel() {
  // ⚠️ До HELLO швидкості немає, і поріг був би запасним 45 — на 921600
  // правильне число 16. Показувати завідомо чуже число в перші секунди, коли
  // людина зі стенда саме й читає кнопку, гірше, ніж не показувати нічого.
  // ⚠️ Порога до HELLO не існує, і показувати запасні 45 не можна: на 921600
  // правильне число 16, а читає кнопку людина зі стенда саме в перші секунди.
  // Але це стосується **тільки** режиму з порогом — решту двох малюємо завжди,
  // інакше після кожного розриву (hello = null) людина перестає бачити, у
  // якому режимі вона зараз.
  const limit = hello ? policy.tileLimit() : null;
  const m = WAIT_MODES[policy.mode];
  btnWait.textContent = m.label(limit);
  btnWait.title = m.title(limit);
}

btnWait.addEventListener('click', () => {
  // Від типового `до N`: до N → завжди → вимк → до N.
  policy.setMode((policy.mode + 1) % WAIT_MODES.length);

  // ⚠️ Лічильники причин гасяться разом із режимом. Третє положення додане
  // саме заради **порівняння чисел** між режимами на одному прогоні (критерій
  // 4.1 задачі 0019), а накопичувальні лічильники показували б суміш двох
  // режимів, і людині довелося б віднімати в голові. Єдиною альтернативою
  // було б перезавантаження сторінки — тобто розрив з'єднання.
  //
  // Гасяться **разом із `frames`**, інакше розлетілась би рівність «сума
  // причин = кадрів», і разом із мітками плашки, інакше вона одну секунду
  // показувала б від'ємне.
  resetFrameCounters();
  // Перейшли у «вимк» посеред очікування — відкладений кадр показуємо негайно,
  // інакше він висів би до строку вже в режимі, який очікування не робить.
  // ⚠️ Саме `pendingFrameTimer !== null`, а не просто showFrame(): показ без
  // причини збив би рівність «сума причин = кадрів».
  if (policy.mode === Wait.WAIT_OFF && pendingFrameTimer !== null) {
    cancelPendingFrame();
    showFrame(Wait.WHY_OFF);
  }
  waitLabel();
});

/* ⚠️ Вимикач правила протягу — **без кнопки в панелі**, і це навмисно.
 *
 * Критерій 2.3 вимагає сліпого порівняння: людині не кажуть, який режим
 * увімкнено. Видима кнопка це знання їй і дала б, а спосіб уже показав свою
 * вартість — саме сліпа пара прогонів у 0019 підтвердила залежність від
 * швидкості, коли зряче порівняння давало суперечливі відповіді.
 *
 * Тому смикає його сценарій із ПК, а стан друкується в панелі «Стан» — знімок
 * усе одно каже, у якому режимі його знято.
 */
window.remoteUiDragRule = function (on) {
  policy.setDragRule(on);
  // Лічильники гасяться з тієї ж причини, що й при зміні режиму «чек»:
  // накопичувальні числа двох режимів в одному прогоні довелося б віднімати в
  // голові, а порівняння чисел — це половина сенсу вимикача.
  resetFrameCounters();
  return policy.dragRule;
};

const info = document.getElementById('info');
document.getElementById('btn-info').addEventListener('click', () => {
  info.hidden = !info.hidden;
  updateInfo();
});

/**
 * Опитування моста: і джерело для панелі стану, і лікування втрачених плиток.
 *
 * Втрату видно **тільки звідси**: пульт про неї не знає (він плитку віддав),
 * клієнт не знає (він її не отримував і не мав чого чекати). Знає рівно міст,
 * бо саме він її й викинув.
 */
async function pollBridge() {
  try {
    const r = await fetch('/api/stats', { cache: 'no-store' });
    bridgeStats = await r.json();
  } catch (e) {
    bridgeStats = null;   // ми не за мостом, або Wi-Fi вимкнули командою
    return;
  }

  const dropped = (bridgeStats.ws && bridgeStats.ws.packets_dropped) || 0;
  if (lastDropped === null) { lastDropped = dropped; return; }

  const grew = dropped > lastDropped;
  lastDropped = dropped;
  if (!grew || !hello) return;

  const now = performance.now();

  // Картинка ще рухається — наступні кадри перемалюють те місце самі, а
  // REFRESH зараз лише додасть 20–40 КБ у той самий затор.
  if (now - lastFrameAt < QUIET_BEFORE_REFRESH_MS) return;

  // Придушення: інакше REFRESH породжує втрати, які породжують REFRESH.
  if (now - lastRefreshAt < REFRESH_MIN_GAP_MS) return;

  lastRefreshAt = now;
  counters.autoRefresh++;
  send(P.encodeFrame(P.PKT_REFRESH));
}

setInterval(pollBridge, BRIDGE_POLL_MS);

const SOURCE_NAME = {
  [Wait.SRC_NONE]: 'вводу не було',
  [Wait.SRC_TOUCH]: 'дотик',
  [Wait.SRC_ENC]: 'енкодер',
  [Wait.SRC_KEY]: 'клавіша',
};

/** Чим щойно керували і яке правило з цього діє — одним рядком. */
function sourceLabel() {
  const now = performance.now();
  const name = SOURCE_NAME[policy.lastSource] || policy.lastSource;
  if (policy.lastSource === Wait.SRC_NONE) return `${name} → чекаю`;

  const ago = Math.round(now - policy.lastInputAt);
  const kind = policy.lastSource === Wait.SRC_TOUCH
    ? (policy.touchDragging ? ' (протяг)' : ' (тик)') : '';
  const rule = policy.dragging(now)
    ? `НЕ чекаю (вікно ${Wait.TOUCH_NOWAIT_MS} мс)` : 'чекаю';
  return `${name}${kind}, ${ago} мс тому → ${rule}`;
}

function latencyLine(name, source) {
  const s = latencyStats(source);
  if (!s) return `  ${name.padEnd(8)} —`;
  return `  ${name.padEnd(8)} медіана ${Math.round(s.med)}` +
         `  90% ${Math.round(s.p90)}  макс ${Math.round(s.max)}  (n=${s.n})`;
}

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
    `    цілих (dirtyTiles = 0)       ${counters.framesWhole}`,
    `    показано за строком          ${counters.framesTimeout}`,
    `    залишок понад поріг          ${counters.framesTooMany}`,
    `    очікування вимкнене          ${counters.framesNoWait}`,
    `    палець веде (не чекав)       ${counters.framesDrag}`,
    `    без ознаки повноти           ${counters.framesLegacy}`,
    // ⚠️ Режим — у панелі, а не лише на кнопці: панель і є артефактом доказу
    // за критеріями 3.2 і 4.1, а на знімку кнопки не видно.
    // ⚠️ Поріг береться лише за живого HELLO: без нього `frameWaitTileLimit`
    // віддає запасні 45, а на 921600 правильне число 16 — і панель, яка сама
    // і є артефактом доказу, надрукувала б чуже число.
    `  очікування                     ` +
      `${policy.mode === Wait.WAIT_OFF ? 'ВИМКНЕНЕ (режим «як було»)'
        : policy.mode === Wait.WAIT_LIMIT
          ? (hello ? `поріг ${policy.tileLimit()} плиток`
                   : 'поріг ще невідомий (не було HELLO)')
          : `ЗАВЖДИ (порога немає, стеля ${P.FRAME_WAIT_MAX_MS} мс)`}`,
    // ⚠️ Джерело вводу — критерій 1.4 задачі 0020, і він не косметичний: без
    // цього рядка при розборі скарги «знову смикається» не відрізнити
    // «правило не спрацювало» від «спрацювало не те».
    `  джерело вводу                  ${sourceLabel()}`,
    `  правило протягу                ` +
      `${policy.dragRule ? `увімкнене (вікно ${Wait.TOUCH_NOWAIT_MS} мс, ` +
                           `поріг ${Wait.TOUCH_DRAG_LIMIT_PX} px)`
                         : 'ВИМКНЕНЕ (поведінка до задачі 0020)'}`,
    `  REFRESH через втрати на мості  ${counters.autoRefresh}`,
    '',
    // ⚠️ Затримка міряється **тут**, а не в tools/input_check.py: той міряє до
    // дроту, а очікування лежить після дроту (борг 4.2 задачі 0019).
    'затримка ввід → показаний кадр, мс',
    latencyLine('дотик', Wait.SRC_TOUCH),
    latencyLine('енкодер', Wait.SRC_ENC),
    latencyLine('клавіша', Wait.SRC_KEY),
    `  відкинуто кадрів «був у дорозі» (< ${LATENCY_FLOOR_MS} мс): ${latency.early}`,
    '',
    'головний потік, мс за секунду',
    `  feed (кадрування+CRC) ${prof.last.feed.toFixed(1)}`,
    `  плитки (RLE+RGBA)     ${prof.last.tile.toFixed(1)}`,
    `  putImageData          ${prof.last.draw.toFixed(1)}`,
    `  РАЗОМ зайнято         ${Math.round(prof.last.busy * 100)}%` +
    `  при ${prof.last.kbs.toFixed(0)} КБ/с`,
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

  // Беремо вже стягнутий стан, а не питаємо вдруге.
  const bridge = bridgeStats
    ? 'міст\n' + JSON.stringify(bridgeStats, null, 1)
    : 'міст — не відповів (або ми не за мостом)';

  info.textContent = lines.join('\n') + bridge;
}

setInterval(updateInfo, 1000);

/* Частота кадрів і зайнятість головного потоку — раз на секунду, щоб не
 * смикати сторінку щокадрово.
 *
 * ⚠️ `busy` показується поруч із кадрами навмисно, а не ховається в панель:
 * це єдине число, яким людина зі стенда може відрізнити «міст не встигає
 * віддати» від «телефон не встигає забрати». Близьке до 100% означає, що
 * шукати треба тут, а не в ефірі.
 */
let lastFrames = 0;
let lastWhole = 0;
let lastProfAt = performance.now();
setInterval(() => {
  // ⚠️ Частка цілих кадрів стоїть поруч із частотою, а не в панелі під
  // кнопкою: обидва критерії задачі 0019 (скільки кадрів цілі, і чого коштувало
  // очікування) людина зі стенда звіряє очима, тримаючи телефон у руках.
  const shown = counters.frames - lastFrames;
  const whole = counters.framesWhole - lastWhole;
  lastFrames = counters.frames;
  lastWhole = counters.framesWhole;
  chip('fps', `${shown} кадр/с · ${whole} цілих`);

  const now = performance.now();
  const dt = Math.max(1, now - lastProfAt);
  lastProfAt = now;

  prof.last = {
    // `feed` без вкладених плиток: інакше розбір «коштував» би разом із
    // розтисканням і малюванням, і розкладка ні на що не вказувала б.
    feed: prof.msFeed - prof.msTile,
    tile: prof.msTile,
    draw: prof.msDraw,
    kbs: prof.bytes / 1024 / (dt / 1000),
    busy: (prof.msFeed + prof.msDraw) / dt,
  };
  prof.msFeed = prof.msTile = prof.msDraw = prof.bytes = 0;

  const pct = Math.round(prof.last.busy * 100);
  chip('busy', `потік ${pct}% · ${Math.round(prof.last.kbs)} КБ/с`,
       pct >= 80 ? 'bad' : pct >= 50 ? 'warn' : 'ok');
}, 1000);

window.addEventListener('resize', fitCanvas);
window.addEventListener('orientationchange', () => setTimeout(fitCanvas, 200));

connect();

/* --- ниточка для тестів ----------------------------------------------------
 *
 * ⚠️ У браузері `module` не існує, тож цей блок там мертвий — рівно як у
 * `proto.js` і `wait.js`. Потрібен він тому, що цей файл **не був покритий
 * нічим**, і саме тут жила вада, яку рецензія 0020 знайшла очима: обгортка
 * гасила таймер, але не низку очікування в автоматі, і запас стелі переживав
 * розрив.
 *
 * Автомат винесено в `wait.js` саме щоб його можна було перевіряти без DOM.
 * Але обгортка навколо нього — теж код, і теж помиляється; `webui/app_test.js`
 * підставляє заглушку DOM і перевіряє саме її.
 */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    policy, counters, latency, WHY_COUNTER,
    showFrame, forgetPendingFrame, cancelPendingFrame, resizeTo,
    noteInput, latencyStats, latencyReset, resetFrameCounters,
    sourceLabel, updateInfo, onFrameEnd,
    LATENCY_FLOOR_MS,
  };
}
