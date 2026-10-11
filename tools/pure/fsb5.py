"""FMOD FSB5 banks as Unity's PS Vita build stores AudioClips: one HEVAG sample per bank.

Every bank in the reference build has the same shape:

    0x00  'FSB5', version 1, 1 sample, sample header size, name table size 0, data size, codec 9
    0x1C  8 zero bytes
    0x24  the first 8 bytes of MD5(clip name), then 16 bytes that depend on the audio only (clips
          with the same audio share them) but match no hash of the source, the encoded data or
          the bank tried (MD5, SHA, xxHash, SpookyHash, MurmurHash3, CRC32); likely a hash of the
          stretched PCM inside FMOD's bank builder. They are written here as MD5 of the data.
    0x3C  the sample header: a 64-bit word (chunk flag, rate code, stereo bit, data offset 0,
          sample count) and, for a rate outside FMOD's table, a frequency chunk; zeros to 0x60
    0x60  the HEVAG frames, then zeros to a multiple of 32 bytes

FMOD reads banks without checking the hash.
"""

import hashlib
import struct

CODEC_HEVAG = 9
HEADER = 0x3C
ALIGN = 32
RATES = [0, 8000, 11000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 96000]
CHUNK_FREQUENCY = 2
CHUNK_CHANNELS = 1


def align(n):
    return -(-n // ALIGN) * ALIGN


def bank(name, rate, channels, samples, data, codec=CODEC_HEVAG):
    """One-sample FSB5 bank of encoded data."""
    chunks = []
    if rate not in RATES:
        chunks.append((CHUNK_FREQUENCY, struct.pack('<I', rate)))
    if channels > 2:
        chunks.append((CHUNK_CHANNELS, bytes([channels])))
    word = (RATES.index(rate) if rate in RATES else 0) << 1 | (channels == 2) << 5 | samples << 34
    sample = bytearray(struct.pack('<Q', word | bool(chunks)))
    for i, (kind, body) in enumerate(chunks):
        more = i + 1 < len(chunks)
        sample += struct.pack('<I', more | len(body) << 1 | kind << 25) + body
    sample_headers = align(HEADER + len(sample)) - HEADER
    data_size = align(len(data))
    head = struct.pack('<4s6I', b'FSB5', 1, 1, sample_headers, 0, data_size, codec)
    content = hashlib.md5(data).digest()
    head += bytes(8) + hashlib.md5(name.encode('utf-8')).digest()[:8] + content
    return head + bytes(sample).ljust(sample_headers, b'\0') + data.ljust(data_size, b'\0')


def parse(b):
    """The fields of a one-sample FSB5 bank."""
    magic, version, count, sample_headers, names, data_size, codec = struct.unpack_from('<4s6I', b)
    if magic != b'FSB5' or version != 1 or count != 1:
        raise ValueError('not a one-sample FSB5 bank')
    word = struct.unpack_from('<Q', b, HEADER)[0]
    info = {
        'codec': codec,
        'sample_headers': sample_headers,
        'names': names,
        'data_size': data_size,
        'name_hash': b[0x24:0x2C],
        'content_hash': b[0x2C:0x3C],
        'word': word,
        'rate': RATES[(word >> 1) & 0xF],
        'channels': (1, 2, 6, 8)[(word >> 5) & 3],
        'data_offset': (word >> 7) & 0x7FFFFFF,
        'samples': word >> 34,
        'chunks': [],
    }
    at, more = HEADER + 8, word & 1
    while more:
        chunk = struct.unpack_from('<I', b, at)[0]
        more, size, kind = chunk & 1, (chunk >> 1) & 0xFFFFFF, chunk >> 25
        body = b[at + 4 : at + 4 + size]
        info['chunks'].append((kind, body))
        if kind == CHUNK_FREQUENCY:
            info['rate'] = struct.unpack('<I', body)[0]
        elif kind == CHUNK_CHANNELS:
            info['channels'] = body[0]
        at += 4 + size
    start = HEADER + sample_headers + names
    info['header'] = b[:start]
    info['data'] = b[start : start + data_size]
    return info
