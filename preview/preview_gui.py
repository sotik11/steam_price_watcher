"""Renders the planned Steam-card watcher GUI and saves a screenshot.

Usage:
    python preview_gui.py --plain     -> preview_plain.png
    python preview_gui.py --bootstrap -> preview_bootstrap.png
"""
import sys
import tkinter as tk
from tkinter import ttk
from PIL import ImageGrab

USE_BOOTSTRAP = "--bootstrap" in sys.argv
THEME = "darkly"
if "--theme" in sys.argv:
    THEME = sys.argv[sys.argv.index("--theme") + 1]
OUT = f"preview_{THEME}.png" if USE_BOOTSTRAP else "preview_plain.png"

if USE_BOOTSTRAP:
    import ttkbootstrap as tb
    root = tb.Window(themename=THEME)
else:
    root = tk.Tk()

root.title("Steam Card Price Watch")
root.geometry("860x540")
root.attributes("-topmost", True)
root.lift()
root.focus_force()

nb = ttk.Notebook(root)
nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

# --- Watchlist tab (the one we screenshot) ---
tab_watch = ttk.Frame(nb)
nb.add(tab_watch, text="  Watchlist  ")

cols = ("name", "appid", "target", "last", "status")
tree = ttk.Treeview(tab_watch, columns=cols, show="headings", height=11)
headings = [
    ("name",   "Имя карточки",         300),
    ("appid",  "AppID",                 80),
    ("target", "Target",                90),
    ("last",   "Last seen",             90),
    ("status", "Status",               240),
]
for col, text, width in headings:
    tree.heading(col, text=text)
    tree.column(col, width=width, anchor="w")

rows = [
    ("Geralt — The Witcher 3",    "292030", "5.50 ₴", "6.20 ₴", "выше цели"),
    ("Triss Merigold",            "292030", "4.00 ₴", "3.85 ₴", "алерт отправлен 12:34"),
    ("Yennefer of Vengerberg",    "292030", "8.00 ₴", "7.55 ₴", "алерт отправлен 09:12"),
    ("Vesemir",                   "292030", "3.00 ₴", "4.10 ₴", "выше цели"),
    ("Ciri",                      "292030", "6.50 ₴", "6.45 ₴", "готов к алерту"),
    ("Dandelion",                 "292030", "2.00 ₴", "2.80 ₴", "выше цели"),
    ("Zoltan Chivay",             "292030", "2.50 ₴", "—",      "ещё не опрошено"),
    ("Cyberpunk 2077 — V",       "1091500", "9.00 ₴", "9.85 ₴", "выше цели"),
    ("Cyberpunk 2077 — Johnny",  "1091500", "12.00 ₴","11.40 ₴","алерт отправлен 14:01"),
]
for r in rows:
    tree.insert("", "end", values=r)
tree.pack(fill="both", expand=True, padx=10, pady=10)

btn_frame = ttk.Frame(tab_watch)
btn_frame.pack(fill="x", padx=10, pady=(0, 10))
for txt in ("Add by URL…", "Edit target", "Remove", "Check now", "Open in browser"):
    ttk.Button(btn_frame, text=txt).pack(side="left", padx=3)

# Other tabs as placeholders so the notebook shows them
for name in ("Settings", "Scheduler", "Log"):
    nb.add(ttk.Frame(nb), text=f"  {name}  ")

status = ttk.Label(
    root,
    text="  Задача активна  •  следующий запуск 14:25  •  карточек в списке: 9",
    relief="sunken",
    anchor="w",
)
status.pack(fill="x", side="bottom", ipady=2)


def capture():
    root.update_idletasks()
    root.update()
    x = root.winfo_rootx()
    y = root.winfo_rooty()
    w = root.winfo_width()
    h = root.winfo_height()
    # Capture including the OS title bar (~32px above winfo_rooty) and a small margin.
    bbox = (x - 8, y - 36, x + w + 8, y + h + 8)
    img = ImageGrab.grab(bbox=bbox)
    img.save(OUT)
    root.destroy()


root.after(1200, capture)
root.mainloop()
print(f"Saved {OUT}")
