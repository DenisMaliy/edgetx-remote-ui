'use strict';
/*
 * Прогін сценаріїв `tools/wait_cases.json` через `webui/wait.js`.
 *
 *     node webui/wait_trace.js [шлях/до/wait_cases.json]
 *
 * Друкує слід — по рядку JSON на кожну подію. Сам по собі нічого не
 * перевіряє: перевіркою є **збіг** цього сліду зі слідом близнюка на Python.
 * Порівнює їх `tools/wait_crosscheck.py`.
 *
 * ⚠️ Тут же моделюється таймер того, хто викликає автомат: у живому клієнті
 * відкладений кадр показує `setTimeout`, і без нього ні низка очікування, ні
 * стеля 250 мс не перевірялись би зовсім.
 */

const fs = require('fs');
const path = require('path');
const W = require('./wait.js');

const file = process.argv[2]
  || path.join(__dirname, '..', 'tools', 'wait_cases.json');
const cases = JSON.parse(fs.readFileSync(file, 'utf8')).cases;

const out = [];

function emit(rec) { out.push(JSON.stringify(rec)); }

for (const c of cases) {
  emit({ case: c.name });

  const policy = new W.FrameWaitPolicy({ mode: c.mode, baud: c.baud });
  let timerAt = null;   // мить, коли спрацює відкладений показ

  // Показати відкладений кадр, якщо його строк уже настав.
  const fireTimer = (now) => {
    if (timerAt !== null && timerAt <= now) {
      emit({ t: timerAt, ev: 'show', why: W.WHY_TIMEOUT });
      policy.noteShown();
      timerAt = null;
    }
  };

  let last = 0;
  for (const step of c.steps) {
    const t = step[1];
    fireTimer(t);
    last = t;

    if (step[0] === 'input') {
      const [, , src, phase, x, y] = step;
      policy.noteInput(src, t, { phase: phase, x: x, y: y });
      emit({ t: t, ev: 'input', src: src, phase: phase,
             drag: policy.dragging(t) });
    } else if (step[0] === 'frame') {
      const dirty = step[2];
      const r = policy.decide(dirty, t);
      if (r.show) {
        emit({ t: t, ev: 'show', why: r.why });
        policy.noteShown();
        timerAt = null;
      } else {
        emit({ t: t, ev: 'wait', ms: r.waitMs });
        timerAt = t + r.waitMs;
      }
    } else if (step[0] === 'mode') {
      policy.setMode(step[2]);
      emit({ t: t, ev: 'mode', mode: step[2] });
    } else if (step[0] === 'baud') {
      policy.setBaud(step[2]);
      emit({ t: t, ev: 'baud', limit: policy.tileLimit() });
    } else if (step[0] === 'forget') {
      // Розрив або зміна розміру: відкладений кадр помирає **без показу**.
      policy.forget();
      timerAt = null;
      emit({ t: t, ev: 'forget', pending: policy.pending });
    } else if (step[0] === 'drag') {
      policy.setDragRule(step[2]);
      emit({ t: t, ev: 'dragrule', on: policy.dragRule });
    } else {
      throw new Error('невідомий крок: ' + step[0]);
    }
  }

  // Досипаємо часу, щоб відкладений кадр не лишився невидимим у сліді.
  fireTimer(last + 10000);
}

process.stdout.write(out.join('\n') + '\n');
