"""Generates icon.ico for the Steam Card Price Watch shortcut.

Style: a stylised Steam trading card on a dark blue background, with a small
green notification dot in the corner (the bell-equivalent — this app sends
alerts). No glyphs, so the icon is font-independent and stays crisp at 16 px.

Palette follows the superhero ttkbootstrap theme:
  * dark blue background     (#2c3e50)
  * cyan card                (#5bc0de — superhero "info")
  * darker inner artwork     (#2c3e50)
  * green notification dot   (#28a745 — superhero "success")
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "icon.ico"

BG_DARK = (44, 62, 80, 255)
CARD = (91, 192, 222, 255)
CARD_DARK = (44, 62, 80, 255)
WHITE = (255, 255, 255, 255)
GREEN = (40, 167, 69, 255)


def _make(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ---- 1. Dark rounded square background -------------------------------
    pad = max(1, size // 32)
    bg_box = (pad, pad, size - pad, size - pad)
    d.rounded_rectangle(bg_box, radius=size // 7, fill=BG_DARK)

    # ---- 2. Trading card ------------------------------------------------
    # Generous margins so the card dominates the icon and reads at 16 px.
    cm_x = int(size * 0.20)
    cm_y = int(size * 0.16)
    card = (cm_x, cm_y, size - cm_x, size - cm_y)
    card_r = max(2, size // 22)
    border_w = max(1, size // 64)
    d.rounded_rectangle(card, radius=card_r, fill=CARD,
                        outline=WHITE, width=border_w)

    card_w = card[2] - card[0]
    card_h = card[3] - card[1]

    # ---- 3. Inner thin frame — imitates the inset of a real trading card --
    inset = max(2, size // 28)
    inner = (card[0] + inset, card[1] + inset,
             card[2] - inset, card[3] - inset)
    inner_r = max(1, size // 32)
    inner_w = max(1, size // 96)
    d.rounded_rectangle(inner, radius=inner_r,
                        outline=WHITE, width=inner_w)

    # ---- 4. "Artwork" panel in the upper ~60% of the card -----------------
    art_pad = max(2, size // 32)
    art_top = inner[1] + art_pad
    art_bot = inner[1] + int(card_h * 0.55)
    art_left = inner[0] + art_pad
    art_right = inner[2] - art_pad
    d.rounded_rectangle(
        (art_left, art_top, art_right, art_bot),
        radius=max(1, size // 40),
        fill=CARD_DARK,
    )

    # ---- 5. Three "text lines" in the lower portion ----------------------
    # Only draw at sizes where they remain readable. Below ~32 px they turn
    # into mud, so we skip them and keep the card cleaner.
    if size >= 32:
        line_h = max(1, size // 32)
        gap = max(1, size // 28)
        line_left = inner[0] + art_pad
        line_right = inner[2] - art_pad
        # First text line starts a bit below the artwork.
        ly = art_bot + max(2, size // 18)
        line_widths = [1.0, 0.75, 0.55]
        for w_frac in line_widths:
            width = int((line_right - line_left) * w_frac)
            d.rounded_rectangle(
                (line_left, ly, line_left + width, ly + line_h),
                radius=line_h // 2,
                fill=WHITE,
            )
            ly += line_h + gap
            if ly + line_h > inner[3] - art_pad:
                break

    # ---- 6. Green notification dot ---------------------------------------
    dot_r = max(2, size // 9)
    dot_cx = size - pad - dot_r - size // 48
    dot_cy = pad + dot_r + size // 48
    d.ellipse(
        (dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r),
        fill=GREEN, outline=WHITE, width=max(1, size // 64),
    )

    return img


def main() -> None:
    sizes = [256, 128, 64, 48, 32, 24, 16]
    images = [_make(s) for s in sizes]
    images[0].save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    images[0].save(OUT.parent / "preview" / "icon_preview.png")
    print(f"Wrote {OUT}  ({len(sizes)} resolutions)")


if __name__ == "__main__":
    main()
