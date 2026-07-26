#!/usr/bin/env bash
#
# Перезнімає firmware/edgetx-patch/patches/hooks.patch із поточного стану
# дерева EdgeTX.
#
# Робочий цикл такий: гачок правиться прямо в upstream/edgetx (там його видно
# в контексті чужого коду й одразу збирається), а потім знімається сюди. Без
# цього кроку tools/patch-revert.sh відмовиться працювати — і правильно
# зробить, бо інакше правки зникли б без сліду.
#
# Скрипт сам стежить, щоб у патч не потрапило нічого поза переліком
# docs/05-hooks.md.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="$REPO/upstream/edgetx"
PATCH="$REPO/firmware/edgetx-patch/patches/hooks.patch"

# Закритий перелік файлів, які нам дозволено чіпати (docs/05-hooks.md).
# Розширювати його можна лише разом із документом — і не мовчки.
ALLOWED=(
  "radio/src/dataconstants.h"                            # пункт 1: режим порту
  "radio/src/storage/yaml/yaml_datastructs_funcs.cpp"    # пункт 2: назва в YAML
  "radio/src/serial.cpp"                                 # пункт 3: драйвер і швидкість
  "radio/src/gui/gui_common.cpp"                         # пункт 4: доступність режиму
  "radio/src/gui/colorlcd/lcd.cpp"                       # пункт 5: захоплення екрана
  "radio/src/keys.cpp"                                   # пункт 6: клавіші й тримери
  "radio/src/gui/colorlcd/LvglWrapper.cpp"               # пункт 7: енкодер і сенсор
  "radio/src/CMakeLists.txt"                             # пункт 8: збірка
  "radio/src/translations/translation_def.h"             # пункт 9: назва режиму
)

if [ ! -d "$UPSTREAM/.git" ]; then
  echo "Немає $UPSTREAM" >&2
  exit 1
fi

# Свідомо НЕ `git diff --name-only`: він показує лише незастейджені зміни
# відстежуваних файлів. Повз нього пройшли б і `git add`-нута правка, і новий
# файл, покладений у чуже дерево, — тобто рівно те, що вартовий має ловити.
# `status --porcelain` бачить усе, зокрема невідстежуване.
CHANGED="$(git -C "$UPSTREAM" status --porcelain | sed 's/^...//' | sed 's/.* -> //')"

if [ -z "$CHANGED" ]; then
  echo "У дереві EdgeTX немає змін — знімати нема чого." >&2
  exit 1
fi

RC=0
while IFS= read -r file; do
  ok=0
  for allowed in "${ALLOWED[@]}"; do
    [ "$file" = "$allowed" ] && ok=1
  done
  if [ "$ok" -eq 0 ]; then
    echo "ЗАБОРОНЕНО: $file немає в переліку docs/05-hooks.md" >&2
    RC=1
  fi
done <<< "$CHANGED"

if [ "$RC" -ne 0 ]; then
  echo >&2
  echo "Правити чужі файли поза переліком не можна без ADR. Патч не знято." >&2
  exit 1
fi

mkdir -p "$(dirname "$PATCH")"

# Пишемо збоку й підставляємо лише перевірене. Пряме `> "$PATCH"` обрізало б
# наявний файл ще до того, як git щось напише, і невдала спроба лишила б нас
# зовсім без патча.
#
# `diff HEAD`, а не просто `diff`: інакше в патч не потрапили б зміни, які
# хтось устиг застейджити.
git -C "$UPSTREAM" diff HEAD -- "${ALLOWED[@]}" > "$PATCH.tmp"

if ! grep -q '^@@' "$PATCH.tmp"; then
  echo "Патч вийшов без жодного шматка — нічого не знято." >&2
  echo "Наявний $PATCH лишився недоторканим." >&2
  rm -f "$PATCH.tmp"
  exit 1
fi

mv "$PATCH.tmp" "$PATCH"

echo "Знято в $PATCH"
echo "Файлів: $(echo "$CHANGED" | wc -l), рядків у патчі: $(wc -l < "$PATCH")"
