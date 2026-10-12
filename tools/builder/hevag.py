"""HEVAG, the PS Vita's ADPCM, which Unity's PS Vita importer makes of every AudioClip.

A channel is a run of 16-byte frames of 28 samples. Byte 0 holds the predictor's low nibble (high
bits) and the shift, byte 1 the predictor's high nibble and a flag, then come 28 four-bit codes,
low nibble first. A sample is (code << 12) >> shift plus a four-tap prediction from the last four
decoded samples, with one of 128 predictors. Unity writes flag 6 on a channel's first frame, 3 on
its last and 2 between.

The coefficient table and the decoder model are vgmstream's (src/coding/psv_decoder.c), which
decodes like the console: float history, no clamping inside. Unity's encoder (Sony's vagconv2) is
closed, so the encoder follows the patcher's (tools/patcher/hevag.go on the patcher branch): for
each frame, rank the predictors by the energy of their residual on the source, then quantise the
best 8 with the smallest shift that covers their residual and the next finer one, against the
history the decoder will really have, and keep the least error. (The patcher also tried the next
coarser shift; it never won.) Its SNR against the source is about 4 dB above Unity's.

Encoding is sequential within a channel (a frame starts from what the previous ones decode to),
so numpy works on many channels at once: channels are cut into segments that encode in lockstep.
A segment after a channel's first starts from the source's samples as its history instead of the
decoded ones. The two differ by the coding noise, and the difference dies out within a few frames
(every predictor's poles lie inside |z| < 0.99), so this costs nothing audible. Decoding cuts
channels the same way but starts each segment WARMUP frames early, which makes it exact.
"""

import numpy as np

FRAME = 28
FRAME_BYTES = 16
FIRST, MIDDLE, LAST = 6, 2, 3
SEGMENT = 1024  # frames a segment encodes in lockstep
LANES = 1024  # segments per batch
WARMUP = 64  # frames a decoded segment starts early; 0.99 ** (64 * 28) < 1e-7
CANDIDATES = 8

# vgmstream's hevag_coefs, in units of 1/8192: 128 predictors of four taps, four per line.
TABLE = """
     0      0      0      0     7680      0      0      0    14720  -6656      0      0    12544  -7040      0      0
 15616  -7680      0      0    14731  -7059      0      0    14507  -7366      0      0    13920  -7522      0      0
 13133  -7680      0      0    12028  -7680      0      0    10764  -7680      0      0     9359  -7680      0      0
  7832  -7680      0      0     6201  -7680      0      0     4488  -7680      0      0     2717  -7680      0      0
   910  -7680      0      0     -910  -7680      0      0    -2717  -7680      0      0    -4488  -7680      0      0
 -6201  -7680      0      0    -7832  -7680      0      0    -9359  -7680      0      0   -10764  -7680      0      0
-12028  -7680      0      0   -13133  -7680      0      0   -13920  -7522      0      0   -14507  -7366      0      0
-14731  -7059      0      0     5376  -9216   3328  -3072    -6400  -7168  -3328  -2304   -10496  -7424  -3584  -1024
  -167  -2722   -494   -541    -7430  -2221  -2298    424    -8001  -3166  -2814    289     6018  -4750   2649  -1298
  3798  -6946   3875  -1216    -8237  -2596  -2071    227     9199   1982  -1382  -2316    13021  -3044  -3792   1267
 13112  -4487  -2250   1665    -1668  -3744  -6456    840     7819  -4328   2111   -506     9571  -1336   -757    487
 10032  -2562    300    199    -4745  -4122  -5486  -1493    -5896   2378  -4787  -6947    -1193  -9117  -1237  -3114
  2783  -7108  -1575  -1447    -7334  -2062  -2212    446     6127  -2577   -315    -18     9457  -1858    102    258
  7876  -4483   2126   -538    -7172  -1795  -2069    482    -7358  -2102  -2233    440    -9170  -3509  -2674   -391
 -2638  -2647  -1929  -1637     1873   9183   1860  -5746     9214   1859  -1124  -2427    13204  -3012  -4139   1370
 12437  -4792   -256    622    -2653  -1144  -3182  -6878     9331  -1048   -828    507     1642   -620   -946  -4229
  4246  -7585   -533  -2259    -8988  -3891  -2807     44    -2562  -2735  -1730  -1899     3182   -483   -714  -1421
  7937  -3844   2821  -1019    10069  -2609    314    195     8400  -3297   1551   -155    -8529  -2775  -2432   -336
  9477  -1882    108    256       75  -2241   -298  -6937    -9143  -4160  -2963      5    -7270  -1958  -2156    460
 -2740   3745   5936  -1089     8993   1948   -683  -2704    13101  -2835  -3854   1055     9543  -1961    130    250
  5272  -4270   3124  -3157    -7696  -3383  -2907   -456     7309   2523    434  -2461    10275  -2867    391    172
 10940  -3721    665     97       24   -310  -1262    320    -8122  -2411  -2311   -271    -8511  -3067  -2337    163
   326  -3846    419   -933     8895   2194   -541  -2880    12073  -1876  -2017   -601     8729  -3423   1674   -169
 12950  -3847  -3007   1946    10038  -2570    302    198     9385  -2757   1008     41    -4720  -5006  -2852  -1161
  7869  -4326   2135   -501     2450  -8597   1299  -2780    10192  -2763    360    181    11313  -4213    833     53
 10154  -2716    345    185     9638  -1417   -737    482     3854  -4554   2843  -3397     6699  -5659   2249  -1074
 11082  -3908    728     80    -1026  -9810   -805  -3462    10396  -3746   1367    -96    10287    988  -1915  -1437
  7953   3878   -764  -3263    12689  -3375  -3354   2079     6641   3166    231  -2089    -2348  -7354  -1944  -4122
  9290  -4039   1885   -246     4633  -6403   1748  -1619    11247  -4125    802     61     9807  -2284    219    222
  9736  -1536   -706    473     8440  -3436   1562   -176     9307  -1021   -835    509     1698  -9025    688  -3037
 10214  -2791    368    179     8390   3248   -758  -2989     7201   3316     46  -2614      -88  -7809   -538  -4571
  6193  -5189   2760  -1245    12325  -1290  -3284    253    13064  -4075  -2824   1877     5333   2999    775  -1132
"""
COEFS = np.array(TABLE.split(), np.float64).reshape(128, 4) / 8192
COEFS32 = COEFS.astype(np.float32)
# The residual of predictor p on a frame is a'x with a = (1, -c); its energy is a'Ra for the frame's
# 5x5 autocorrelation R, so all 128 predictors rank from one product.
_A = np.concatenate([np.ones((128, 1)), -COEFS], 1)
UPPER = [(j, k) for j in range(5) for k in range(j, 5)]
PAIRS = np.array([(1 if j == k else 2) * _A[:, j] * _A[:, k] for j, k in UPPER], np.float32)
# The largest step of each shift 1..12, for the smallest shift that covers a residual peak.
PEAKS = np.array([(7 << 12) >> s for s in range(1, 13)], np.float32)
SHIFT_TRIES = np.array([0, 1])
STRETCH_BLOCK = 1 << 20  # samples stretch() interpolates at once, which bounds its temporaries


def frames_for(samples):
    return -(-samples // FRAME)


def stretch(pcm, frames):
    """Unity time-stretches a clip to whole frames rather than padding it. Catmull-Rom cubic
    interpolation, sample i taken at i * n / (frames * 28), matched the reference build best of the
    interpolators tried (linear, cubic B-spline family, Lanczos, FFT). pcm: (channels, n) floats in
    the 16-bit range; returns (channels, frames * 28) float32, rounded and clamped to 16 bits."""
    channels, n = pcm.shape
    out_len = frames * FRAME
    pcm = pcm.astype(np.float32, copy=False)
    if n == out_len:
        return np.clip(np.round(pcm), -32768, 32767)
    out = np.empty((channels, out_len), np.float32)
    for a in range(0, out_len, STRETCH_BLOCK):
        b = min(out_len, a + STRETCH_BLOCK)
        x = np.arange(a, b) * (n / out_len)
        i = np.floor(x).astype(np.int64)
        t = (x - i).astype(np.float32)
        # The four neighbours, the first and last sample repeated past the ends.
        y0, y1, y2, y3 = (pcm[:, np.clip(i + k, 0, n - 1)] for k in (-1, 0, 1, 2))
        y = y1 + 0.5 * t * (y2 - y0 + t * (2 * y0 - 5 * y1 + 4 * y2 - y3 + t * (3 * (y1 - y2) + y3 - y0)))
        np.clip(np.round(y), -32768, 32767, out=out[:, a:b])
    return out


def _segments(streams, warmup):
    """Cut channels (float arrays of whole frames) into segments of at most SEGMENT frames, each
    with `warmup` frames before it where there are some: [(stream, first frame, frames, warmup)]."""
    out = []
    for s, x in enumerate(streams):
        frames = len(x) // FRAME
        for start in range(0, frames, SEGMENT):
            w = min(warmup, start)
            out.append((s, start, min(SEGMENT, frames - start), w))
    out.sort(key=lambda seg: -(seg[2] + seg[3]))
    return out


def encode(clips):
    """HEVAG data of each clip: clips are (channels, frames * 28) arrays of 16-bit samples (see
    stretch()); the result is bytes, frame by frame, the channels of each frame side by side."""
    streams = [ch for c in clips for ch in c]
    codes = [np.zeros((len(x) // FRAME, FRAME_BYTES), np.uint8) for x in streams]
    segments = _segments(streams, 0)
    for at in range(0, len(segments), LANES):
        batch = segments[at : at + LANES]
        steps = batch[0][2]
        x = np.zeros((len(batch), steps * FRAME), np.float32)
        history = np.zeros((len(batch), 4), np.float32)
        for lane, (s, start, frames, _) in enumerate(batch):
            x[lane, : frames * FRAME] = streams[s][start * FRAME : (start + frames) * FRAME]
            if start:
                history[lane] = streams[s][start * FRAME - 4 : start * FRAME][::-1]
        out = _encode_lanes(x.reshape(len(batch), steps, FRAME), history, CANDIDATES)
        for lane, (s, start, frames, _) in enumerate(batch):
            codes[s][start : start + frames] = out[lane, :frames]
    result, k = [], 0
    for c in clips:
        channels = [codes[k + i] for i in range(len(c))]
        k += len(c)
        for frames in channels:
            frames[:, 1] |= MIDDLE
            frames[0, 1] = frames[0, 1] & 0xF0 | FIRST
            frames[-1, 1] = frames[-1, 1] & 0xF0 | LAST
        result.append(np.stack(channels, 1).tobytes())
    return result


PLAN_BLOCK = 8  # frames ranked at once; small enough that the temporaries stay in cache


def _plan(x, history, candidates):
    """For every frame, the predictors worth a trial and the smallest shift that covers their
    residual. Both come from the source: ranking against the decoded history, as the patcher did,
    needs the frames in order, and the two differ only by the coding noise. Returns predictor
    indices and base shifts, (lanes, steps, candidates) each."""
    lanes, steps, _ = x.shape
    src = np.concatenate([history[:, ::-1], x.reshape(lanes, -1)], 1)
    best = np.empty((lanes, steps, candidates), np.int32)
    base = np.empty((lanes, steps, candidates), np.int64)
    upper = tuple(np.array(UPPER).T)
    index = np.arange(128, dtype=np.int32)
    shape = (lanes, PLAN_BLOCK, candidates, FRAME)
    residual, product = np.empty(shape, np.float32), np.empty(shape, np.float32)
    for t0 in range(0, steps, PLAN_BLOCK):
        t1 = min(steps, t0 + PLAN_BLOCK)
        frames = t1 - t0
        # lags[:, :, j]: x[i - j] for the block's samples, as (lanes, frames, 5, 28)
        lags = np.stack(
            [src[:, t0 * FRAME + 4 - j : t1 * FRAME + 4 - j].reshape(lanes, frames, FRAME) for j in range(5)], 2
        )
        corr = (lags @ lags.transpose(0, 1, 3, 2))[:, :, upper[0], upper[1]]
        cost = (corr.reshape(-1, len(UPPER)) @ PAIRS).reshape(lanes, frames, 128)
        # The cheapest predictors: costs are not negative, so their float bits sort as integers;
        # the low 7 bits carry the index instead, which partitions faster than argpartition.
        key = cost.view(np.int32) & ~127 | index
        chosen = np.partition(key, candidates - 1, axis=2)[..., :candidates] & 127
        coef = COEFS32[chosen][..., None]  # (lanes, frames, candidates, 4, 1)
        r, tmp = residual[:, :frames], product[:, :frames]
        np.multiply(coef[..., 0, :], lags[:, :, None, 1], out=r)
        np.subtract(lags[:, :, None, 0], r, out=r)
        for j in range(1, 4):
            np.multiply(coef[..., j, :], lags[:, :, None, j + 1], out=tmp)
            r -= tmp
        np.abs(r, out=r)
        best[:, t0:t1] = chosen
        base[:, t0:t1] = len(PEAKS) - np.searchsorted(PEAKS[::-1], r.max(3), side='left')
    return best, base


def _encode_lanes(x, history, candidates):
    """Encode lanes of frames in lockstep: x (lanes, steps, 28) float32, history (lanes, 4), newest
    first. Returns (lanes, steps, 16) uint8 frames with flag 0."""
    lanes, steps, _ = x.shape
    out = np.zeros((lanes, steps, FRAME_BYTES), np.uint8)
    h = history.astype(np.float32)
    best, base = _plan(x, h, candidates)
    rows = np.arange(lanes)
    tries = len(SHIFT_TRIES)
    trials = candidates * tries
    q = np.empty((FRAME, lanes, trials), np.float32)
    p = np.empty((lanes, trials), np.float32)
    tmp = np.empty((lanes, trials), np.float32)
    for t in range(steps):
        frame = x[:, t]
        shift = (base[:, t, :, None] + SHIFT_TRIES).reshape(lanes, trials)
        err = np.where(shift <= 12, 0, np.inf).astype(np.float32)
        shift = np.clip(shift, 0, 12)
        step = np.ldexp(np.float32(1), 12 - shift).astype(np.float32)
        inverse = (1 / step).astype(np.float32)
        c = np.repeat(COEFS32[best[:, t]], tries, axis=1)
        c0, c1, c2, c3 = (np.ascontiguousarray(c[..., j]) for j in range(4))
        h0, h1, h2, h3 = (np.repeat(h[:, j : j + 1], trials, 1) for j in range(4))
        for i in range(FRAME):
            np.multiply(h0, c0, out=p)
            p += np.multiply(h1, c1, out=tmp)
            p += np.multiply(h2, c2, out=tmp)
            p += np.multiply(h3, c3, out=tmp)
            xi = frame[:, i : i + 1]
            qi = q[i]
            np.subtract(xi, p, out=qi)
            qi *= inverse
            np.rint(qi, out=qi)
            np.clip(qi, -8, 7, out=qi)
            s = qi * step
            s += p
            np.subtract(xi, s, out=tmp)
            tmp *= tmp
            err += tmp
            h3, h2, h1, h0 = h2, h1, h0, s
        k = err.argmin(1)
        chosen = best[rows, t, k // tries]
        h = np.stack([h0[rows, k], h1[rows, k], h2[rows, k], h3[rows, k]], 1)
        nibbles = q[:, rows, k].T.astype(np.int8).view(np.uint8) & 0x0F  # (lanes, 28)
        out[:, t, 0] = (chosen & 0x0F) << 4 | shift[rows, k]
        out[:, t, 1] = chosen & 0xF0
        out[:, t, 2:] = nibbles[:, 0::2] | nibbles[:, 1::2] << 4
    return out


def decode(clips):
    """Decode clips as the console does: clips are (data, channels); returns (channels, frames * 28)
    int16 arrays."""
    streams, shapes = [], []
    for data, channels in clips:
        frames = np.frombuffer(data, np.uint8)
        frames = frames[: len(frames) // (FRAME_BYTES * channels) * FRAME_BYTES * channels]
        frames = frames.reshape(-1, channels, FRAME_BYTES)
        shapes.append((len(streams), channels))
        streams += [_excitation(frames[:, c]) for c in range(channels)]
    outputs = [np.zeros(len(e[0]) * FRAME, np.float32) for e in streams]
    segments = _segments([np.empty(len(e[0]) * FRAME) for e in streams], WARMUP)
    batch_lanes = LANES * 8
    for at in range(0, len(segments), batch_lanes):
        batch = segments[at : at + batch_lanes]
        steps = batch[0][2] + batch[0][3]
        e = np.zeros((steps * FRAME, len(batch)), np.float32)
        c = np.zeros((steps, 4, len(batch)), np.float32)
        for lane, (s, start, frames, warm) in enumerate(batch):
            excitation, coef = streams[s]
            first, end = start - warm, start + frames
            e[: (end - first) * FRAME, lane] = excitation[first:end].ravel()
            c[: end - first, :, lane] = coef[first:end]
        y = _decode_lanes(e, c)
        for lane, (s, start, frames, warm) in enumerate(batch):
            outputs[s][start * FRAME : (start + frames) * FRAME] = y[warm * FRAME : (warm + frames) * FRAME, lane]
    return [np.clip(np.round(np.stack(outputs[k : k + n])), -32768, 32767).astype(np.int16) for k, n in shapes]


def _excitation(frames):
    """(code << 12 >> shift per sample (frames, 28), coefficients (frames, 4)) of one channel."""
    predictor = (frames[:, 0] >> 4) | (frames[:, 1] & 0xF0)
    predictor = np.where(predictor > 127, 0, predictor)
    shift = (frames[:, 0] & 0x0F).astype(np.int32)
    flag = frames[:, 1] & 0x0F
    nibbles = np.empty((len(frames), FRAME), np.int32)
    nibbles[:, 0::2] = frames[:, 2:] & 0x0F
    nibbles[:, 1::2] = frames[:, 2:] >> 4
    nibbles = np.where(nibbles >= 8, nibbles - 16, nibbles)
    codes = ((nibbles << 12) >> shift[:, None]).astype(np.float32)
    coef = COEFS32[predictor]
    codes[flag >= 7] = 0  # the decoder outputs silence for these flags
    coef[flag >= 7] = 0
    return codes, coef


def _decode_lanes(e, c):
    """e (samples, lanes) excitation, c (frames, 4, lanes) coefficients; returns the decoded samples."""
    samples, lanes = e.shape
    y = np.empty_like(e)
    h0, h1, h2, h3 = (np.zeros(lanes, np.float32) for _ in range(4))
    for n in range(samples):
        c0, c1, c2, c3 = c[n // FRAME]
        s = e[n] + h0 * c0
        s += h1 * c1
        s += h2 * c2
        s += h3 * c3
        y[n] = s
        h3, h2, h1, h0 = h2, h1, h0, s
    return y


def snr(reference, test):
    """Signal-to-noise ratio in dB of test against reference."""
    reference = np.asarray(reference, np.float64)
    noise = np.sum((reference - np.asarray(test, np.float64)) ** 2)
    signal = np.sum(reference**2)
    return np.inf if noise == 0 else 10 * np.log10(max(signal, 1e-9) / noise)
