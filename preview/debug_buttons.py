"""Sample pixel colours from the action buttons to see what's actually drawn."""
import ctypes
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32
hwnd = user32.FindWindowW(None, "Steam Card Price Watch")
if not hwnd:
    raise SystemExit("GUI not running")
user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.4)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
img = ImageGrab.grab(bbox=bbox).convert("RGB")
print("Window rect:", bbox)
print("Window size:", img.size)

# Action button row sits roughly at y=565 (window-relative). Sample several
# x positions inside "Додати за URL..." and "Оновити зараз".
print("Action button pixels (y=565):")
for x in [40, 60, 80, 100, 200, 300, 400, 500, 600]:
    print(f"  x={x:4d}: {img.getpixel((x, 565))}")
print("And y=575:")
for x in [40, 80, 200, 400]:
    print(f"  x={x:4d}: {img.getpixel((x, 575))}")
# Also sample green Купив button for reference (y=600).
print("Купив button pixels (y=600):")
for x in [25, 40, 60]:
    print(f"  x={x:4d}: {img.getpixel((x, 600))}")
