# Remote UI — статична бібліотека з нашим кодом.
#
# Підключається з `radio/src/CMakeLists.txt` рядком `add_subdirectory(remote_ui)`
# усередині `if(REMOTE_UI)` — це пункт 8 з docs/05-hooks.md, і робиться він
# окремою задачею («коміт B: гачки»). Тут навмисно немає ані project(), ані
# cmake_minimum_required(): файл призначений для включення в чуже дерево і не
# має права переставляти чужі політики CMake.
#
# Залежностей від EdgeTX у цих файлах немає — ані заголовків, ані типів.
# Той самий код збирається звичайним g++ на комп'ютері (див. README.md).

add_library(remote_ui STATIC
  crc16.cpp
  protocol.cpp
)

target_include_directories(remote_ui PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

# Без цього визначення весь вміст .cpp вимикається і бібліотека виходить
# порожньою — так задумано (CLAUDE.md: усе під прапорцем REMOTE_UI).
target_compile_definitions(remote_ui PUBLIC REMOTE_UI)
