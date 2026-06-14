"""Click the token edit pencil and screenshot the resulting state."""
import ctypes
import time
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32

hwnd = user32.FindWindowW(None, "Steam Price Watcher")
if not hwnd:
    raise SystemExit("Steam Price Watcher window not found")

user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.5)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def click(x_abs, y_abs):
    user32.SetCursorPos(x_abs, y_abs)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
# Settings tab (2nd tab in the band, ~x=130 from window left, y≈70).
click(rect.left + 130, rect.top + 70)
time.sleep(0.3)

# Get fresh rect, locate pencil near token entry. From the screenshot the
# pencil sits at roughly (rect.left + 525, rect.top + 95).
user32.GetWindowRect(hwnd, ctypes.byref(rect))
click(rect.left + 525, rect.top + 95)
time.sleep(0.3)

# Also click the chat-id pencil so both rows show the active state.
click(rect.left + 405, rect.top + 125)
time.sleep(0.3)

user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
print("rect:", bbox)

img = ImageGrab.grab(bbox=bbox)
out = Path(__file__).resolve().parent / "gui_settings_editing.png"
img.save(out)
print("saved", out)
