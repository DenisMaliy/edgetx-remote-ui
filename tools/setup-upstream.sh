#!/usr/bin/env bash
# Клонує EdgeTX у upstream/edgetx (у .gitignore, у наш репозиторій не потрапляє).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/upstream/edgetx"
# Базуємось на релізній гілці, що збігається з прошивкою пульта (2.12),
# бо вміст SD-картки прив'язаний до версії. Див. CLAUDE.md.
BRANCH="${EDGETX_BRANCH:-2.12}"

if [ -d "$DEST/.git" ]; then
  echo "EdgeTX вже є у $DEST — оновлюю"
  git -C "$DEST" fetch --depth 1 origin "$BRANCH"
  git -C "$DEST" checkout -f FETCH_HEAD
else
  mkdir -p "$ROOT/upstream"
  echo "Клоную EdgeTX, гілка $BRANCH (це ~320 МБ, буде не швидко)"
  git clone --depth 1 --branch "$BRANCH" https://github.com/EdgeTX/edgetx.git "$DEST" \
    || { echo "⚠️  гілки $BRANCH немає — перевір актуальну назву в репозиторії EdgeTX"; exit 1; }
fi

echo
echo "Підмодулі (потрібні для збірки, LVGL і libopenui):"
git -C "$DEST" submodule update --init --depth 1 || \
  echo "  ⚠️  не всі підмодулі підтягнулись — перевір вручну"

echo
echo "Готово (гілка $BRANCH). Далі: docs/04-plan.md, етап 1."
echo "Щоб перевірити наш патч на свіжому main:  EDGETX_BRANCH=main $0"
echo "Для збірки симулятора знадобляться cmake, gcc, SDL2, Qt (див. документацію EdgeTX)."
