# Патч для EdgeTX

Тут лежить **наш** код. Він потім кладеться в `upstream/edgetx/radio/src/remote_ui/`
або підключається симлінком — спосіб визначається на етапі 1.

```
remote_ui/
  remote_ui.h        публічний інтерфейс (~5 функцій)
  protocol.cpp       кадрування, CRC16, розбір команд          [спільне]
  input.cpp          віртуальні клавіші / енкодер / сенсор      [спільне]
  capture_color.cpp  захоплення для кольорових пультів
  capture_bw.cpp     захоплення для ЧБ (етап 5)
  transport.cpp      UART або USB CDC
  rle.cpp            кодувальник RLE16
```

Окремо — `patches/hooks.patch`: ~34 рядки у 9 файлах EdgeTX.
Повний перелік дозволених місць — `docs/05-hooks.md`. Виходити за нього не можна.

## Правило комітів

```
commit A: remote_ui/**        тільки нові файли
commit B: patches/hooks.patch тільки гачки
```
