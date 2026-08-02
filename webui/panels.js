'use strict';
/*
 * Бокові панелі клієнта: намір, розкладка клавіш, джойстик.
 *
 * ⚠️ Головна думка файлу — **намір, а не клавіша**. Джойстик і клавіатура
 * кажуть «вгору», «вибрати», «назад»; чим саме це виконати, вирішує перелік
 * клавіш із `HELLO`. На пульті зі справжніми стрілками намір «вгору» — це
 * клавіша `UP`; на TX16S стрілок немає взагалі, і той самий намір виконує
 * клацання енкодера.
 *
 * ⚠️ Вліво-вправо **ніколи не стають сторінками** — рішення людини 2026-08-03.
 * Усередині сторінки горизонтального руху на цьому пульті немає, а `PAGE<` і
 * `PAGE>` той, хто звик до пульта, знає як окремі кнопки. Розмити їх стрілками
 * означало б зіпсувати саме ту звичку, заради якої ми відтворюємо розкладку.
 *
 * Файл не знає ні про WebSocket, ні про протокол: він будує розкладку й
 * повідомляє, що натиснули. Пакети шле `app.js`. Через це чиста частина
 * (розбір `HELLO` у наміри й розкладку, розгін енкодера) прогоняється під node
 * — `node webui/panels_test.js`.
 */

(function (root) {

// ---------------------------------------------------------------- наміри ---

const INTENT_UP = 'up';
const INTENT_DOWN = 'down';
const INTENT_LEFT = 'left';
const INTENT_RIGHT = 'right';
const INTENT_SELECT = 'select';
const INTENT_BACK = 'back';
const INTENT_PAGE_PREV = 'page-prev';
const INTENT_PAGE_NEXT = 'page-next';

const INTENTS = [
  INTENT_UP, INTENT_DOWN, INTENT_LEFT, INTENT_RIGHT,
  INTENT_SELECT, INTENT_BACK, INTENT_PAGE_PREV, INTENT_PAGE_NEXT,
];

/**
 * Мітки клавіш, якими різні пульти EdgeTX виражають той самий намір.
 *
 * ⚠️ Це **словник синонімів**, а не опис TX16S. Мітки приходять із
 * `keysGetLabel()` і в різних цілях різні (`RTN` проти `EXIT`, `PAGE<` проти
 * `PGUP`). Мітка, якої тут немає, нічого не ламає: намір просто лишиться
 * невиконуваним, а сама клавіша все одно потрапить на панель.
 */
const INTENT_LABELS = {
  [INTENT_UP]: ['UP'],
  [INTENT_DOWN]: ['DOWN'],
  [INTENT_LEFT]: ['LEFT'],
  [INTENT_RIGHT]: ['RIGHT'],
  [INTENT_SELECT]: ['ENTER', 'OK'],
  [INTENT_BACK]: ['RTN', 'EXIT'],
  [INTENT_PAGE_PREV]: ['PAGE<', 'PGUP'],
  [INTENT_PAGE_NEXT]: ['PAGE>', 'PGDN'],
};

/** Підпис наміру для людини — у панелі стану й у підказках кнопок. */
const INTENT_NAME = {
  [INTENT_UP]: 'вгору',
  [INTENT_DOWN]: 'вниз',
  [INTENT_LEFT]: 'вліво',
  [INTENT_RIGHT]: 'вправо',
  [INTENT_SELECT]: 'вибрати',
  [INTENT_BACK]: 'назад',
  [INTENT_PAGE_PREV]: 'сторінка ←',
  [INTENT_PAGE_NEXT]: 'сторінка →',
};

// -------------------------------------------------------------- розкладка --

/**
 * Бажане розташування кнопок — **дані про звичку руки, а не логіка**.
 *
 * Порядок звірений із фотографією TX16S: `PAGE>` справді стоїть **над**
 * `PAGE<`, і це не описка. Але код не залежить від того, що саме тут написано:
 * мітка, якої в `HELLO` немає, просто не показується, а клавіша, якої немає в
 * таблиці, отримує місце в кінці лівої панелі. Пульт із іншим набором дає
 * робочі панелі без правки коду — просто в іншому порядку.
 */
const SIDE_ORDER = {
  left: [['SYS'], ['RTN', 'EXIT'], ['PAGE>', 'PGDN'], ['PAGE<', 'PGUP'], ['TELE']],
  right: [['MDL']],
};

/**
 * Клавіші, які на пульті виглядають інакше за решту, — і панель це відтворює.
 *
 * ⚠️ Не оздоблення. На самому TX16S `SYS` і `MDL` — широкі світлі кнопки
 * **над** екраном, ліворуч і праворуч, тоді як `RTN`, `PAGE>`, `PAGE<`, `TELE`
 * — вузькі чорні ребристі на лівому ребрі. Рука шукає їх окремо від решти, і
 * панель, де всі кнопки однакові, цю звичку ламає.
 */
const WIDE_LABELS = ['SYS', 'MDL'];

const norm = (s) => String(s === null || s === undefined ? '' : s).trim().toUpperCase();

/** Знайти в `HELLO` клавішу за будь-якою з міток. */
function findKey(hello, labels) {
  if (!hello || !hello.keys) return null;
  const want = labels.map(norm);
  for (const k of hello.keys) {
    if (want.includes(norm(k.name))) return k;
  }
  return null;
}

/**
 * Чим виконується кожен намір на **цьому** пульті.
 *
 * @return {Object} намір -> `{kind:'key', code, label}` або
 *   `{kind:'enc', steps, label}`, або `null`, якщо виконати нічим.
 *
 * ⚠️ Гілка «стрілок немає» бере енкодер **тільки на вгору-вниз**. Вліво-вправо
 * лишаються `null` і на панелі не показуються зовсім: клавіші під ними немає, а
 * вигадувати їй заміну зі сторінок заборонено рішенням людини.
 */
function resolveIntents(hello) {
  const out = {};
  for (const name of INTENTS) {
    const k = findKey(hello, INTENT_LABELS[name]);
    out[name] = k ? { kind: 'key', code: k.code, label: k.name } : null;
  }

  // Справжніх стрілок немає — вгору й вниз виражає енкодер. Напрямок той
  // самий, що в колеса миші (задача 0019): вниз = +1.
  if (!out[INTENT_UP] && !out[INTENT_DOWN] && hello && hello.hasEncoder) {
    out[INTENT_UP] = { kind: 'enc', steps: -1, label: 'енкодер −' };
    out[INTENT_DOWN] = { kind: 'enc', steps: 1, label: 'енкодер +' };
  }
  return out;
}

/**
 * Які кнопки лягають на ліву й праву панелі.
 *
 * Клавіша, що вже має своє місце на джойстику (центр і стрілки), на панель не
 * дублюється. `RTN` і сторінки — дублюються навмисно: вони є і кнопками на
 * ребрі пульта, і намірами з клавіатури, і людина шукає їх саме там, де вони
 * стоять на залізі.
 */
function planPanels(hello, intents) {
  const plan = { left: [], right: [] };
  if (!hello || !hello.keys) return plan;

  const claimed = new Set();
  for (const name of [INTENT_SELECT, INTENT_UP, INTENT_DOWN, INTENT_LEFT, INTENT_RIGHT]) {
    const i = intents && intents[name];
    if (i && i.kind === 'key') claimed.add(i.code);
  }

  const byLabel = new Map();
  for (const k of hello.keys) {
    if (!byLabel.has(norm(k.name))) byLabel.set(norm(k.name), k);
  }

  const placed = new Set();
  const button = (k, extra) => ({
    code: k.code,
    label: k.name,
    wide: WIDE_LABELS.includes(norm(k.name)),
    extra: !!extra,
  });

  for (const side of ['left', 'right']) {
    for (const synonyms of SIDE_ORDER[side]) {
      // Місце в розкладці належить **ролі**, а не буквальному напису: пульт,
      // що зве «назад» міткою `EXIT`, має отримати її там само, де TX16S
      // отримує `RTN`, — а не в купі невідомих у кінці панелі.
      let k = null;
      for (const label of synonyms) {
        k = byLabel.get(norm(label));
        if (k) break;
      }
      if (!k || claimed.has(k.code) || placed.has(k.code)) continue;
      plan[side].push(button(k, false));
      placed.add(k.code);
    }
  }

  // Невідома клавіша панель не ламає: їй просто дають місце в кінці лівої.
  for (const k of hello.keys) {
    if (claimed.has(k.code) || placed.has(k.code)) continue;
    plan.left.push(button(k, true));
    placed.add(k.code);
  }
  return plan;
}

// ------------------------------------------------------- розгін енкодера ---

/*
 * ⚠️ Утримання напрямку на пульті **без стрілок** автоповтору від EdgeTX не
 * отримує — повторювати нема чого: енкодер не клавіша, він не «натиснутий», а
 * «клацнув». Тому клацання повторює клієнт, і саме тут лежить різниця між
 * «шле правдиві проміжки» і «сипле пачкою».
 *
 * Проміжок скорочується, поки палець тримають: перше клацання неспішне (щоб
 * тик означав рівно один пункт), далі щораз швидше до стелі. Це і є те, що
 * бачить EdgeTX: прискорення в прошивці рахується з `dt` між клацаннями
 * (гачок 7, задача 0008), тож короткий проміжок сам собою дає більший крок.
 * Пачка з нульовими проміжками дала б протилежне — прошивка вважала б оберт
 * нескінченно швидким і крок застигав би на максимумі.
 */
const ENC_FIRST_MS = 260;
const ENC_FAST_MS = 70;
const ENC_RAMP = 0.82;

/** Скільки чекати перед клацанням №`clicks` (нумерація з нуля). */
function encDelay(clicks) {
  const d = ENC_FIRST_MS * Math.pow(ENC_RAMP, Math.max(0, clicks));
  return Math.max(ENC_FAST_MS, Math.round(d));
}

// ------------------------------------------------------------ оформлення ---

/*
 * ⚠️ Утримання, а не клацання. Пульту передається натискання **і**
 * відпускання окремо: довге натискання, автоповтор і прискорення дає сам
 * EdgeTX, і клієнт має лише чесно тримати рівень.
 *
 * Три способи відпустити, і всі три обов'язкові:
 *   - `pointerup` — палець підняли;
 *   - `pointercancel` — систему перебило (дзвінок, жест ОС);
 *   - вихід за межі кнопки — палець зісковзнув (критерій 4.3). Захоплення
 *     вказівника лишає події нам, тож без цієї перевірки кнопка лишалась би
 *     натиснутою, доки палець не піднімуть будь-де.
 */
function bindHold(el, press, release) {
  let id = null;

  const end = (ev) => {
    if (ev.pointerId !== id) return;
    id = null;
    el.classList.remove('down');
    release();
    if (ev.cancelable) ev.preventDefault();
  };

  el.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;   // права кнопка миші тут не при справах
    if (id !== null) return;
    id = ev.pointerId;
    try { el.setPointerCapture(ev.pointerId); } catch (e) { /* немає захоплення */ }
    el.classList.add('down');
    press();
    // Фокус лишається на екрані пульта: інакше клавіатура «переставала
    // працювати» після кожного натискання кнопки на панелі.
    if (ev.cancelable) ev.preventDefault();
  });

  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);

  /* ⚠️ Захоплення вказівника може не статись і може загубитись — тоді подій
   * від пальця, що поїхав геть, ця кнопка більше не побачить, і перевірка меж
   * нижче ніколи не спрацює. Обидва запасні шляхи безкоштовні: при живому
   * захопленні `pointerleave` не приходить зовсім. */
  el.addEventListener('lostpointercapture', end);
  el.addEventListener('pointerleave', end);

  el.addEventListener('pointermove', (ev) => {
    if (ev.pointerId !== id) return;
    const r = el.getBoundingClientRect();
    if (ev.clientX < r.left || ev.clientX > r.right ||
        ev.clientY < r.top || ev.clientY > r.bottom) end(ev);
  });
}

/** Кнопка клавіші пульта. */
function keyButton(doc, k, cb) {
  const b = doc.createElement('button');
  b.type = 'button';
  b.className = 'key' + (k.wide ? ' key-wide' : '') + (k.extra ? ' key-extra' : '');
  b.textContent = k.label;
  b.title = `клавіша пульта ${k.label} (код ${k.code})`;
  bindHold(b, () => cb.key(k.code, true), () => cb.key(k.code, false));
  return b;
}

const STICK_CELLS = [
  { intent: INTENT_UP, glyph: '▲', cls: 'st-up' },
  { intent: INTENT_LEFT, glyph: '◀', cls: 'st-left' },
  { intent: INTENT_SELECT, glyph: '●', cls: 'st-mid' },
  { intent: INTENT_RIGHT, glyph: '▶', cls: 'st-right' },
  { intent: INTENT_DOWN, glyph: '▼', cls: 'st-down' },
];

/**
 * Джойстик: хрестовина з центром.
 *
 * ⚠️ Напрямок, якого пульт виконати не може, **не показується** — саме цього
 * вимагає правило наміру. На TX16S це вліво-вправо: стрілок немає, а сторінки
 * під них вішати заборонено. Порожня клітинка лишається, щоб хрестовина не
 * поїхала й палець не промахувався.
 */
function stickBox(doc, intents, cb) {
  const box = doc.createElement('div');
  box.className = 'stick';

  for (const c of STICK_CELLS) {
    const i = intents[c.intent];
    if (!i) {
      const gap = doc.createElement('span');
      gap.className = 'stick-gap ' + c.cls;
      box.appendChild(gap);
      continue;
    }
    const b = doc.createElement('button');
    b.type = 'button';
    b.className = 'stick-btn ' + c.cls;
    b.textContent = c.glyph;
    b.title = `${INTENT_NAME[c.intent]} (${i.label})`;
    bindHold(b, () => cb.intent(c.intent, true), () => cb.intent(c.intent, false));
    box.appendChild(b);
  }
  return box;
}

/**
 * Побудувати обидві панелі наново.
 *
 * @param doc      document
 * @param plan     результат `planPanels`
 * @param intents  результат `resolveIntents`
 * @param cb       `{key(code, pressed), intent(name, pressed)}`
 */
function render(doc, plan, intents, cb) {
  const sides = [
    { body: doc.getElementById('pad-left-body'), keys: plan.left, stick: false },
    { body: doc.getElementById('pad-right-body'), keys: plan.right, stick: true },
  ];

  for (const s of sides) {
    if (!s.body) continue;
    s.body.innerHTML = '';
    for (const k of s.keys) s.body.appendChild(keyButton(doc, k, cb));
    if (s.stick) s.body.appendChild(stickBox(doc, intents, cb));
  }
}

/** Одним рядком: чим виконується кожен намір. Для панелі стану. */
function intentsText(intents) {
  const out = [];
  for (const name of INTENTS) {
    const i = intents && intents[name];
    out.push(`${INTENT_NAME[name]}=${i ? i.label : '—'}`);
  }
  return out.join('  ');
}

// ---------------------------------------------------------------- вихід ----

const RemoteUIPanels = {
  INTENT_UP, INTENT_DOWN, INTENT_LEFT, INTENT_RIGHT,
  INTENT_SELECT, INTENT_BACK, INTENT_PAGE_PREV, INTENT_PAGE_NEXT,
  INTENTS, INTENT_LABELS, INTENT_NAME,
  SIDE_ORDER, WIDE_LABELS,
  findKey, resolveIntents, planPanels, intentsText,
  ENC_FIRST_MS, ENC_FAST_MS, ENC_RAMP, encDelay,
  bindHold, render,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = RemoteUIPanels;   // node: panels_test.js
} else {
  root.RemoteUIPanels = RemoteUIPanels;   // браузер: app.js
}

})(typeof globalThis !== 'undefined' ? globalThis : this);
