#!/usr/bin/env python3
"""Два клієнти показують кадр однаково — або ця перевірка падає.

    python3 tools/wait_crosscheck.py

Проганяє спільні сценарії `tools/wait_cases.json` через **обидві** реалізації
автомата очікування:

    webui/wait.js             — браузерний клієнт (через `node`)
    tools/remote_ui_proto.py  — клієнт ПК (тут же, у цьому файлі)

і порівнює сліди рядок у рядок.

⚠️ Навіщо саме так, а не двома наборами очікуваних значень. Заготовлені
очікування перевіряють кожну реалізацію **проти себе самої**: правку в одному
клієнті достатньо продублювати в його ж очікуваннях, і розбіжність лишиться
непоміченою. Так уже було — рецензія 0019 назвала `webui/app.js` не покритим
нічим, а дзеркальність двох клієнтів такою, що тримається на читанні очима, і
одну розбіжність це коштувало.

Тут очікуваних значень **немає взагалі**. Еталон одного клієнта — другий
клієнт. Розійтись потай неможливо за побудовою.

⚠️ Перевірка, яка не може провалитись, у цьому проєкті траплялась уже тричі.
Ця може: щоб у цьому переконатись, зіпсуйте одне число в `webui/wait.js` і
запустіть знову.
"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from remote_ui_proto import (  # noqa: E402
    FrameWaitPolicy,
    WHY_TIMEOUT,
)

CASES = ROOT / "tools" / "wait_cases.json"
TRACE_JS = ROOT / "webui" / "wait_trace.js"


def trace_python(cases) -> list[str]:
    """Той самий прогін, що й у `webui/wait_trace.js`, — дослівно."""
    out = []

    def emit(rec):
        # ⚠️ `separators` і `ensure_ascii` підігнані під JSON.stringify: інакше
        # сліди розійшлися б на пробілах і кирилиці, а не на поведінці.
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    for c in cases:
        emit({"case": c["name"]})

        policy = FrameWaitPolicy(mode=c["mode"], baud=c["baud"])
        timer_at = None

        def fire(now, _p=policy):
            nonlocal timer_at
            if timer_at is not None and timer_at <= now:
                emit({"t": timer_at, "ev": "show", "why": WHY_TIMEOUT})
                _p.note_shown()
                timer_at = None

        last = 0
        for step in c["steps"]:
            t = step[1]
            fire(t)
            last = t

            if step[0] == "input":
                _, _, src, phase, x, y = step
                policy.note_input(src, t, phase=phase, x=x, y=y)
                emit({"t": t, "ev": "input", "src": src, "phase": phase,
                      "drag": policy.dragging(t)})
            elif step[0] == "frame":
                show, wait_ms, why = policy.decide(step[2], t)
                if show:
                    emit({"t": t, "ev": "show", "why": why})
                    policy.note_shown()
                    timer_at = None
                else:
                    emit({"t": t, "ev": "wait", "ms": wait_ms})
                    timer_at = t + wait_ms
            elif step[0] == "mode":
                policy.set_mode(step[2])
                emit({"t": t, "ev": "mode", "mode": step[2]})
            elif step[0] == "baud":
                policy.set_baud(step[2])
                emit({"t": t, "ev": "baud", "limit": policy.tile_limit()})
            elif step[0] == "forget":
                policy.forget()
                timer_at = None
                emit({"t": t, "ev": "forget", "pending": policy.pending})
            elif step[0] == "drag":
                policy.set_drag_rule(step[2])
                emit({"t": t, "ev": "dragrule", "on": policy.drag_rule})
            else:
                raise SystemExit(f"невідомий крок: {step[0]}")

        fire(last + 10000)

    return out


def trace_js() -> list[str]:
    r = subprocess.run(["node", str(TRACE_JS), str(CASES)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("wait_trace.js упав:\n" + r.stderr)
    return r.stdout.strip().split("\n")


def normalise(line: str) -> str:
    """Звести рядок до порівнюваного вигляду.

    ⚠️ Числа проходять через `float`: JavaScript друкує 250, Python 250.0, і
    без цього перевірка падала б на поданні чисел замість поведінки. Решта
    полів звіряється як є — саме в них і живе розбіжність, яку ловимо.
    """
    rec = json.loads(line)
    for k, v in rec.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            rec[k] = float(v)
    return json.dumps(rec, ensure_ascii=False, sort_keys=True)


def main() -> int:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]

    py = trace_python(cases)
    js = trace_js()

    bad = 0
    for i in range(max(len(py), len(js))):
        a = py[i] if i < len(py) else "<немає рядка>"
        b = js[i] if i < len(js) else "<немає рядка>"
        same = (a != "<немає рядка>" and b != "<немає рядка>"
                and normalise(a) == normalise(b))
        if not same:
            bad += 1
            if bad <= 10:
                print(f"  ✗ рядок {i + 1}")
                print(f"      python: {a}")
                print(f"      js:     {b}")

    total = max(len(py), len(js))
    if bad:
        print(f"\nРОЗБІЖНІСТЬ: {bad} рядків із {total}. "
              f"Два клієнти показують кадр по-різному.")
        return 1

    print(f"сценаріїв {len(cases)}, подій {total} — сліди збіглися повністю")
    return 0


if __name__ == "__main__":
    sys.exit(main())
