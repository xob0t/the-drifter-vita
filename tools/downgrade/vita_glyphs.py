"""PS Vita control glyphs, drawn in the style of the game's PlayStation glyphs.

The game's text and prompt glyphs (L1_0, R1_0, L2_0, R2_0, ...) are 18x18 sprites in three
colours: an outline/shadow, a body and a light symbol. On the PS Vita (see Compat Controls):
- L and R act as the triggers: VitaL/VitaR take L2/R2's place (columns 2-15, bottom row 12);
- touching the front screen's left or right half acts as the bumpers: VitaTouchL/VitaTouchR
  take L1/R1's place and footprint (columns 1-15 and 0-14, rows 6-12).
"""

from PIL import Image

SIZE = 18
# Measured from the game's L1_0/R1_0/L2_0/R2_0 textures.
PALETTE = {'a': (31, 36, 49, 255), 'b': (57, 89, 95, 255), 'c': (95, 205, 228, 255)}

# The game's letters, 3x5 as in its trigger glyphs (L2_0, R2_0).
LETTERS = {'L': ['c..', 'c..', 'c..', 'c..', 'ccc'], 'R': ['ccc', 'c.c', 'c.c', 'cc.', 'c.c']}

# The left shoulder button, rounded where the Vita's corner is (top left), square towards the
# middle: 14 columns wide at column 2, from row 5, its letter at column 6 within it. The right
# button is the mirror image.
SHOULDER = [
    '....abbbbbbbba',
    '..abbbbbbbbbbb',
    '.abbbbbbbbbbbb',
    '.bbbbbbbbbbbbb',
    'abbbbbbbbbbbbb',
    'bbbbbbbbbbbbbb',
    'abbbbbbbbbbbbb',
    'aaaaaaaaaaaaaa',
]
SHOULDER_TOP, LETTER_LEFT = 5, 6

# The left half touched: the console with rounded ends, a dark screen with a lit touch ring in
# that half. 15x7 at column 1, rows 6-12; the right one, at column 0, is the mirror image.
TOUCH = [
    '..abbbbbbbbba..',
    '.abccccaaaaaba.',
    'abbcbbcaaaaabba',
    'abbcbbcaaaaabba',
    '.abccccaaaaaba.',
    '..abbbbbbbbba..',
    '...aaaaaaaaa...',
]

# The Vita's Start button: a pill with START in the game's 3x5 letters, wider than the other
# glyphs (24x18). Its pivot is right of the middle so that, as an image, it ends where the
# game's PlayStation Start glyph (Start_0_0) ends; in texts it takes its own width.
START_PILL = [
    '..abbbbbbbbbbbbbbbbba..',
    '.abbbbbbbbbbbbbbbbbbba.',
    'abbbbbbbbbbbbbbbbbbbbba',
    'abbbbbbbbbbbbbbbbbbbbba',
    'abbbbbbbbbbbbbbbbbbbbba',
    'abbbbbbbbbbbbbbbbbbbbba',
    '.abbbbbbbbbbbbbbbbbbba.',
    '..aaaaaaaaaaaaaaaaaaa..',
]
START_LETTERS = {
    'S': ['ccc', 'c..', 'ccc', '..c', 'ccc'],
    'T': ['ccc', '.c.', '.c.', '.c.', '.c.'],
    'A': ['.c.', 'c.c', 'ccc', 'c.c', 'c.c'],
    'R': LETTERS['R'],
}
START_SIZE = (24, SIZE)
START_PIVOT = (18 / 24, 0.5)


def start_glyph():
    image = Image.new('RGBA', START_SIZE, (0, 0, 0, 0))
    place(image, START_PILL, 0, 5)
    for i, letter in enumerate('START'):
        place(image, START_LETTERS[letter], 2 + 4 * i, 6)
    return image


def mirror(rows):
    return [row[::-1] for row in rows]


def place(image, rows, left, top):
    for y, row in enumerate(rows):
        for x, key in enumerate(row):
            if key in PALETTE:
                image.putpixel((left + x, top + y), PALETTE[key])


def glyphs():
    """name -> 18x18 RGBA image."""
    out = {}
    for name, rows, letter, column in (
        ('VitaL', SHOULDER, 'L', LETTER_LEFT),
        ('VitaR', mirror(SHOULDER), 'R', 14 - LETTER_LEFT - 3),
    ):
        image = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
        place(image, rows, 2, SHOULDER_TOP)
        place(image, LETTERS[letter], 2 + column, SHOULDER_TOP + 1)
        out[name] = image
    for name, rows, column in (('VitaTouchL', TOUCH, 1), ('VitaTouchR', mirror(TOUCH), 0)):
        image = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
        place(image, rows, column, 6)
        out[name] = image
    return out
