---
description: Перевірити наш патч на свіжій версії EdgeTX
allowed-tools: Bash, Read, Grep, Glob, Edit
model: sonnet
---

Делегуй роботу агенту `upstream-watcher`.

Задача: оновити `upstream/edgetx/`, перевірити всі точки дотику з
`docs/05-hooks.md`, спробувати rebase, зібрати симулятор після нього.

Обов'язково перевір окремо:
- версію LVGL (перехід 8 → 9 = серйозна робота);
- чи не додав upstream нових режимів у `enum UartModes`;
- чи не змінилася схема кадрових буферів у `gui/colorlcd/lcd.cpp`.

Результат: звіт + оновлені номери рядків у `docs/05-hooks.md`.
Нічого не комітити без підтвердження людини.
