import struct, time, os, fcntl, glob

EV_SIZE = 24            # timeval(16) + type(2) + code(2) + value(4)
EV_KEY, EV_ABS = 1, 3
DURATION = 22

CH = {0: 'CH1  X', 1: 'CH2  Y', 2: 'CH3  Z', 3: 'CH4  rX',
      4: 'CH5  rY', 5: 'CH6  rZ', 6: 'CH7  Slider', 7: 'CH8  Dial'}

path = glob.glob('/dev/input/by-id/*TX16S*event-joystick')[0]
f = open(path, 'rb')
fcntl.fcntl(f, fcntl.F_SETFL, os.O_NONBLOCK)

vals = {}
btns = set()
end = time.time() + DURATION

while time.time() < end:
    try:
        d = f.read(EV_SIZE)
    except BlockingIOError:
        time.sleep(0.002)
        continue
    if not d or len(d) < EV_SIZE:
        time.sleep(0.002)
        continue
    _, _, typ, code, val = struct.unpack('qqHHi', d)
    if typ == EV_ABS and code in CH:
        vals.setdefault(code, set()).add(val)
    elif typ == EV_KEY and val:
        btns.add(code)

print(f"{'канал':14s} {'різних значень':>15s}   {'діапазон':>13s}   що це схоже на")
for code in sorted(CH):
    s = vals.get(code)
    if not s:
        print(f"{CH[code]:14s} {'—':>15s}   {'не рухалось':>13s}")
        continue
    n, lo, hi = len(s), min(s), max(s)
    if n == 1:
        kind = "нерухоме"
    elif n <= 3:
        kind = f"{n} чіткі рівні: {sorted(s)}"
    elif n <= 8:
        kind = f"{n} рівнів: {sorted(s)}"
    else:
        kind = "плавна вісь"
    print(f"{CH[code]:14s} {n:>15d}   {lo:>5d}..{hi:<5d}   {kind}")

print("\nкнопки (канали 9-32):", sorted(btns) if btns else "жодної не бачив")
