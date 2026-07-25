#!/usr/bin/env bash
# Клонує EdgeTX у upstream/edgetx (у .gitignore, у наш репозиторій не потрапляє).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/upstream/edgetx"
# Закріплено на **тезі** тієї самої версії, що залита в пульт — v2.12.2.
# Не на гілці 2.12: її голова їде далі за реліз, і «та сама версія» була б
# збігом, а не гарантією. Див. CLAUDE.md і tasks/0002.
REF="${EDGETX_BRANCH:-v2.12.2}"

if [ -d "$DEST/.git" ]; then
  echo "EdgeTX вже є у $DEST — оновлюю"
  git -C "$DEST" fetch --depth 1 origin "$REF"
  git -C "$DEST" checkout -f FETCH_HEAD
else
  mkdir -p "$ROOT/upstream"
  echo "Клоную EdgeTX, $REF (це ~320 МБ, буде не швидко)"
  git clone --depth 1 --branch "$REF" https://github.com/EdgeTX/edgetx.git "$DEST" \
    || { echo "⚠️  $REF немає — перевір актуальну назву тега чи гілки в репозиторії EdgeTX"; exit 1; }
fi

echo
echo "Підмодулі (потрібні для збірки, LVGL і libopenui):"
git -C "$DEST" submodule update --init --depth 1 || \
  echo "  ⚠️  не всі підмодулі підтягнулись — перевір вручну"

echo
echo "Готово ($REF). Далі: docs/04-plan.md, етап 1."
echo "Щоб перевірити наш патч на свіжому main:  EDGETX_BRANCH=main $0"
echo "Для збірки симулятора знадобляться cmake, gcc, SDL2, Qt (див. документацію EdgeTX)."
