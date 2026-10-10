"""Launch a Windows player, drive it with keys, and capture its window.

    uv run tools/downgrade/capture.py <exe> <out dir> [--script "4:shot title; 1:key RETURN; 3:shot next"]

The script is a ';'-separated list of "<delay seconds>:<action>" steps, where an
action is "shot <name>", "key <VK name>", "click <x> <y>" (client pixels), "offscreen"
(moves the window past the desktop's right edge) or "waitfile <name> <seconds>" (waits
for the player to write that file to the out dir). Every player runs windowed at the
same size so captures are comparable.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WIDTH, HEIGHT = 960, 544
VK = {
    'RETURN': 0x0D,
    'ESCAPE': 0x1B,
    'SPACE': 0x20,
    'LEFT': 0x25,
    'UP': 0x26,
    'RIGHT': 0x27,
    'DOWN': 0x28,
    'TAB': 0x09,
    **{chr(c): c for c in range(0x30, 0x5B)},
}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wt.DWORD),
        ('biWidth', wt.LONG),
        ('biHeight', wt.LONG),
        ('biPlanes', wt.WORD),
        ('biBitCount', wt.WORD),
        ('biCompression', wt.DWORD),
        ('biSizeImage', wt.DWORD),
        ('biXPelsPerMeter', wt.LONG),
        ('biYPelsPerMeter', wt.LONG),
        ('biClrUsed', wt.DWORD),
        ('biClrImportant', wt.DWORD),
    ]


def read_raw(path: Path):
    data = path.read_bytes()
    width, height = np.frombuffer(data[:8], '<i4')
    pixels = np.frombuffer(data[8 : 8 + width * height * 4], np.uint8).reshape(height, width, 4)
    return pixels[::-1, :, :3]


def mean_difference(reference: Image.Image, other: Image.Image):
    """Mean absolute difference per channel (0-255) between two frames, the other scaled to the
    reference's size if they differ."""
    a = np.asarray(reference.convert('RGB'), float)
    other = other.convert('RGB')
    if other.size != reference.size:
        other = other.resize(reference.size)
    return float(np.abs(a - np.asarray(other, float)).mean())


def offscreen(hwnd):
    """Past the right edge of the virtual desktop, so the user's cursor can never hover the game;
    PrintWindow and in-game captures still work."""
    right = user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78)  # SM_XVIRTUALSCREEN + SM_CXVIRTUALSCREEN
    user32.SetWindowPos(hwnd, 0, right + 200, 0, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE


def find_window(pid, timeout=60):
    # Window positions and sizes in physical pixels from here on, whatever the display scaling.
    user32.SetProcessDPIAware()
    found = []
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            # Unity also creates small helper windows; the game window has a real client area.
            _, _, width, height = client_rect(hwnd)
            if width >= 320 and height >= 180:
                found.append(hwnd)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(callback_type(callback), 0)
        if found:
            return found[0]
        time.sleep(0.5)
    raise RuntimeError(f'no window for process {pid}')


def client_rect(hwnd):
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right, rect.bottom


def capture(hwnd):
    """PrintWindow with PW_RENDERFULLCONTENT reads DirectX content even when covered."""
    _, _, width, height = client_rect(hwnd)
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    user32.PrintWindow(hwnd, memory_dc, 1 | 2)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(hwnd, window_dc)
    return Image.frombuffer('RGB', (width, height), buffer, 'raw', 'BGRX', 0, 1)


def press(hwnd, name):
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    code = VK[name.upper()]
    user32.keybd_event(code, 0, 0, 0)
    time.sleep(0.08)
    user32.keybd_event(code, 0, 2, 0)


def click(hwnd, x, y):
    left, top, _, _ = client_rect(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetCursorPos(left + x, top + y)
    time.sleep(0.05)
    user32.mouse_event(2, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(4, 0, 0, 0, 0)


def run(exe: Path, out: Path, script: str, extra_args=()):
    exe, out = exe.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(exe),
            '-screen-fullscreen',
            '0',
            '-screen-width',
            str(WIDTH),
            '-screen-height',
            str(HEIGHT),
            '-logFile',
            str(out / 'player.log'),
            *extra_args,
        ],
        cwd=exe.parent,
    )
    shots = []
    try:
        hwnd = find_window(process.pid)
        for step in filter(None, (s.strip() for s in script.split(';'))):
            delay, action = step.split(':', 1)
            time.sleep(float(delay))
            if process.poll() is not None:
                raise RuntimeError(f'player exited with {process.returncode} before "{action}"')
            verb, *args = action.split()
            if verb == 'shot':
                path = out / f'{args[0]}.png'
                capture(hwnd).save(path)
                shots.append(path)
            elif verb == 'key':
                press(hwnd, args[0])
            elif verb == 'click':
                click(hwnd, int(args[0]), int(args[1]))
            elif verb == 'offscreen':
                offscreen(hwnd)
            elif verb == 'waitfile':
                deadline = time.time() + float(args[1])
                while not (out / args[0]).exists():
                    if process.poll() is not None:
                        raise RuntimeError(f'player exited with {process.returncode} while waiting for {args[0]}')
                    if time.time() > deadline:
                        raise RuntimeError(f'timed out waiting for {args[0]}')
                    time.sleep(0.25)
            else:
                raise ValueError(f'unknown action {verb}')
    finally:
        process.kill()
    return shots


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('exe', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--script', default='8:shot start')
    args = parser.parse_args()
    for shot in run(args.exe, args.out, args.script):
        print(shot)


if __name__ == '__main__':
    main()
