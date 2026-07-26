# Remote UI — наші файли у складі збірки EdgeTX.
#
# Підключається з `radio/src/CMakeLists.txt` рядком `add_subdirectory(remote_ui)`
# усередині `if(REMOTE_UI)` — це пункт 8 з docs/05-hooks.md.
#
# Тут навмисно немає ані project(), ані cmake_minimum_required(): файл
# призначений для включення в чуже дерево і не має права переставляти чужі
# політики CMake.
#
# Окремої статичної бібліотеки теж немає, і це свідомо. EdgeTX збирає радіо з
# одного списку `SRC`: із нього робиться і об'єктна бібліотека `radiolib_native`
# для симулятора, і ціль `firmware` для пульта. Свій target довелося б
# прилінковувати до обох, тобто чіпати чужий файл ще у двох місцях. Дописати
# файли у вже наявний список — рівно нуль зайвих рядків у чужому коді.
#
# Шляхи відносні до `radio/src`, бо саме там створюються цілі.

set(SRC ${SRC}
  remote_ui/capture.cpp
  remote_ui/crc16.cpp
  remote_ui/hello.cpp
  remote_ui/protocol.cpp
  remote_ui/rle16.cpp
  remote_ui/tile.cpp
  remote_ui/transport_simu.cpp
  PARENT_SCOPE
)

# Прапорець REMOTE_UI ставить викликач (`add_definitions(-DREMOTE_UI)` поряд із
# `add_subdirectory`). Без нього вміст усіх наших .cpp вимикається препроцесором
# і об'єктні файли виходять порожніми — так задумано (CLAUDE.md).
