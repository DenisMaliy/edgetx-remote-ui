#!/usr/bin/env python3
"""Перевірка перетворювача USB-UART на потрібній швидкості.

Замкніть TX на RX перемичкою і запустіть:
    python3 tools/uart_loopback.py /dev/ttyUSB0 921600

Нуль розбіжностей — перетворювач годиться.
Якщо є втрати — або швидкість завелика для цього чипа (звичайний CP2102
має стелю 1 Мбод), або чип не той, за який себе видає.
"""
import sys, os, time

try:
    import serial
except ImportError:
    sys.exit("Немає pyserial:  pip install --user pyserial")

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 921600
total = 1 << 20            # 1 МіБ
chunk = 4096

print(f"Порт {port}, швидкість {baud}, обсяг {total // 1024} КіБ")
print("TX має бути замкнений на RX.\n")

with serial.Serial(port, baud, timeout=2) as s:
    s.reset_input_buffer()
    sent = recv = bad = 0
    t0 = time.time()
    while sent < total:
        block = os.urandom(chunk)
        s.write(block)
        back = s.read(len(block))
        if len(back) != len(block):
            print(f"⚠️  недоотримано {len(block) - len(back)} Б на зсуві {sent}")
        bad += sum(a != b for a, b in zip(block, back))
        sent += len(block)
        recv += len(back)
    dt = time.time() - t0

print(f"\nВідправлено: {sent} Б")
print(f"Отримано:    {recv} Б")
print(f"Розбіжностей: {bad}")
print(f"Швидкість:   {recv / dt / 1024:.1f} КіБ/с")
print("\n" + ("✅ Годиться." if bad == 0 and recv == sent
              else "❌ Є втрати — знизьте швидкість або візьміть інший перетворювач."))
