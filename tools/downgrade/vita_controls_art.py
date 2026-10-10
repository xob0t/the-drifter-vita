"""The PS Vita picture for the game's controls screen (GuiControls), drawn in the style of the
game's controller pictures (GuiControlsAtlas: Ps_0, Xbox_0, ...).

Those pictures are 207x73 sprites: the screen's dark panel, the controller in four colours,
and the cyan callout lines that end at the screen's labels. Their ends stay where the
labels are; on the Vita they point at:
- "TOGGLE ITEMS" (x 98 and 110 from row 12): touch points in the front screen's halves;
- L "EQUIP/CRAFT" (from 69,25) and R "SELECT/INTERACT" (from 138,25): the shoulder buttons;
- "WALK" (from 71,53): the left stick; "LOOK AROUND" (from 138,48): the right stick.
Coordinates are image pixels, origin top left. PIVOT puts the picture where Ps_0 sits.

    uv run tools/downgrade/vita_controls_art.py <out png>
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 207, 73
# Ps_0's pivot, in this image: its point (104, 31 from the bottom).
PIVOT = (104 / WIDTH, 31 / HEIGHT)
# Measured from the game's controller pictures.
PALETTE = {
    'panel': (45, 64, 73, 255),
    'line': (95, 205, 228, 255),
    'dark': (8, 21, 22, 255),
    'light': (87, 138, 135, 255),
    'body': (57, 89, 95, 255),
}
CENTER = 104  # the picture's mirror axis

STICK = [
    '..ddd..',
    '.dllld.',
    'dllllld',
    'dllblld',
    'dllllld',
    '.dllld.',
    '..ddd..',
]
TOUCH = [
    '.ccc.',
    'c...c',
    'c...c',
    'c...c',
    '.ccc.',
]
KEYS = {'d': 'dark', 'l': 'light', 'b': 'body', 'c': 'line'}


def stamp(image, rows, left, top, mirror=False):
    for y, row in enumerate(rows):
        for x, key in enumerate(row[::-1] if mirror else row):
            if key in KEYS:
                image.putpixel((left + x, top + y), PALETTE[KEYS[key]])


def picture():
    image = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # The panel behind it, as in the game's pictures.
    draw.rectangle([3, 2, 203, 70], fill=PALETTE['panel'])
    draw.rectangle([2, 3, 204, 69], fill=PALETTE['panel'])

    # Shoulder buttons: raised strips over the top edge, short of the corners.
    for left, right in ((82, 93), (2 * CENTER - 93, 2 * CENTER - 82)):
        draw.rectangle([left + 1, 29, right - 1, 29], fill=PALETTE['dark'])
        draw.rectangle([left + 1, 30, right - 1, 30], fill=PALETTE['light'])
        image.putpixel((left, 30), PALETTE['dark'])
        image.putpixel((right, 30), PALETTE['dark'])
    # The body: a wide rounded slab.
    draw.rounded_rectangle([72, 31, 2 * CENTER - 72, 58], radius=12, fill=PALETTE['body'], outline=PALETTE['dark'])
    # Lit edge along the top, between the shoulder buttons.
    draw.line([(94, 32), (2 * CENTER - 94, 32)], fill=PALETTE['light'])
    # The screen.
    draw.rectangle([89, 35, 2 * CENTER - 89, 53], fill=PALETTE['dark'])
    # Speaker dots and the front camera above it.
    for x in (79, 81, 2 * CENTER - 81, 2 * CENTER - 79):
        image.putpixel((x, 35), PALETTE['dark'])
    image.putpixel((2 * CENTER - 86, 33), PALETTE['dark'])
    # D-pad (left) and face buttons (right).
    draw.rectangle([79, 38, 81, 44], fill=PALETTE['light'])
    draw.rectangle([77, 40, 83, 42], fill=PALETTE['light'])
    image.putpixel((80, 41), PALETTE['body'])
    face = 2 * CENTER - 80
    for dx, dy in ((0, -3), (0, 3), (-3, 0), (3, 0)):
        draw.rectangle([face + dx - 1, 41 + dy - 1, face + dx, 41 + dy], fill=PALETTE['light'])
    # Sticks, inside the lower corners, and the PS, Select and Start buttons below the screen's
    # edge.
    stamp(image, STICK, 78, 50)
    stamp(image, STICK, 2 * CENTER - 84, 50, mirror=True)
    draw.line([(88, 56), (89, 56)], fill=PALETTE['light'])
    draw.line([(2 * CENTER - 92, 56), (2 * CENTER - 91, 56)], fill=PALETTE['light'])
    draw.line([(2 * CENTER - 89, 56), (2 * CENTER - 88, 56)], fill=PALETTE['light'])

    # Callouts. TOGGLE ITEMS: down into the screen's halves, ending in touch rings.
    for x in (98, 110):
        draw.line([(x, 12), (x, 42)], fill=PALETTE['line'])
        stamp(image, TOUCH, x - 2, 43)
    # EQUIP/CRAFT and SELECT/INTERACT: to the shoulder buttons.
    draw.line([(69, 25), (73, 29)], fill=PALETTE['line'])
    draw.line([(74, 30), (81, 30)], fill=PALETTE['line'])
    draw.line([(138, 25), (134, 29)], fill=PALETTE['line'])
    draw.line([(2 * CENTER - 81, 30), (133, 30)], fill=PALETTE['line'])
    # WALK and LOOK AROUND: to the sticks.
    draw.line([(71, 53), (77, 53)], fill=PALETTE['line'])
    draw.line([(2 * CENTER - 77, 53), (133, 53)], fill=PALETTE['line'])
    draw.line([(134, 52), (138, 48)], fill=PALETTE['line'])
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    picture().save(args.out)


if __name__ == '__main__':
    main()
