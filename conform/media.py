"""Render file facts (frames, rate, size, start timecode) without third-party tools.

QuickTime/MP4 is read directly; other containers fall back to ffprobe when it
is installed. Resolve's own Python has only the system PATH, so common
Homebrew locations are searched too.
"""
from dataclasses import dataclass
from fractions import Fraction
import json
import os
import shutil
import struct
import subprocess

CONTAINERS = (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'udta')


@dataclass(frozen=True)
class MediaInfo:
    path: str
    frames: int
    rate: Fraction          # frames per second
    width: int
    height: int
    tc_start: int = None    # start timecode as a frame count at round(rate)
    drop_frame: bool = False

    @property
    def timebase(self):
        return round(self.rate)


class MediaError(ValueError):
    pass


def _atoms(data, start=0, end=None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack('>I4s', data[i:i + 8])
        header = 8
        if size == 1:
            size = struct.unpack('>Q', data[i + 8:i + 16])[0]
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            raise MediaError('Corrupt QuickTime atom')
        yield kind, i + header, i + size
        i += size


def _read_moov(path):
    with open(path, 'rb') as f:
        f.seek(0, 2)
        length = f.tell()
        offset = 0
        while offset + 8 <= length:
            f.seek(offset)
            head = f.read(16)
            size, kind = struct.unpack('>I4s', head[:8])
            header = 8
            if size == 1:
                size, header = struct.unpack('>Q', head[8:16])[0], 16
            elif size == 0:
                size = length - offset
            if size < header:
                raise MediaError('Corrupt QuickTime file')
            if kind == b'moov':
                f.seek(offset)
                return f.read(size)
            offset += size
    raise MediaError('No QuickTime movie header')


def _find(data, start, end, *path):
    for kind, a, b in _atoms(data, start, end):
        if kind == path[0]:
            return (a, b) if len(path) == 1 else _find(data, a, b, *path[1:])
    return None


def _full_box(data, a):
    return data[a], a + 4  # version, payload start


def _chunk_offsets(data, stbl):
    box = _find(data, *stbl, b'stco')
    if box:
        a = box[0] + 4
        count = struct.unpack('>I', data[a:a + 4])[0]
        return list(struct.unpack('>%dI' % count, data[a + 4:a + 4 + 4 * count]))
    box = _find(data, *stbl, b'co64')
    if box:
        a = box[0] + 4
        count = struct.unpack('>I', data[a:a + 4])[0]
        return list(struct.unpack('>%dQ' % count, data[a + 4:a + 4 + 8 * count]))
    return []


def _quicktime(path):
    moov = _read_moov(path)
    _, body, end = next(_atoms(moov))
    video = timecode = None
    for kind, a, b in _atoms(moov, body, end):
        if kind != b'trak':
            continue
        hdlr = _find(moov, a, b, b'mdia', b'hdlr')
        handler = moov[hdlr[0] + 8:hdlr[0] + 12] if hdlr else b''
        if handler == b'vide' and video is None:
            video = (a, b)
        elif handler == b'tmcd' and timecode is None:
            timecode = (a, b)
    if video is None:
        raise MediaError('No video track')
    mdhd = _find(moov, *video, b'mdia', b'mdhd')
    version, p = _full_box(moov, mdhd[0])
    timescale = struct.unpack('>I', moov[p + (16 if version else 8):p + (20 if version else 12)])[0]
    stbl = _find(moov, *video, b'mdia', b'minf', b'stbl')
    stts = _find(moov, *stbl, b'stts')
    count = struct.unpack('>I', moov[stts[0] + 4:stts[0] + 8])[0]
    entries = struct.unpack('>%dI' % (2 * count), moov[stts[0] + 8:stts[0] + 8 + 8 * count])
    frames = sum(entries[0::2])
    if not frames or not entries[1]:
        raise MediaError('Empty video track')
    rate = Fraction(timescale, entries[1])
    stsd = _find(moov, *stbl, b'stsd')
    entry = stsd[0] + 8  # full box header + entry count
    width, height = struct.unpack('>HH', moov[entry + 32:entry + 36])
    tc_start, drop = None, False
    if timecode:
        tstbl = _find(moov, *timecode, b'mdia', b'minf', b'stbl')
        tstsd = _find(moov, *tstbl, b'stsd')
        e = tstsd[0] + 8
        if moov[e + 4:e + 8] == b'tmcd':
            flags = struct.unpack('>I', moov[e + 20:e + 24])[0]
            drop = bool(flags & 1)
            offsets = _chunk_offsets(moov, tstbl)
            if offsets:
                with open(path, 'rb') as f:
                    f.seek(offsets[0])
                    tc_start = struct.unpack('>I', f.read(4))[0]
    return MediaInfo(path, frames, rate, width, height, tc_start, drop)


def _ffprobe_path():
    found = shutil.which('ffprobe')
    if found:
        return found
    for candidate in ('/opt/homebrew/bin/ffprobe', '/usr/local/bin/ffprobe'):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def tc_to_frames(text, timebase, drop=False):
    hh, mm, ss, ff = (int(x) for x in text.replace(';', ':').split(':'))
    frames = ((hh * 60 + mm) * 60 + ss) * timebase + ff
    if drop:
        dropped = 2 * (timebase // 30)
        minutes = hh * 60 + mm
        frames -= dropped * (minutes - minutes // 10)
    return frames


def frames_to_tc(frames, timebase, drop=False):
    if drop:
        dropped = 2 * (timebase // 30)
        per_10 = timebase * 600 - dropped * 9
        per_min = timebase * 60 - dropped
        d, m = divmod(frames, per_10)
        frames += dropped * 9 * d + (dropped * ((m - dropped) // per_min) if m > dropped else 0)
    ff = frames % timebase
    total = frames // timebase
    return '%02d:%02d:%02d%s%02d' % (total // 3600 % 24, total // 60 % 60, total % 60, ';' if drop else ':', ff)


def _ffprobe(path):
    tool = _ffprobe_path()
    if not tool:
        raise MediaError('Unsupported container and ffprobe is not installed')
    r = subprocess.run([tool, '-v', 'error', '-select_streams', 'v:0', '-count_packets',
                        '-show_entries', 'stream=width,height,r_frame_rate,nb_frames,nb_read_packets:stream_tags=timecode:format_tags=timecode',
                        '-of', 'json', path], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise MediaError('ffprobe failed: ' + r.stderr.strip()[:200])
    info = json.loads(r.stdout)
    stream = info['streams'][0]
    rate = Fraction(stream['r_frame_rate'])
    frames = int(stream.get('nb_frames') or stream.get('nb_read_packets') or 0)
    tc = stream.get('tags', {}).get('timecode') or info.get('format', {}).get('tags', {}).get('timecode')
    drop = bool(tc and ';' in tc)
    return MediaInfo(path, frames, rate, int(stream['width']), int(stream['height']),
                     tc_to_frames(tc, round(rate), drop) if tc else None, drop)


def probe(path):
    path = str(path)
    if path.lower().endswith(('.mov', '.mp4', '.m4v', '.qt')):
        try:
            return _quicktime(path)
        except (MediaError, struct.error, IndexError, TypeError):
            pass
    return _ffprobe(path)
