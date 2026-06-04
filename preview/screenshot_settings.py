"""Switch to the Settings tab and screenshot the running GUI."""
import ctypes
import time
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32

hwnd = user32.FindWindowW(None, "Steam Card Price Watch")
if not hwnd:
    raise SystemExit("Steam Card Price Watch window not found")

user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.5)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
# Click on the Settings tab. We know from layout: Watchlist tab starts at
# ~x=15, Settings is the second tab. Both x ranges have ~80-100px width.
# Crude but reliable for the fixed tab order in our app.
import ctypes.wintypes as wt
# SendMessage WM_LBUTTONDOWN/UP at a point inside the Settings tab
# (relative to client, but window has title bar). Easier: send mouse via
# SetCursorPos + mouse_event. Or simplest: send a Tk-level event via tkinter
# is impossible from outside the process.
# Use SetCursorPos + mouse_event:
sx = rect.left + 130   # roughly the Settings tab x
sy = rect.top + 50     # tab band y (just below the title bar)
user32.SetCursorPos(sx, sy)
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.05)
user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.4)

# Re-fetch window rect in case anything shifted
user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
print("rect:", bbox)

img = ImageGrab.grab(bbox=bbox)
out = Path(__file__).resolve().parent / "gui_settings.png"
img.save(out)
print("saved", out)
