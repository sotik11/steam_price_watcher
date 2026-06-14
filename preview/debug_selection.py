"""Click first watchlist row, then sample pixel colours to see what's
actually being drawn."""
import ctypes
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32
hwnd = user32.FindWindowW(None, "Steam Price Watcher")
if not hwnd:
    raise SystemExit("GUI not running")
user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.4)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def click(x, y):
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))

# First make sure we're on the Список tab.
click(rect.left + 45, rect.top + 50)
time.sleep(0.3)

# Click on row 1 ("The Sceptre of God") — somewhere safely in the row's
# Назва картки cell. From earlier screenshots that's around (200, 122).
click(rect.left + 200, rect.top + 122)
time.sleep(0.5)

user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
img = ImageGrab.grab(bbox=bbox).convert("RGB")
img.save("preview/debug_after_click.png")
print("Window rect:", bbox)
print("Selected row sample pixels (y=122 inside the row, away from text):")
for x in [25, 30, 40, 250, 500, 800]:
    print(f"  x={x:4d}: {img.getpixel((x, 122))}")
print("Row 2 sample (y=148):")
for x in [25, 250, 500]:
    print(f"  x={x:4d}: {img.getpixel((x, 148))}")
