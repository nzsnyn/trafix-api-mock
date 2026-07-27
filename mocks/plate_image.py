"""Draw a fake Indonesian number plate as a JPEG.

The mock camera has to return an image URL that actually resolves to a picture,
otherwise the terminal side of the flow is never really exercised.
"""

from __future__ import annotations

import random
import re
from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# Common Indonesian region prefixes.
REGION_CODES = ["B", "D", "F", "L", "N", "T", "AA", "AB", "AD", "AG", "BK", "DK"]
SUFFIX_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"

PLATE_W, PLATE_H = 520, 260
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


def random_plate(rng: random.Random | None = None) -> str:
    """Generate a plausible plate, e.g. ``B1234XYZ``."""
    rng = rng or random
    region = rng.choice(REGION_CODES)
    number = rng.randint(1, 9999)
    suffix = "".join(rng.choice(SUFFIX_LETTERS) for _ in range(rng.randint(1, 3)))
    return f"{region}{number}{suffix}"


def format_plate(plate: str) -> str:
    """Insert the spaces a real plate shows: ``B 1234 XYZ``."""
    match = re.fullmatch(r"([A-Z]{1,2})(\d{1,4})([A-Z]{0,3})", plate.upper())
    if not match:
        return plate.upper()
    return " ".join(part for part in match.groups() if part)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, tuple[int, int, int, int]]:
    """Pick the largest font size whose rendered text still fits the plate.

    A long plate like ``AB 1234 XYZ`` overflows a fixed size, so the size is
    chosen per plate rather than hard-coded.
    """
    font = _load_font(88)
    box = draw.textbbox((0, 0), text, font=font)
    for size in range(88, 23, -4):
        font = _load_font(size)
        box = draw.textbbox((0, 0), text, font=font)
        if (box[2] - box[0]) <= max_w and (box[3] - box[1]) <= max_h:
            break
    return font, box


def render_plate_jpeg(
    plate: str,
    *,
    lane: str,
    device: str,
    captured_at: datetime,
    confidence: float,
) -> bytes:
    """Render a black plate with white text, plus a capture caption strip."""
    caption_h = 64
    image = Image.new("RGB", (PLATE_W, PLATE_H + caption_h), (24, 24, 26))
    draw = ImageDraw.Draw(image)

    # Plate body.
    margin = 14
    draw.rounded_rectangle(
        [margin, margin, PLATE_W - margin, PLATE_H - margin],
        radius=18,
        fill=(12, 12, 12),
        outline=(240, 240, 240),
        width=5,
    )

    text = format_plate(plate)
    font, box = _fit_font(draw, text, PLATE_W - 2 * (margin + 24), PLATE_H - 2 * (margin + 20))
    draw.text(
        (
            (PLATE_W - (box[2] - box[0])) / 2 - box[0],
            (PLATE_H - (box[3] - box[1])) / 2 - box[1],
        ),
        text,
        font=font,
        fill=(245, 245, 245),
    )

    # Caption strip: which camera, when, how sure.
    small = _load_font(20)
    draw.rectangle([0, PLATE_H, PLATE_W, PLATE_H + caption_h], fill=(38, 38, 42))
    draw.text(
        (16, PLATE_H + 10),
        f"{device}  lane={lane}  conf={confidence:.2f}",
        font=small,
        fill=(170, 200, 255),
    )
    draw.text(
        (16, PLATE_H + 34),
        captured_at.strftime("%Y-%m-%d %H:%M:%S"),
        font=small,
        fill=(150, 150, 160),
    )

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
