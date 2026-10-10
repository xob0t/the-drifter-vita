"""PS Vita LiveArea and splash artwork, composed from The Drifter's Steam store art.

    uv run tools/downgrade/livearea.py [--force]

The images come from Steam's public CDN (no Steam client, no game launch). They are
downloaded once into work/livearea/store/ and read from there afterwards, so later runs
are offline. Writes to work/livearea/:

  icon0.png     128x128   bubble: Mick's face from the vertical key art
  bg0.png       840x500   LiveArea background: the sunset at the border fence (store screenshot)
  gate.png      280x158   LiveArea start gate: the wide key art with its logo
  pic0.png      960x544   shown while the app starts: the wide key art with its logo
  splash.png    960x544   Unity's PS Vita splash image: pic0 in full colour

The LiveArea images and pic0 are 8-bit indexed: the installer rejects a truecolor pic0
(promote error 0x8010113D), and icon0 must not have an alpha channel. The outputs are
remade only when the cached sources or this script change. pipeline.py puts them into
Vita builds (vita_app_files, settings_overlay). Previews of each image, the bubble and a
mock-up of the LiveArea screen go to artifacts/livearea/.
"""

import argparse
import hashlib
import shutil
import urllib.request
from pathlib import Path

import numpy as np
from common import ROOT, WORK, log
from PIL import Image, ImageDraw, ImageFont

OUT = WORK / 'livearea'
STORE = OUT / 'store'
PREVIEW = ROOT / 'artifacts' / 'livearea'
FILES = ['icon0.png', 'bg0.png', 'gate.png', 'pic0.png', 'splash.png']
PALETTE_COLORS = 256  # 8-bit indexed

STEAM_APP_ID = '1170570'
CDN = f'https://cdn.akamai.steamstatic.com/steam/apps/{STEAM_APP_ID}/'
SCREENSHOTS = f'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{STEAM_APP_ID}/'
# Cached name: (URL, SHA-256 of the file the compositions below were made for). Crops are
# in source pixels, so a changed file is reported; the cached copy is what the build uses.
# A mismatch is only reported, not refused as common.download refuses one: Valve can replace
# store art at the same URL, and a first build on a new machine should not stop for it.
SOURCES = {
    # Wide key art, logo top left, 616x353.
    'capsule.jpg': (CDN + 'capsule_616x353.jpg', 'ca01a3d5f46f921f14d2a2b1492e098c2797941d781599ef9caeadfa7763aab7'),
    # Vertical key art (library capsule), 600x900; the logo is in its lower quarter.
    'library.jpg': (CDN + 'library_600x900_2x.jpg', '8f903a5fb6f85911e216eed5f15af28a2785b8d352cd8f7bbcae6e36cad389cc'),
    # Store screenshot, 1920x1080 (6x the game's 320x180, with the CRT filter): Mick at the
    # sunset by the border fence. The only screenshot without subtitles or interface.
    'sunset.jpg': (
        SCREENSHOTS
        + '00b547d44208071a4f2fa5dfe0c50579642069ca/ss_00b547d44208071a4f2fa5dfe0c50579642069ca.1920x1080.jpg',
        '5aa72d8c52ffcc07fcaebc64741a8f13cc8349029bcaf0ff0436763ec18f52b8',
    ),
}

# The LiveArea screen (style a1, 960x544): the background sits below the 30-line status
# bar; the system draws the bubble at its top centre, the title at its top left, and the
# gate in its middle with "Start" over the gate's lower edge. Positions are measured from
# Sony's PS4 Link LiveArea manual screenshot; they only matter for the mock-up and for
# keeping the background's subjects out of the gate.
BG_ORIGIN = (60, 30)
GATE_IN_BG = (280, 151)


def fetch():
    """Returns the cached store images, downloading the missing ones."""
    STORE.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (url, digest) in SOURCES.items():
        path = STORE / name
        if not path.exists():
            log(f'Downloading {url}')
            request = urllib.request.Request(url, headers={'User-Agent': 'drifter-vita-livearea'})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
            except OSError as error:
                raise RuntimeError(
                    f'cannot download {url} ({error}); the LiveArea artwork needs it once, or a copy at {path}'
                ) from error
            part = path.with_suffix('.part')
            part.write_bytes(data)
            part.replace(path)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            log(
                f'warning: {path.name} differs from the store image the crops were made for '
                f'({actual[:12]} instead of {digest[:12]}); check artifacts/livearea/'
            )
        paths[name] = path
    return paths


def cover(image: Image.Image, size, box=None):
    """Scales the box (default: all) of the image to fill size, cropping the excess evenly."""
    if box is not None:
        image = image.crop(box)
    width, height = size
    scale = max(width / image.width, height / image.height)
    scaled = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    left, top = (scaled.width - width) // 2, (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def indexed(image: Image.Image):
    """8-bit indexed copy: a median-cut palette refined by k-means over the image's colours,
    mapped without dithering. The art is flat-shaded pixel art; with the refined palette its
    glows and skies do not band (mean error 2.6 levels on pic0), while Floyd-Steinberg
    speckled flat areas (skin, sky) with the JPEG noise. Deterministic for the same input."""
    rgb = image.convert('RGB')
    seed = rgb.quantize(PALETTE_COLORS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = np.array(seed.getpalette()[: PALETTE_COLORS * 3], dtype=np.float64).reshape(-1, 3)
    unique, counts = np.unique(np.asarray(rgb).reshape(-1, 3), axis=0, return_counts=True)
    unique = unique.astype(np.float64)
    for _ in range(12):
        nearest = np.empty(len(unique), dtype=np.int64)
        for start in range(0, len(unique), 8192):
            chunk = unique[start : start + 8192]
            nearest[start : start + 8192] = ((chunk[:, None, :] - palette[None]) ** 2).sum(axis=2).argmin(axis=1)
        weight = np.bincount(nearest, weights=counts, minlength=len(palette))
        for channel in range(3):
            total = np.bincount(nearest, weights=counts * unique[:, channel], minlength=len(palette))
            palette[:, channel] = np.where(weight > 0, total / np.maximum(weight, 1), palette[:, channel])
    reference = Image.new('P', (1, 1))
    reference.putpalette(np.clip(np.rint(palette), 0, 255).astype(np.uint8).flatten().tolist())
    return rgb.quantize(palette=reference, dither=Image.Dither.NONE)


def compose(sources):
    capsule = Image.open(sources['capsule.jpg']).convert('RGB')
    library = Image.open(sources['library.jpg']).convert('RGB')
    sunset = Image.open(sources['sunset.jpg']).convert('RGB')
    # The wide key art fills the boot screen (scaled 1.56x; it is the largest wide version).
    pic0 = cover(capsule, (960, 544))
    return {
        # Mick's face, lit red, centred for the round mask.
        'icon0.png': library.crop((240, 130, 520, 410)).resize((128, 128), Image.LANCZOS),
        # Cropped past the CRT filter's darkened corners; Mick kneels left of the gate, the fence
        # and the sun stand to its right, the sky stays clear for the bubble and the title.
        'bg0.png': cover(sunset, (840, 500), (130, 15, 1894, 1065)),
        'gate.png': cover(capsule, (280, 158)),
        'pic0.png': pic0,
        'splash.png': pic0,
    }


def stamp(sources):
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name in sorted(sources):
        digest.update(name.encode() + hashlib.sha256(sources[name].read_bytes()).digest())
    return digest.hexdigest()


def bubble(icon: Image.Image, diameter):
    """The icon at a diameter and the round mask the home screen shows it in."""
    scale = 4  # supersampled edge
    mask = Image.new('L', (diameter * scale,) * 2, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * scale - 1, diameter * scale - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.LANCZOS)
    method = Image.LANCZOS if diameter < icon.width else Image.NEAREST
    return icon.convert('RGB').resize((diameter, diameter), method), mask


def mockup(images):
    """Approximate LiveArea screen with the final (indexed) images."""
    screen = Image.new('RGB', (960, 544), (24, 24, 32))
    screen.paste(images['bg0.png'].convert('RGB'), BG_ORIGIN)
    draw = ImageDraw.Draw(screen)
    draw.rectangle((0, 0, 959, BG_ORIGIN[1] - 1), fill=(8, 8, 12))
    draw.text((900, 7), '12:00', fill=(235, 235, 235), font=ImageFont.load_default(15))
    icon, mask = bubble(images['icon0.png'], 76)
    screen.paste(icon, (480 - 38, BG_ORIGIN[1] + 8), mask)
    draw.ellipse((480 - 39, BG_ORIGIN[1] + 7, 480 + 38, BG_ORIGIN[1] + 84), outline=(240, 240, 240), width=2)
    for offset, colour in (((2, 2), (0, 0, 0)), ((0, 0), (250, 250, 250))):
        draw.text(
            (BG_ORIGIN[0] + 12 + offset[0], BG_ORIGIN[1] + 12 + offset[1]),
            'The Drifter',
            fill=colour,
            font=ImageFont.load_default(20),
        )
    gx, gy = BG_ORIGIN[0] + GATE_IN_BG[0], BG_ORIGIN[1] + GATE_IN_BG[1]
    screen.paste(images['gate.png'].convert('RGB'), (gx, gy))
    draw.rectangle((gx - 3, gy - 3, gx + 282, gy + 160), outline=(245, 245, 245), width=3)
    label = ImageFont.load_default(18)
    width = draw.textlength('Start', font=label)
    for offset, colour in (((1, 1), (0, 0, 0)), ((0, 0), (255, 255, 255))):
        draw.text((gx + 140 - width / 2 + offset[0], gy + 132 + offset[1]), 'Start', fill=colour, font=label)
    return screen


def previews(images):
    PREVIEW.mkdir(parents=True, exist_ok=True)
    for name in FILES[:4]:
        shutil.copyfile(OUT / name, PREVIEW / name)
    sheet = Image.new('RGB', (128 * 3 + 40, 296), (24, 24, 32))
    icon, mask = bubble(images['icon0.png'], 128)
    sheet.paste(icon, (10, 84), mask)
    big, mask = bubble(images['icon0.png'], 256)
    sheet.paste(big, (158, 20), mask)
    sheet.save(PREVIEW / 'bubble.png')
    mockup(images).save(PREVIEW / 'livearea-mockup.png')


def generate(force=False):
    """Makes the artwork if it is missing or out of date; returns its folder."""
    sources = fetch()
    key = stamp(sources)
    stamp_file = OUT / 'stamp.txt'
    current = stamp_file.exists() and stamp_file.read_text().strip() == key
    if not force and current and all((OUT / name).exists() for name in FILES):
        return OUT
    OUT.mkdir(parents=True, exist_ok=True)
    composed = compose(sources)
    final = {}
    for name, image in composed.items():
        final[name] = image if name == 'splash.png' else indexed(image)
        final[name].save(OUT / name, optimize=True)
        log(f'{name}: {final[name].size} {final[name].mode} {(OUT / name).stat().st_size} bytes')
    stamp_file.write_text(key + '\n')
    previews(final)
    log(f'Previews in {PREVIEW}')
    return OUT


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--force', action='store_true', help='compose again even if the images are up to date')
    generate(parser.parse_args().force)


if __name__ == '__main__':
    main()
