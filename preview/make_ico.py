"""Запакувати Steam Card Watcher.png у .ico (16/32/48/64/128/256)."""
from PIL import Image

SRC = r'C:\Users\sotik\PycharmProjects\steam_card_price_watch\preview\Steam Card Watcher.png'
OUT = r'C:\Users\sotik\PycharmProjects\steam_card_price_watch\preview\Steam Card Watcher.ico'

im = Image.open(SRC).convert('RGBA')
W, H = im.size
print('source', W, H)

# Квадратимо прозорим паддингом (центруємо), нічого не обрізаючи.
side = max(W, H)
square = Image.new('RGBA', (side, side), (0, 0, 0, 0))
square.paste(im, ((side - W) // 2, (side - H) // 2), im)

sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# Базуємось на найбільшому (256), PIL сам згенерує всі розміри зі списку sizes.
base = square.resize((256, 256), Image.LANCZOS)
base.save(OUT, format='ICO', sizes=sizes)
print('ICO saved:', OUT)

# Перевірка: які розміри реально всередині
chk = Image.open(OUT)
print('sizes inside:', sorted(chk.ico.sizes()) if hasattr(chk, 'ico') else chk.info.get('sizes'))
