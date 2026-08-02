'use strict';
/*
 * Тести автомата очікування. Запуск:  node webui/wait_test.js
 *
 * ⚠️ Тут перевіряються **правила** — читабельними твердженнями, по одному на
 * пункт критерію 1 задачі 0020. Того, що два клієнти не розійшлись, ці тести
 * не доводять і довести не можуть: це робить `tools/wait_crosscheck.py`,
 * порівнюючи сліди обох реалізацій на спільних сценаріях. Дві перевірки
 * навмисно різні за природою — одна каже «правило те», друга «правило одне на
 * двох».
 */

const W = require('./wait.js');
const P = require('./proto.js');

let failed = 0;
let checks = 0;

function check(cond, msg) {
  checks++;
  if (!cond) { failed++; console.log('  ✗ ' + msg); }
}

/** Коротка обгортка: створити автомат у типовому режимі. */
function policy(mode, baud) {
  return new W.FrameWaitPolicy({
    mode: mode === undefined ? W.WAIT_ALWAYS : mode,
    baud: baud === undefined ? 2625000 : baud,
  });
}

/** Протяг: down, потім рух далі за поріг. Повертає мить останнього пакета. */
function drag(p, t0) {
  p.noteInput(W.SRC_TOUCH, t0, { phase: 'down', x: 100, y: 100 });
  p.noteInput(W.SRC_TOUCH, t0 + 30, { phase: 'move', x: 100, y: 160 });
  return t0 + 30;
}

// ------------------------------------------- 1.1. правило за джерелом ------

console.log('1.1. після ENC чекаємо, під час TOUCH — ні');
{
  const p = policy();
  p.noteInput(W.SRC_ENC, 0);
  const r = p.decide(40, 10);
  check(r.show === false, 'після енкодера неповний кадр чекає');
  check(r.waitMs === P.frameWaitMs(40), 'строк той самий, що й до 0020');
}
{
  const p = policy();
  const t = drag(p, 0);
  const r = p.decide(40, t + 10);
  check(r.show === true && r.why === W.WHY_DRAG,
        'під час протягу кадр показується негайно');
}
{
  // ⚠️ Найважливіше в правилі — воно не має «покращувати» цілий кадр.
  const p = policy();
  const t = drag(p, 0);
  const r = p.decide(0, t + 10);
  check(r.why === W.WHY_WHOLE,
        'цілий кадр лишається цілим, а не стає «показаним через протяг»');
}
{
  const p = policy();
  p.noteInput(W.SRC_KEY, 0);
  check(p.decide(40, 10).show === false, 'клавіша поводиться як енкодер');
}

// ----------------------------- 1.2. «одразу після» названо числом ----------

console.log('1.2. хвіст інерції — вікно після останнього пакета дотику');
{
  const p = policy();
  const t = drag(p, 0);
  p.noteInput(W.SRC_TOUCH, t + 30, { phase: 'up', x: 100, y: 200 });
  const up = t + 30;

  check(p.decide(40, up + 1).why === W.WHY_DRAG,
        'одразу після відпускання ще не чекаємо');
  check(p.decide(40, up + W.TOUCH_NOWAIT_MS).why === W.WHY_DRAG,
        'на самій межі вікна ще не чекаємо');
  check(p.decide(40, up + W.TOUCH_NOWAIT_MS + 1).show === false,
        'за межею вікна очікування повертається');
}
{
  // Вікно тримається на **останньому** пакеті, а не на початку жесту: довгий
  // протяг не має вичерпати вікно до того, як палець зупиниться.
  const p = policy();
  drag(p, 0);
  p.noteInput(W.SRC_TOUCH, 3000, { phase: 'move', x: 100, y: 240 });
  check(p.decide(40, 3500).why === W.WHY_DRAG,
        'довгий протяг не вичерпує вікно');
}
{
  const p = policy();
  const t = drag(p, 0);
  p.noteInput(W.SRC_TOUCH, t + 30, { phase: 'up', x: 100, y: 200 });
  p.noteInput(W.SRC_ENC, t + 100);
  check(p.decide(40, t + 110).show === false,
        'енкодер посеред хвоста інерції закриває вікно негайно');
}

// ---------------------------------- тик пальцем — не протяг ---------------

console.log('тик пальцем чекає, як енкодер');
{
  const p = policy();
  p.noteInput(W.SRC_TOUCH, 0, { phase: 'down', x: 100, y: 100 });
  p.noteInput(W.SRC_TOUCH, 120, { phase: 'up', x: 102, y: 103 });
  check(p.decide(40, 130).show === false, 'тик без руху — очікування лишається');
}
{
  const p = policy();
  p.noteInput(W.SRC_TOUCH, 0, { phase: 'down', x: 100, y: 100 });
  p.noteInput(W.SRC_TOUCH, 30,
              { phase: 'move', x: 100, y: 100 + W.TOUCH_DRAG_LIMIT_PX - 1 });
  check(p.decide(40, 40).show === false, 'рух менший за поріг — ще тик');

  const q = policy();
  q.noteInput(W.SRC_TOUCH, 0, { phase: 'down', x: 100, y: 100 });
  q.noteInput(W.SRC_TOUCH, 30,
              { phase: 'move', x: 100, y: 100 + W.TOUCH_DRAG_LIMIT_PX });
  check(q.decide(40, 40).why === W.WHY_DRAG, 'рівно поріг — уже протяг');
}
{
  // Новий дотик після протягу починає жест наново — інакше одного протягу
  // вистачило б, щоб усі наступні тики показувались зшитими.
  const p = policy();
  const t = drag(p, 0);
  p.noteInput(W.SRC_TOUCH, t + 30, { phase: 'up', x: 100, y: 200 });
  p.noteInput(W.SRC_TOUCH, t + 100, { phase: 'down', x: 50, y: 50 });
  check(p.decide(40, t + 110).show === false, 'новий тик закриває вікно');
}

// ------------------------ 2.3. вимикач правила для сліпого порівняння -----

console.log('2.3. вимкнене правило дає поведінку рівно до 0020');
{
  const p = policy();
  const t = drag(p, 0);
  check(p.decide(40, t + 10).why === W.WHY_DRAG, 'увімкнене — показ негайно');

  p.setDragRule(false);
  check(p.decide(40, t + 20).show === false,
        'вимкнене — очікування повертається, хоч палець і веде');
  check(p.touchDragging === true,
        'сам жест при цьому не забувається: вимикач не переписує стан');

  p.setDragRule(true);
  check(p.decide(40, t + 30).why === W.WHY_DRAG, 'увімкнули назад — знову негайно');
}

// -------------------------------------- 1.3. вводу не було взагалі --------

console.log('1.3. без вводу поводимось як після енкодера');
{
  const p = policy();
  check(p.lastSource === W.SRC_NONE, 'початковий стан — вводу не було');
  const r = p.decide(12, 0);
  check(r.show === false, 'самостійна перемальовка пульта чекає цілого кадру');
  check(r.waitMs === P.frameWaitMs(12), 'строк той самий');
}

// -------------------------------- усе, що лишилось із 0019, ціле ----------

console.log('правила 0019 не зрушені');
{
  check(policy().decide(null, 0).why === W.WHY_LEGACY,
        'стара прошивка без ознаки повноти — показ негайно');
  check(policy().decide(0, 0).why === W.WHY_WHOLE, 'цілий кадр — негайно');
  check(policy(W.WAIT_OFF).decide(90, 0).why === W.WHY_OFF,
        'режим «вимк» — показ на кожному FRAME_END');
}
{
  const p = policy(W.WAIT_LIMIT, 921600);
  const limit = p.tileLimit();
  check(limit === P.frameWaitTileLimit(921600), 'поріг береться зі швидкості');
  check(p.decide(limit, 0).show === false, 'рівно поріг — ще чекаємо');
  check(p.decide(limit + 1, 0).why === W.WHY_TOO_MANY, 'понад поріг — показ');
}
{
  // ⚠️ Протяг обходить поріг: рух важливіший за шов саме тоді, коли залишок
  // великий. Без цього правило 0020 вимикалось би на найбільших перемальовках.
  const p = policy(W.WAIT_LIMIT, 921600);
  const t = drag(p, 0);
  check(p.decide(p.tileLimit() + 50, t + 10).why === W.WHY_DRAG,
        'у режимі «до N» протяг обходить поріг');
}
{
  // Стеля рахується від початку низки, а не від кожного кадру.
  const p = policy();
  let t = 0;
  check(p.decide(90, t).show === false, 'перший кадр низки чекає');
  t = P.FRAME_WAIT_MAX_MS - 10;
  const r = p.decide(90, t);
  check(r.show === false && r.waitMs === 10,
        `на межі стелі лишається рівно залишок: ${r.waitMs}`);
  check(p.decide(90, P.FRAME_WAIT_MAX_MS).why === W.WHY_TIMEOUT,
        'стеля вичерпана — показ за строком');
}
{
  // Після показу низка починається заново — інакше після паузи на нерухомому
  // екрані перше ж гортання показалось би без очікування зовсім.
  const p = policy();
  p.decide(20, 0);
  p.noteShown();
  const r = p.decide(20, 5000);
  check(r.show === false && r.waitMs === P.frameWaitMs(20),
        'нова низка отримує повний запас');
}

// ----------------------------------------------------------------------------

console.log(`\nперевірок ${checks}, невдалих ${failed}`);
process.exit(failed ? 1 : 0);
