"""Grab a screenshot of the running Steam Price Watcher window."""
import ctypes
import time
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32

# FindWindowW(lpClassName, lpWindowName)
hwnd = user32.FindWindowW(None, "Steam Price Watcher")
if not hwnd:
    raise SystemExit("Steam Price Watcher window not found")

# SW_RESTORE then bring to top
user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.6)

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
print("rect:", bbox)

img = ImageGrab.grab(bbox=bbox)
out = Path(__file__).resolve().parent / "gui_after_cosmetics.png"
img.save(out)
print("saved", out)
