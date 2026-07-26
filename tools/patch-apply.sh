#!/usr/bin/env bash
#
# Вбудовує Remote UI в дерево EdgeTX.
#
# Дві дії, і обидві оборотні:
#   1. symlink  upstream/edgetx/radio/src/remote_ui -> firmware/edgetx-patch/remote_ui
#   2. patch    firmware/edgetx-patch/patches/hooks.patch на файли EdgeTX
#
# Скасування — tools/patch-revert.sh. Після нього `git -C upstream/edgetx
# status --short` порожній.
#
# Чому саме так — див. state/DECISIONS.md (рядок про механізм вбудовування).
# Коротко: symlink лишає один-єдиний примірник нашого коду (той, що в git
# нашого репозиторію), а гачки лежать патчем, бо їх мало і вони в чужих файлах.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="$REPO/upstream/edgetx"
SOURCE_DIR="$REPO/firmware/edgetx-patch/remote_ui"
PATCH="$REPO/firmware/edgetx-patch/patches/hooks.patch"

LINK="$UPSTREAM/radio/src/remote_ui"
LINK_TARGET="../../../../firmware/edgetx-patch/remote_ui"
EXCLUDE_LINE="radio/src/remote_ui"

if [ ! -d "$UPSTREAM/.git" ]; then
  echo "Немає $UPSTREAM — спершу tools/setup-upstream.sh" >&2
  exit 1
fi

if [ ! -d "$SOURCE_DIR" ]; then
  echo "Немає $SOURCE_DIR" >&2
  exit 1
fi

# Усі перевірки — до першої дії, яка щось міняє. Інакше скрипт, який упав на
# півдорозі, лишає дерево напівзміненим, і наступний запуск починає з дивного
# стану.
if [ ! -f "$PATCH" ]; then
  echo "Немає $PATCH" >&2
  exit 1
fi

if ! grep -q '^@@' "$PATCH"; then
  echo "$PATCH порожній або обрізаний — гачків у ньому немає." >&2
  echo "Мовчки зібрати симулятор без захоплення екрана гірше, ніж не зібрати." >&2
  exit 1
fi

if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "У дереві EdgeTX уже лежить справжній каталог radio/src/remote_ui." >&2
  echo "Це не наш symlink — розберіться руками, щоб нічого не затерти." >&2
  exit 1
fi

VERSION="$(git -C "$UPSTREAM" describe --tags 2>/dev/null || echo '?')"
if [ "$VERSION" != "v2.12.2" ]; then
  echo "УВАГА: upstream на $VERSION, а патч знято з v2.12.2" >&2
fi

# Чи ляже патч — теж вирішується тут, до першої дії. Інакше патч, який не
# лягає, лишав би по собі symlink.
if git -C "$UPSTREAM" apply --reverse --check "$PATCH" 2>/dev/null; then
  HOOKS_ALREADY=1
else
  HOOKS_ALREADY=0
  git -C "$UPSTREAM" apply --check "$PATCH"
fi

# --- 1. Наш код ------------------------------------------------------------

ln -sfn "$LINK_TARGET" "$LINK"

# Відносний шлях прибитий рядком вище, тож перевіряємо, що він і справді
# кудись веде: битий symlink інакше виявився б аж помилкою компіляції.
if [ ! -f "$LINK/remote_ui.h" ]; then
  echo "Symlink $LINK нікуди не веде (чекали на remote_ui.h)." >&2
  rm -f "$LINK"
  exit 1
fi

# Щоб `git status` у EdgeTX показував лише справжні зміни в його файлах, а не
# наш каталог. Файл .git/info/exclude місцевий і в чужий репозиторій не
# потрапляє.
EXCLUDE_FILE="$UPSTREAM/.git/info/exclude"
if ! grep -qxF "$EXCLUDE_LINE" "$EXCLUDE_FILE" 2>/dev/null; then
  printf '%s\n' "$EXCLUDE_LINE" >> "$EXCLUDE_FILE"
fi

# --- 2. Гачки --------------------------------------------------------------

if [ "$HOOKS_ALREADY" -eq 1 ]; then
  echo "Гачки вже стоять — патч не застосовується двічі."
else
  git -C "$UPSTREAM" apply "$PATCH"
  echo "Гачки застосовано."
fi

echo
echo "Готово. Стан дерева EdgeTX:"
git -C "$UPSTREAM" status --short
echo
echo "Зібрати симулятор із Remote UI:"
echo "  EDGETX_VERSION_SUFFIX=remoteui cmake --preset simu \\"
echo "      -DPCB=X10 -DPCBREV=TX16S -DDISABLE_COMPANION=ON -DREMOTE_UI=ON"
