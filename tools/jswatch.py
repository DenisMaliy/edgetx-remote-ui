import struct, time, os, fcntl

f = open('/dev/input/js0', 'rb')
fcntl.fcntl(f, fcntl.F_SETFL, os.O_NONBLOCK)

end = time.time() + 15
axes = {}
btns = set()

while time.time() < end:
    try:
        d = f.read(8)
    except BlockingIOError:
        time.sleep(0.005)
        continue
    if not d or len(d) < 8:
        time.sleep(0.005)
        continue
    _, val, typ, num = struct.unpack('IhBB', d)
    if typ & 0x02:
        a = axes.setdefault(num, [val, val])
        a[0] = min(a[0], val)
        a[1] = max(a[1], val)
    elif typ & 0x01 and val:
        btns.add(num)

moved = 0
for n in sorted(axes):
    lo, hi = axes[n]
    span = hi - lo
    if span > 1000:
        moved += 1
    print(f"вісь {n:2d}: від {lo:6d} до {hi:6d}   розмах {span}")
print(f"осей, що реально рухались: {moved}")
print("кнопки:", sorted(btns) if btns else "не натискались")
