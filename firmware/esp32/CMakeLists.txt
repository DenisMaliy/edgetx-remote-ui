# Міст TX16S Remote UI — чистий ESP-IDF (ADR-0004).
#
# Збирач — idf.py на ESP-IDF v5.5.4 з ~/.espressif (крок 2.6a, DECISIONS.md).
# PlatformIO цей проєкт не збирає: там Arduino-середовище людини.
#
#   . ~/.espressif/tools/activate_idf_v5.5.4.sh
#   idf.py set-target esp32 && idf.py build
#   idf.py -B build-c3 -DIDF_TARGET=esp32c3 build

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(tx16s_remote_ui_bridge)
