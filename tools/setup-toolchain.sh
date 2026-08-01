#!/usr/bin/env bash
# Ставить усе, що потрібно для збірки прошивки пульта, у теку поза git:
#
#   1. Arm GNU Toolchain 14.2.rel1 — офіційний тарбол ARM;
#   2. хостові Python-залежності збірки (Pillow, lz4, libclang) — у власний
#      virtualenv, без sudo й без docker.
#
# Навіщо не системний arm-none-eabi-gcc: у системі стоїть 16.1.0 (Arch), а
# EdgeTX 2.12 пінує рівно 14.2.rel1 і зупиняє збірку жорстким FATAL_ERROR ще
# до компіляції (radio/src/CMakeLists.txt). Рішення людини 2026-08-01:
# USE_UNSUPPORTED_TOOLCHAIN не використовувати для збірок, що йдуть у
# пульт — 16.1 проти 14.2 це два мажорні релізи GCC, інша кодогенерація й
# інший newlib, а пульт керує моделлю в повітрі й не має екрана.
#
# Навіщо компілятор і Python в ОДНОМУ скрипті, а не в двох: перевстановлення
# системи 2026-08-01 показало, що середовище тут недовговічне. Два скрипти
# означають, що колись запустять лише один — і збірка впаде на половині
# дороги з повідомленням, яке ні про що не говорить.
#
# Скрипт ідемпотентний: повторний запуск на вже встановленому наборі нічого
# не качає й нічого не ламає.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VERSION="14.2.rel1"
VERSION_NUM="14.2.1"     # те, що видає --version і що звіряє CMakeLists.txt
ARCHIVE="arm-gnu-toolchain-${VERSION}-x86_64-arm-none-eabi.tar.xz"
URL="https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/${VERSION}/binrel/${ARCHIVE}"
CHECKSUM_URL="${URL}.asc"

# Офіційний MD5 з ${CHECKSUM_URL} (ARM публікує його саме так, під розширенням
# .asc, хоча це не PGP-підпис, а рядок "md5  ім'я_файла" — перевірено запитом
# до armkeil.blob.core.windows.net 2026-08-01). Пінується тут, а не
# перечитується щоразу з мережі: тарбол один раз завантажується й довше не
# змінюється, а сталий рядок легше звірити оком у диффі цього файлу.
EXPECTED_MD5="fcdcd7c8d5b22d2d0cc6bf3721686e69"

# SHA256 порахований нами з архіву, який уже пройшов звірку за MD5 вище
# (2026-08-01). ARM його не публікує; сенс не в тому, щоб довіряти ARM удруге,
# а в тому, що MD5 на колізії ламається, і другий, стійкий відбиток робить
# підміну архіву між ARM і нами практично неможливою.
EXPECTED_SHA256="62a63b981fe391a9cbad7ef51b17e49aeaa3e7b0d029b36ca1e9c3b2a9b78823"

# Хостові Python-залежності збірки. Версії пінуються числами навмисно:
# без пінів збірка стає заручником майбутнього релізу будь-якого з трьох
# пакетів, а ламатися вона буде на пульті без екрана.
#
#   Pillow   — radio/util/encode-bitmap.py, растри у .lbm
#   lz4      — там само, стиснення .lbm
#   libclang — radio/util/generate_datacopy.py через find_clang.py.
#              Саме PyPI-пакет, а не системний clang/llvm: він носить
#              libclang.so в собі, тобто не потребує sudo й сотень МБ.
PY_PILLOW="12.3.0"
PY_LZ4="4.4.5"
PY_LIBCLANG="18.1.1"

TOOLCHAIN_ROOT="$ROOT/toolchain"
DEST="$TOOLCHAIN_ROOT/arm-gnu-toolchain-${VERSION}-x86_64-arm-none-eabi"
GCC="$DEST/bin/arm-none-eabi-gcc"
VENV="$TOOLCHAIN_ROOT/py-fw-build"
VENV_PY="$VENV/bin/python3"

# --- .gitignore: тека не має потрапити в історію ніколи -------------------

GITIGNORE="$ROOT/.gitignore"
IGNORE_LINE="toolchain/"
if ! grep -qxF "$IGNORE_LINE" "$GITIGNORE" 2>/dev/null; then
  {
    echo ""
    echo "# Пінований компілятор для прошивки пульта (tools/setup-toolchain.sh)"
    echo "# — ~150 МБ, тягнеться з ARM щоразу наново, у git їм не місце"
    echo "$IGNORE_LINE"
  } >> "$GITIGNORE"
  echo "Додано '$IGNORE_LINE' у .gitignore"
fi

mkdir -p "$TOOLCHAIN_ROOT"

# --- 1. Компілятор ---------------------------------------------------------

install_toolchain() {
  # Усі перевірки — до розпакування. Архів, що не пройшов звірку, не має
  # лишити по собі частково розпаковану теку.
  local tmp
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  echo "Качаю $ARCHIVE (~143 МБ) з $URL"
  curl -fL --progress-bar -o "$tmp/$ARCHIVE" "$URL"

  echo "Звіряю MD5 із опублікованим ($CHECKSUM_URL)…"
  local actual_md5
  actual_md5="$(md5sum "$tmp/$ARCHIVE" | awk '{print $1}')"
  if [ "$actual_md5" != "$EXPECTED_MD5" ]; then
    echo "MD5 не збігається!" >&2
    echo "  очікували: $EXPECTED_MD5" >&2
    echo "  отримали:  $actual_md5" >&2
    echo "Архів не той, що опублікувала ARM — компілятор, що зібере код для" >&2
    echo "літаючої моделі, не має права бути 'приблизно тим самим'." >&2
    exit 1
  fi
  echo "MD5 збігається."

  echo "Звіряю SHA256…"
  local actual_sha
  actual_sha="$(sha256sum "$tmp/$ARCHIVE" | awk '{print $1}')"
  if [ "$actual_sha" != "$EXPECTED_SHA256" ]; then
    echo "SHA256 не збігається!" >&2
    echo "  очікували: $EXPECTED_SHA256" >&2
    echo "  отримали:  $actual_sha" >&2
    echo "MD5 зійшовся, а SHA256 — ні. Це або колізія MD5, або наш пін" >&2
    echo "застарів; у будь-якому разі розпаковувати це не можна." >&2
    exit 1
  fi
  echo "SHA256 збігається."

  echo "Розпаковую в $TOOLCHAIN_ROOT"
  tar -xJf "$tmp/$ARCHIVE" -C "$TOOLCHAIN_ROOT"

  if [ ! -x "$GCC" ]; then
    echo "Розпакувалось, але $GCC не знайдено — перевір вміст архіву." >&2
    exit 1
  fi

  local installed
  installed="$("$GCC" -dumpversion)"
  if [ "$installed" != "$VERSION_NUM" ]; then
    echo "УВАГА: розпакований компілятор видає версію $installed, очікувалась $VERSION_NUM" >&2
  fi
  echo "Компілятор поставлено: $DEST"
}

if [ -x "$GCC" ]; then
  INSTALLED="$("$GCC" -dumpversion)"
  if [ "$INSTALLED" = "$VERSION_NUM" ]; then
    echo "Компілятор уже стоїть: $DEST"
    "$GCC" --version | head -1
  else
    echo "У $DEST лежить версія $INSTALLED, очікувалась $VERSION_NUM — перевстановлюю" >&2
    rm -rf "$DEST"
    install_toolchain
  fi
else
  install_toolchain
fi

# --- 2. Хостові Python-залежності -----------------------------------------
#
# Окремий virtualenv, а не системний Python: Arch забороняє pip install у
# систему (PEP 668), а ставити pacman-ом означало б sudo й системний clang.
# --system-site-packages потрібен, бо pyyaml і jinja2 EdgeTX бере системні,
# і дублювати їх у venv нема сенсу.

py_deps_ok() {
  [ -x "$VENV_PY" ] || return 1
  "$VENV_PY" - "$PY_PILLOW" "$PY_LZ4" "$PY_LIBCLANG" <<'EOF' >/dev/null 2>&1
import sys
from importlib.metadata import version, PackageNotFoundError

want = dict(zip(("pillow", "lz4", "libclang"), sys.argv[1:4]))
try:
    for pkg, expected in want.items():
        if version(pkg) != expected:
            sys.exit(1)
except PackageNotFoundError:
    sys.exit(1)

# Мало збігу версій — треба, щоб воно ще й імпортувалось: саме на імпорті
# libclang падає, коли в системі немає libclang.so.
import PIL.Image          # noqa: F401
import lz4.block          # noqa: F401
import yaml               # noqa: F401  (системний, через --system-site-packages)
import jinja2             # noqa: F401  (там само)
from clang.cindex import Index
index = Index.create()

# ⚠️ Імпорту мало, і це не теорія: 2026-08-01 збірка симулятора впала на
# `fatal error: 'stdbool.h' file not found`, коли `Index.create()` проходив
# бездоганно.
#
# Причина: пакет `libclang` із pip дає саме `libclang.so` і **не дає
# вбудованих заголовків** clang (`stdbool.h`, `stddef.h` і решта). Вони
# приходять тільки із системним clang. `radio/util/find_clang.py` шукає їх у
# `/usr/lib/clang` і сусідніх місцях; не знайшовши — тихо йде далі, і падає
# аж генератор `datacopy.inc`, за кілька хвилин збірки й у чужому файлі.
#
# Тому перевіряємо те саме, що перевіряє сам EdgeTX: чи розбирається
# найпростіший файл із `#include <stdbool.h>`.
tu = index.parse("probe.c", ["-x", "c"],
                 [("probe.c", "#include <stdbool.h>\nbool b;\n")])
if any(d.severity >= 3 for d in tu.diagnostics):
    sys.exit(2)
EOF
}

if py_deps_ok; then
  echo "Python-залежності вже стоять: $VENV"
  "$VENV_PY" -c "
from importlib.metadata import version
for p in ('pillow', 'lz4', 'libclang'):
    print('  %-10s %s' % (p, version(p)))
"
else
  if [ ! -x "$VENV_PY" ]; then
    echo "Створюю virtualenv $VENV"
    python3 -m venv --system-site-packages "$VENV"
  fi
  echo "Ставлю Pillow==$PY_PILLOW, lz4==$PY_LZ4, libclang==$PY_LIBCLANG"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check \
    "Pillow==$PY_PILLOW" "lz4==$PY_LZ4" "libclang==$PY_LIBCLANG"

  if ! py_deps_ok; then
    # ⚠️ Розрізняємо дві різні біди, бо лікуються вони по-різному, а виглядають
    # однаково. Брак пакета лікується цим скриптом; брак заголовків clang —
    # ні, і сказати про це треба прямо, а не залишати людину з «щось не
    # імпортується».
    if ! "$VENV_PY" -c "from clang.cindex import Index; Index.create()" >/dev/null 2>&1; then
      echo "Залежності поставились, але перевірка не пройшла — щось не імпортується." >&2
      exit 1
    fi

    cat >&2 <<'MSG'

⚠️ libclang є, а його вбудованих заголовків немає.

Пакет libclang із pip дає лише саму бібліотеку. Заголовки (stdbool.h,
stddef.h і решта) приходять тільки із системним clang, і без них збірка
СИМУЛЯТОРА падає на генераторі datacopy.inc:

    fatal error: 'stdbool.h' file not found

Прошивку пульта це не зачіпає — вона збирається й так.

Полагодити (Arch), одна команда, і вона потребує sudo, тому цей скрипт її
не виконує:

    sudo pacman -S clang

MSG
    exit 1
  fi
  echo "Python-залежності поставлено: $VENV"
fi

# --- Як цим користуватись --------------------------------------------------

cat <<EOF

Готово.

Компілятор:  $("$GCC" --version | head -1)
Python:      $VENV

Збірка прошивки пульта — три змінні й дві команди:

  export PATH="$DEST/bin:\$PATH"
  # generate_datacopy.py ходить у libclang з PyPI, а той не знає, де лежать
  # стандартні заголовки компілятора (stdbool.h та інші) — показуємо йому:
  export CPATH="$DEST/lib/gcc/arm-none-eabi/$VERSION_NUM/include"

  cd upstream/edgetx && mkdir -p build-fw && cd build-fw
  cmake -DCMAKE_BUILD_TYPE=Release -DPCB=X10 -DPCBREV=TX16S \\
        -DREMOTE_UI=YES -DEdgeTX_SUPERBUILD=YES \\
        -DPython3_EXECUTABLE=$VENV_PY ..
  cmake --build . --target arm-none-eabi-configure -j\$(nproc)
  cmake --build arm-none-eabi --target firmware -j\$(nproc)
EOF
