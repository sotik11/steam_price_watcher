"""Mock-up: show what a "wishlist gold" selection would look like.

We can't apply a real CSS-style gradient inside ttk.Treeview (it only
supports solid colours per tag), but for visual evaluation we render
two PNGs:

  selection_gold_solid.png    — solid #5D5119 (the gradient midpoint
                                blended over the dark bg). This is what
                                we CAN actually apply in code.

  selection_gold_gradient.png — pseudo-gradient: rgb(241,196,15) painted
                                on top of the row at 0.1 → 0.5 alpha
                                along the 135° diagonal, mirroring the
                                wishlist CSS exactly. Reference only —
                                won't translate to real Tk.
"""
import ctypes
import time
from pathlib import Path

from PIL import ImageGrab

# Wishlist accent — rgb(241, 196, 15) = gold #F1C40F.
GOLD = (0xF1, 0xC4, 0x0F)

# First data row inside the Watchlist tab (eyeballed from screenshots).
ROW_Y_TOP, ROW_Y_BOTTOM = 110, 138
TABLE_X_LEFT, TABLE_X_RIGHT = 15, 1085

user32 = ctypes.windll.user32

hwnd = user32.FindWindowW(None, "Steam Card Price Watch")
if not hwnd:
    raise SystemExit("Steam Card Price Watch window not found")
user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.4)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
bbox = (rect.left, rect.top, rect.right, rect.bottom)
print("rect:", bbox)

base_img = ImageGrab.grab(bbox=bbox).convert("RGB")


def blend(under, over, alpha):
    return (
        int(alpha * over[0] + (1 - alpha) * under[0]),
        int(alpha * over[1] + (1 - alpha) * under[1]),
        int(alpha * over[2] + (1 - alpha) * under[2]),
    )


def variant_solid():
    img = base_img.copy()
    px = img.load()
    # 30% gold over each row pixel — visually similar to the gradient midpoint.
    for y in range(ROW_Y_TOP, min(ROW_Y_BOTTOM, img.height)):
        for x in range(TABLE_X_LEFT, min(TABLE_X_RIGHT, img.width)):
            px[x, y] = blend(px[x, y], GOLD, 0.30)
    return img


def variant_gradient():
    img = base_img.copy()
    px = img.load()
    span = (TABLE_X_RIGHT - TABLE_X_LEFT) + (ROW_Y_BOTTOM - ROW_Y_TOP)
    for y in range(ROW_Y_TOP, min(ROW_Y_BOTTOM, img.height)):
        for x in range(TABLE_X_LEFT, min(TABLE_X_RIGHT, img.width)):
            # Diagonal position 0..1, then map to alpha 0.1..0.5.
            dx = x - TABLE_X_LEFT
            dy = y - ROW_Y_TOP
            t = (dx + dy) / span
            alpha = 0.10 + (0.50 - 0.10) * t
            px[x, y] = blend(px[x, y], GOLD, alpha)
    return img


out_dir = Path(__file__).resolve().parent
variant_solid().save(out_dir / "selection_gold_solid.png")
variant_gradient().save(out_dir / "selection_gold_gradient.png")
print("saved selection_gold_solid.png and selection_gold_gradient.png")
