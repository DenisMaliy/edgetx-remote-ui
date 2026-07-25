import fcntl, struct, array

JSIOCGAXES    = 0x80016a11
JSIOCGBUTTONS = 0x80016a12
JSIOCGNAME    = 0x80806a13

with open('/dev/input/js0', 'rb') as f:
    buf = array.array('B', [0])
    fcntl.ioctl(f, JSIOCGAXES, buf)
    axes = buf[0]
    fcntl.ioctl(f, JSIOCGBUTTONS, buf)
    btns = buf[0]
    name = array.array('B', [0] * 128)
    fcntl.ioctl(f, JSIOCGNAME, name)
    label = bytes(name).split(b'\x00')[0].decode('utf-8', 'replace')

print(f"назва:  {label}")
print(f"осей:   {axes}")
print(f"кнопок: {btns}")

# справжній діапазон осі — тільки з evdev, joydev його ховає
ABS_NAMES = ['X', 'Y', 'Z', 'rX', 'rY', 'rZ', 'Throttle', 'Rudder',
             'Wheel', 'Gas', 'Brake']
try:
    with open('/dev/input/event19', 'rb') as e:
        print("\nсправжні діапазони осей (з evdev):")
        for a in range(11):
            EVIOCGABS = (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + a)
            b = array.array('B', [0] * 24)
            try:
                fcntl.ioctl(e, EVIOCGABS, b)
            except OSError:
                continue
            val, lo, hi, fuzz, flat, res = struct.unpack('iiiiii', bytes(b))
            if lo == 0 and hi == 0:
                continue
            print(f"  {ABS_NAMES[a]:9s} від {lo} до {hi}   рівнів {hi - lo + 1}"
                  f"   поточне {val}")
except PermissionError:
    print("\nevdev недоступний без прав — діапазон не зчитати цим шляхом")
