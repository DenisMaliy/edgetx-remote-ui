#!/usr/bin/env bash
#
# Прибирає Remote UI з дерева EdgeTX — рівно те, що зробив tools/patch-apply.sh.
#
# Критерій успіху жорсткий: після цього `git -C upstream/edgetx status --short`
# порожній. Скрипт перевіряє це сам і повертає ненульовий код, якщо не так.
#
# Якщо гачки в дереві встигли змінитись (правили руками), зворотне
# застосування не пройде — і це навмисно. Спершу зніміть зміни в патч
# командою tools/patch-update.sh, інакше правки просто зникнуть.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="$REPO/upstream/edgetx"
PATCH="$REPO/firmware/edgetx-patch/patches/hooks.patch"

LINK="$UPSTREAM/radio/src/remote_ui"
EXCLUDE_LINE="radio/src/remote_ui"
EXCLUDE_FILE="$UPSTREAM/.git/info/exclude"

if [ ! -d "$UPSTREAM/.git" ]; then
  echo "Немає $UPSTREAM — прибирати нема що" >&2
  exit 1
fi

# --- 1. Гачки --------------------------------------------------------------

if [ -f "$PATCH" ]; then
  if git -C "$UPSTREAM" apply --reverse --check "$PATCH" 2>/dev/null; then
    git -C "$UPSTREAM" apply --reverse "$PATCH"
    echo "Гачки знято."
  elif git -C "$UPSTREAM" apply --check "$PATCH" 2>/dev/null; then
    echo "Гачків у дереві не було."
  else
    echo "Дерево EdgeTX змінене не так, як описує hooks.patch." >&2
    echo "Зніміть зміни в патч (tools/patch-update.sh) або поверніть файли" >&2
    echo "командою git -C upstream/edgetx checkout -- <файл>." >&2
    exit 1
  fi
fi

# --- 2. Наш код ------------------------------------------------------------

if [ -L "$LINK" ]; then
  rm "$LINK"
  echo "Symlink прибрано."
elif [ -e "$LINK" ]; then
  echo "radio/src/remote_ui — не symlink, руками його не чіпаю." >&2
  exit 1
fi

if [ -f "$EXCLUDE_FILE" ]; then
  # Код 1 у grep означає «жодного рядка не вибрано» — для порожнього файлу це
  # норма. А ось код 2 і вище — справжня помилка, і глушити її не можна:
  # перенаправлення вже створило порожній .tmp, і `mv` затер би чужі місцеві
  # винятки користувача.
  set +e
  grep -vxF "$EXCLUDE_LINE" "$EXCLUDE_FILE" > "$EXCLUDE_FILE.tmp"
  GREP_RC=$?
  set -e

  if [ "$GREP_RC" -ge 2 ]; then
    rm -f "$EXCLUDE_FILE.tmp"
    echo "Не вдалось перечитати $EXCLUDE_FILE — лишаю як є." >&2
    exit 1
  fi

  mv "$EXCLUDE_FILE.tmp" "$EXCLUDE_FILE"
fi

# --- 3. Доказ --------------------------------------------------------------

STATUS="$(git -C "$UPSTREAM" status --short)"
if [ -n "$STATUS" ]; then
  echo
  echo "УВАГА: дерево EdgeTX не чисте:" >&2
  echo "$STATUS" >&2
  exit 1
fi

echo "Дерево EdgeTX чисте: git status --short порожній."
