# Патч для EdgeTX

Тут лежить **наш** код. У дерево EdgeTX він не копіюється: `tools/patch-apply.sh`
ставить symlink `upstream/edgetx/radio/src/remote_ui` → `remote_ui/`, тож
примірник коду один, і він у git цього репозиторію.

```
remote_ui/       наш код (див. remote_ui/README.md)
patches/
  hooks.patch    рівно дві вставки у два чужі файли
```

## Дві команди

```sh
tools/patch-apply.sh     # symlink + hooks.patch
tools/patch-revert.sh    # назад; перевіряє, що дерево EdgeTX чисте
tools/patch-update.sh    # зняти правки гачків з upstream назад у hooks.patch
```

Після `patch-revert.sh` команда `git -C upstream/edgetx status --short` має
давати порожній вивід. Скрипт перевіряє це сам і падає, якщо не так.

Робочий цикл, коли треба поправити гачок: правимо прямо в `upstream/edgetx`
(там видно контекст чужого коду й одразу збирається), потім `patch-update.sh`.
Без цього `patch-revert.sh` відмовиться працювати — інакше правки зникли б
без сліду.

## Що дозволено чіпати

Повний перелік — `docs/05-hooks.md`. Зараз використано два пункти з дев'яти:

| Файл EdgeTX | Що саме | Пункт |
|---|---|---|
| `radio/src/gui/colorlcd/lcd.cpp` | `#include` + виклик `remoteUiOnFlush()` на початку `flushLcd()` | 5 |
| `radio/src/CMakeLists.txt` | `option(REMOTE_UI …)` + `add_subdirectory(remote_ui)` | 8 |

`tools/patch-update.sh` тримає цей перелік у собі й не дасть знятися патчу, у
якому є сторонній файл.

## Правило комітів

```
commit A: remote_ui/**        тільки нові файли
commit B: patches/hooks.patch тільки гачки
```

Так `git rebase` на нову версію EdgeTX конфліктує щонайбільше в десятку
рядків, а не в усьому нашому коді.
