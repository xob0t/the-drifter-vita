# The simplex noise kernel in _noise is a port of snoise from webgl-noise
# (https://github.com/ashima/webgl-noise, https://github.com/stegu/webgl-noise) by Ian McEwan
# (Ashima Arts) and Stefan Gustavson, used under its MIT license (see also THIRD_PARTY_NOTICES):
#
# Copyright (C) 2011 by Ashima Arts (Simplex noise)
# Copyright (C) 2011-2016 by Stefan Gustavson (Classic noise and others)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""The precomputed noise texture of the fast noise path (shader_rules.py noise_fast, Compat FastNoise).

The game's noise shaders (WaterSprite, PropBurble, PropLightMasked) evaluate 2D simplex noise
(Ashima Arts / McEwan's snoise, times 130) twice per pixel. The fast path reads the same kind of
noise from a texture instead: SIZE x SIZE 8-bit texels, periodic over PERIOD lattice cells along
both of the simplex lattice's skewed axes, so the texture wraps (Repeat) without a seam. A noise
position p is read at texture coordinate skew(p) / PERIOD, skew(p) = p + (p.x + p.y) * F2, which
the vertex programs compute (it is affine in the texture coordinate), and bilinear filtering
between texels (SIZE / PERIOD = 16 a cell) stands in for the kernel between them.

The noise is the game's function in all but the hash: the same simplex kernel, the same set of
41 gradients and normalisation, with each lattice point's gradient drawn at random (a fixed seed)
instead of from the permutation polynomial, which has no period that a texture could wrap at.
Over 400,000 positions the two agree in mean (0.001 and 0.001), standard deviation (0.472 and
0.475), quantiles (within 0.01) and gradient size (rms 2.29 and 2.26 a unit); the texture lookup
is within 0.0064 rms (0.025 at most) of the exact periodic noise. A period of 32 cells repeats
every 26 units along the skewed axes, more than any affected sprite spans (Drowning's surface
and Crypt's lights about 2-5, EscReactorWater's and LabReactor's water at most 10).

Texel value v (0-255) holds noise (v - 128) / 127, so 0 is exact."""

import numpy as np

SIZE = 512
PERIOD = 32
SEED = 1
RESOURCE_NAME = 'DrifterNoise'  # Resources/DrifterNoise.bytes, which Compat FastNoise loads
F2 = 0.3660254037844386  # (sqrt(3) - 1) / 2
G2 = 0.21132486540518713  # (3 - sqrt(3)) / 6


def _noise(x, y, table):
    """The game's simplex kernel at (x, y), gradients from table (PERIOD x PERIOD hash values
    k / 41, as frac(permute(...) / 41) gives in the shader)."""
    s = (x + y) * F2
    i, j = np.floor(x + s), np.floor(y + s)
    t = (i + j) * G2
    x0, y0 = x - i + t, y - j + t
    i1 = (x0 > y0).astype(np.float64)
    j1 = 1.0 - i1
    corners = (
        (0.0, 0.0, x0, y0),
        (i1, j1, x0 - i1 + G2, y0 - j1 + G2),
        (1.0, 1.0, x0 - 1.0 + 2 * G2, y0 - 1.0 + 2 * G2),
    )
    total = np.zeros_like(x)
    period = table.shape[0]
    for di, dj, dx, dy in corners:
        frac = table[np.mod(i + di, period).astype(np.int64), np.mod(j + dj, period).astype(np.int64)]
        gx = 2.0 * frac - 1.0
        h = np.abs(gx) - 0.5
        a0 = gx - np.floor(gx + 0.5)
        m = np.maximum(0.5 - (dx * dx + dy * dy), 0.0)
        m = m**4 * (1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h))
        total += m * (a0 * dx + h * dy)
    return 130.0 * total


def texels():
    """The texture's bytes, rows bottom to top (Texture2D.LoadRawTextureData order): texel (a, b)
    holds the noise at skewed lattice coordinates ((a, b) + 0.5) * PERIOD / SIZE."""
    table = np.random.default_rng(SEED).integers(0, 41, size=(PERIOD, PERIOD)).astype(np.float64) / 41.0
    c = (np.arange(SIZE) + 0.5) * PERIOD / SIZE
    si, sj = np.meshgrid(c, c)  # si along a row (a), sj along a column (b)
    t = (si + sj) * G2
    values = _noise(si - t, sj - t, table)
    return np.clip(np.round(values * 127.0 + 128.0), 0, 255).astype(np.uint8).tobytes()
