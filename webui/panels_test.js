'use strict';
/*
 * Тести розкладки панелей і наміру. Запуск:  node webui/panels_test.js
 *
 * ⚠️ Перевіряється головне правило задачі 0022: **у клієнті нуль рядків
 * специфіки TX16S**. Таблиця бажаного розташування описує звичку руки для
 * цього пульта, але код має лишатися робочим на пульті з іншим набором клавіш
 * — з іншими мітками, зі справжніми стрілками, без енкодера, без нічого.
 * Саме це тут і перевіряється: не «чи гарно на TX16S», а «чи не зламається на
 * чужому».
 *
 * DOM тут не потрібен: усе, що торкається document, лишилось в `app.js` і
 * перевіряється в `app_test.js`.
 */

const Panels = require('./panels.js');

let failed = 0;
let checks = 0;

function check(cond, msg) {
  checks++;
  if (!cond) { failed++; console.log('  ✗ ' + msg); }
}

/** Пульт із HELLO: перелік клавіш і прапорці. */
function radio(keys, opts) {
  const o = opts || {};
  return {
    keys: keys.map((name, i) => ({ code: i, name })),
    hasEncoder: o.encoder !== false,
    hasTouch: true,
  };
}

const TX16S = ['RTN', 'Enter', 'PAGE<', 'PAGE>', 'MDL', 'TELE', 'SYS'];

// ------------------------------------------- 1. пульт без стрілок ---------

console.log('пульт без стрілок: вгору-вниз бере енкодер, вліво-вправо — ніхто');
{
  const i = Panels.resolveIntents(radio(TX16S));

  check(i.up && i.up.kind === 'enc' && i.up.steps === -1, 'вгору = енкодер −1');
  check(i.down && i.down.kind === 'enc' && i.down.steps === 1, 'вниз = енкодер +1');
  check(i.left === null && i.right === null,
        '⚠️ вліво-вправо лишаються невиконуваними, а не стають сторінками');
  check(i.select && i.select.label === 'Enter', 'вибрати = Enter');
  check(i.back && i.back.label === 'RTN', 'назад = RTN');
  check(i['page-prev'] && i['page-prev'].label === 'PAGE<', 'сторінка ← = PAGE<');
  check(i['page-next'] && i['page-next'].label === 'PAGE>', 'сторінка → = PAGE>');
}

// ---------------------------------------- 2. пульт зі справжніми стрілками -

console.log('пульт зі стрілками: намір бере клавіші, енкодер їх не відбирає');
{
  const i = Panels.resolveIntents(radio(['EXIT', 'ENTER', 'UP', 'DOWN', 'LEFT', 'RIGHT']));

  check(i.up && i.up.kind === 'key' && i.up.label === 'UP',
        'вгору — справжня клавіша, а не енкодер');
  check(i.left && i.left.kind === 'key', 'на такому пульті вліво існує');
  check(i.back && i.back.label === 'EXIT', 'інша мітка «назад» теж упізнана');
  check(i['page-prev'] === null,
        'сторінок у цього пульта немає — і намір лишається порожнім');
}

// ------------------------------------------------ 3. пульт без енкодера ---

console.log('пульт без енкодера й без стрілок: вгору-вниз просто немає');
{
  const i = Panels.resolveIntents(radio(['RTN', 'Enter'], { encoder: false }));
  check(i.up === null && i.down === null,
        'нічим виконати — і кнопки на джойстику не буде');
  check(i.select !== null, 'а центр працює');
}

// ------------------------------------------------------- 4. розкладка ----

console.log('розкладка: порядок із таблиці, склад — із HELLO');
{
  const hello = radio(TX16S);
  const plan = Panels.planPanels(hello, Panels.resolveIntents(hello));

  check(plan.left.map((k) => k.label).join(',') === 'SYS,RTN,PAGE>,PAGE<,TELE',
        `ліва панель у порядку з фотографії: ${plan.left.map((k) => k.label)}`);
  check(plan.right.map((k) => k.label).join(',') === 'MDL',
        'права панель — MDL, решта місця під джойстик');
  check(!plan.left.some((k) => k.label === 'Enter') &&
        !plan.right.some((k) => k.label === 'Enter'),
        'Enter на панель не дублюється — він у центрі джойстика');

  const wide = plan.left.concat(plan.right).filter((k) => k.wide).map((k) => k.label);
  check(wide.join(',') === 'SYS,MDL',
        `широкими світлими лишились рівно SYS і MDL: ${wide}`);
}

// -------------------------------------- 5. чужий пульт нічого не ламає ----

console.log('чужий набір клавіш дає робочі панелі без правки коду');
{
  const hello = radio(['EXIT', 'ENTER', 'WTF', 'BIND', 'PAGE>']);
  const intents = Panels.resolveIntents(hello);
  const plan = Panels.planPanels(hello, intents);

  const all = plan.left.concat(plan.right).map((k) => k.label);
  check(all.includes('WTF') && all.includes('BIND'),
        `клавіша, якої немає в таблиці, місце все одно отримала: ${all}`);
  check(!all.includes('ENTER'), 'центр джойстика дублем на панель не пішов');
  check(plan.left[0] && plan.left[0].label === 'EXIT' && !plan.left[0].extra,
        'місце належить ролі, а не напису: EXIT стає там, де в TX16S RTN');
  check(plan.left[1] && plan.left[1].label === 'PAGE>',
        'і сторінка йде за ним, а не в кінці серед невідомих');
  check(plan.left.filter((k) => k.extra).length === 2,
        'невідомі клавіші позначені як «місце дане, а не вибране»');
  check(!all.includes('PAGE<'),
        '⚠️ клавіша, якої в HELLO немає, не показується взагалі');
}

// -------------------------------------------------- 6. розгін енкодера ---

console.log('розгін енкодера: проміжки правдиві й скорочуються');
{
  const d0 = Panels.encDelay(0);
  const d1 = Panels.encDelay(1);
  const d9 = Panels.encDelay(9);

  check(d0 === Panels.ENC_FIRST_MS,
        `перший проміжок неспішний (${d0} мс): тик має означати один пункт`);
  check(d1 < d0, 'другий уже коротший');
  check(d9 === Panels.ENC_FAST_MS, `на утриманні виходить на стелю: ${d9} мс`);
  check(Panels.encDelay(1000) === Panels.ENC_FAST_MS, 'нижче стелі не падає');

  // ⚠️ Нуль тут був би вадою, а не швидкістю: EdgeTX рахує прискорення з `dt`
  // між клацаннями, і пачка з нульовими проміжками застигла б на максимальному
  // кроці — рівно те, що обійшла задача 0008.
  check(Panels.ENC_FAST_MS > 0, 'проміжок ніколи не нульовий');
  check(1000 / Panels.ENC_FAST_MS >= 10,
        `стеля дає ${(1000 / Panels.ENC_FAST_MS).toFixed(1)} клацань/с — довгий ` +
        'список проходиться');
}

// ----------------------------------------------------------------------------

console.log(`\nперевірок ${checks}, невдалих ${failed}`);
process.exit(failed ? 1 : 0);
