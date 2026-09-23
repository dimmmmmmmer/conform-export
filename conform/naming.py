"""Pure naming plan. Future render-job adapters must consume this same plan."""
from dataclasses import dataclass, asdict
from string import Formatter
import re
import unicodedata

DEFAULT_TEMPLATE = '{TRACK}-{INDEX:4}_{SOURCE}'  # {INDEX:4} = 0001


@dataclass(frozen=True)
class Clip:
    uid: str
    track: int
    start: int
    duration: int
    old_name: str
    source: str


@dataclass(frozen=True)
class Settings:
    template: str = DEFAULT_TEMPLATE
    prefix: str = 'V'  # TRACK = prefix + track number: V1, V2, ...


@dataclass(frozen=True)
class NamedClip:
    clip: Clip
    index: int
    new_name: str

    def manifest(self):
        return dict(asdict(self.clip), index=self.index, new_name=self.new_name)


# Windows device names are reserved with any extension (CON.mov, nul).
RESERVED = re.compile(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', re.IGNORECASE)


def safe(value):
    # Keep Unicode, spaces and source extension; produce portable future filenames.
    # NFC so that APFS-equivalent spellings collide; C0/C1 controls and bidi
    # overrides can disguise an extension.
    value = unicodedata.normalize('NFC', value)
    value = re.sub(r'[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069<>:"/\\|?*]', '_', value)
    value = value.strip().strip('. ').strip()
    return '_' + value if RESERVED.fullmatch(value) else value


def plan(clips, timeline, settings=Settings()):
    parsed = list(Formatter().parse(settings.template))
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in ('TRACK', 'INDEX', 'SOURCE', 'TIMELINE', 'PREFIX') or conversion:
            raise ValueError('Use only TRACK, INDEX, SOURCE, TIMELINE, PREFIX; no conversions')
        if spec and (field != 'INDEX' or not re.fullmatch(r'0?[1-9][0-9]?d?', spec)):
            raise ValueError('Only INDEX supports numeric width, e.g. {INDEX:04}')
        if field == 'INDEX' and spec and int(spec.rstrip('d')) > 12:
            raise ValueError('Maximum index width is 12')
    ordered = sorted(clips, key=lambda c: (c.track, c.start, c.uid))
    seen, ids, result = set(), set(), []
    index, track = 0, None
    for clip in ordered:
        # Resolve numbers individual-clip renders from 1 on every track.
        index = 1 if clip.track != track else index + 1
        track = clip.track
        values = dict(TRACK='%s%d' % (settings.prefix, clip.track), INDEX=index,
                      SOURCE=clip.source, TIMELINE=timeline, PREFIX=settings.prefix)
        parts = []
        for literal, field, spec, _ in parsed:
            parts.append(literal)
            if field:
                width = int(spec.rstrip('d')) if spec else 4
                parts.append('%0*d' % (width, index) if field == 'INDEX' else str(values[field]))
        name = safe(''.join(parts))
        if not name or len(name.encode('utf-8')) > 240:
            raise ValueError('Empty or overlong output name')
        if name.casefold() in seen or clip.uid in ids:
            raise ValueError('Duplicate output name or clip identity: ' + name)
        seen.add(name.casefold()); ids.add(clip.uid)
        result.append(NamedClip(clip, index, name))
    if not result:
        raise ValueError('No supported video clips in timeline')
    return result
