"""Conform Resolve's FCP7 XML: rename video clips and point them at renders.

Each renamed clip gets its own <file>: the matching render (in/out shifted by
the render's timecode, so handles and trimmed renders line up), or, without a
render folder, the same media facts under the new name and no path at all, so
nothing links back to the camera originals. Only the edited elements change;
audio and everything else stay exactly as Resolve exported them.
"""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
from xml.sax.saxutils import quoteattr
from .media import MediaError, frames_to_tc, probe, tc_to_frames
from .naming import Clip
from .xmlbytes import Unsupported, apply, parse, remove, text, text_edit

BYPASS = {'transforms': 'Basic Motion', 'crop': 'Crop', 'retime': 'Time Remap', 'opacity': 'Opacity'}
RENDER_EXTENSIONS = ('.mov', '.mp4', '.m4v', '.mxf')


@dataclass
class Item:
    node: object        # <clipitem>
    file: object        # full <file> definition it uses
    clip: Clip


def _definitions(root):
    """Resolve defines each <file> once and later refers to it as <file id=".."/>."""
    found, refs = {}, []

    def walk(node, clipitem):
        if node.tag == 'clipitem':
            clipitem = node
        if node.tag == 'file':
            refs.append((node, clipitem))
            if node.children and node.attrs.get('id') not in found:
                found[node.attrs.get('id')] = node
        for child in node.children:
            walk(child, clipitem)
    walk(root, None)
    return found, refs


def read(data, warnings):
    root = parse(data)
    if root.tag != 'xmeml':
        raise Unsupported('Not FCP7 XML')
    files, _ = _definitions(root)
    items = []
    for track, element in enumerate(root.path('sequence', 'media', 'video').all('track'), 1):
        for node in element.all('clipitem'):
            label = node.all('name')[0].text if node.all('name') else node.attrs.get('id', '?')
            try:
                definition = files.get(node.one('file').attrs.get('id'))
                if definition is None:
                    raise Unsupported('file definition missing')
                start, end = int(node.value('start')), int(node.value('end'))
                if start < 0 or end <= start:
                    raise Unsupported('clip edge is inside a transition')
                slug = [n.text for n in definition.all('mediaSource')] == ['Slug'] or (
                    definition.value('name') == 'Slug' and not definition.all('pathurl'))
                items.append(Item(node, definition, Clip(
                    node.attrs.get('id') or '%d:%d' % (track, start), track, start, end - start,
                    node.value('name'), definition.value('name'), not slug)))
            except ValueError as exc:
                warnings.append('V%d %s: %s; left unchanged.' % (track, label, exc))
    offline = sum(not item.clip.media for item in items)
    if offline:
        warnings.append('%d clips have no media (Resolve exports them as "Slug") and are not renamed. '
                        'Link their media in Resolve and export again.' % offline)
    return root, items


def clips(data, warnings):
    return [item.clip for item in read(data, warnings)[1]]


def for_resolve_import(data):
    """Resolve writes Basic Motion's vertical centre relative to a 16:9 frame of
    the sequence width but reads it relative to the real height, so Tilt and
    anchor Y drift on other aspects (x 1920/2160 on 3840x1920). Undo that for
    the XML that Resolve itself imports to build the DRT."""
    root = parse(data)
    chars = root.path('sequence', 'media', 'video', 'format', 'samplecharacteristics')
    width, height = int(chars.value('width')), int(chars.value('height'))
    factor = width * 9 / 16 / height
    if abs(factor - 1) < 1e-6:
        return data
    edits = []
    for track in root.path('sequence', 'media', 'video').all('track'):
        for node in track.all('clipitem'):
            for f in node.all('filter'):
                if _effect(f) != 'Basic Motion':
                    continue
                for param in f.one('effect').all('parameter'):
                    if param.all('parameterid') and param.value('parameterid') in ('center', 'centerOffset'):
                        edits += [text_edit(v, _number(float(v.text) * factor, 6)) for v in _all(param, 'vert')]
    return apply(data, edits)


def _all(node, tag):
    found = []
    for child in node.children:
        found += [child] if child.tag == tag else _all(child, tag)
    return found


def linked_renders(data):
    """Render files the conformed XML points at."""
    from urllib.parse import unquote, urlparse
    return sorted({unquote(urlparse(item.file.value('pathurl')).path) for item in read(data, [])[1]
                   if item.file.attrs.get('id', '').startswith('conform-') and item.file.all('pathurl')})


class Renders:
    """Render folder, matched by file name without extension (Resolve swaps the source's)."""

    def __init__(self, folder):
        self.folder = Path(folder).expanduser()
        if not self.folder.is_dir():
            raise ValueError('Render folder not found: %s' % self.folder)
        self.by_stem = {}
        for entry in os.scandir(self.folder):
            stem, ext = os.path.splitext(entry.name)
            if entry.is_file() and not entry.name.startswith('.') and ext.lower() in RENDER_EXTENSIONS:
                self.by_stem.setdefault(stem.casefold(), []).append(entry.path)
        self._info = {}

    def named(self, new_name):
        return self.by_stem.get(os.path.splitext(new_name)[0].casefold(), [])

    def containing(self, source):
        suffix = '_' + os.path.splitext(source)[0].casefold()
        return [p for stem, paths in self.by_stem.items() if stem.endswith(suffix) for p in paths]

    def info(self, path):
        if path not in self._info:
            try:
                self._info[path] = probe(path)
            except (MediaError, OSError, ValueError, KeyError) as exc:
                self._info[path] = exc
        return self._info[path]

    def prefetch(self, paths):
        # Renders often live on network shares: read headers in parallel.
        todo = sorted({p for p in paths if p not in self._info})
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(self.info, todo))




class Timing:
    """Where a clip sits in its file.

    <in>/<out> count clip frames at the sequence rate. With a Time Remap the
    graph maps clip frames to media time, also in sequence frames; one media
    unit is `k` frames of the file (k = 2 for 50p on a 25p timeline).
    """

    def __init__(self, item, sequence_base, api_range=None):
        node, rate = item.node, item.file.all('rate')
        self.k = (int(rate[0].value('timebase')) if rate else sequence_base) / sequence_base
        self.start = _source_tc(item.file)
        self.first_in, self.first_out = int(node.value('in')), int(node.value('out'))
        self.filter = next((f for f in node.all('filter') if _effect(f) == 'Time Remap'), None)
        # Resolve's exact source range, made relative to this file's start.
        self.api = tuple(x - self.start for x in api_range) if api_range and self.start is not None else None
        self.speed, self.reverse, self.ramp, self.graph, self.keys = 1.0, False, False, None, []
        self.freeze = False
        if self.filter is None:
            return
        params = {p.value('parameterid'): p for p in self.filter.one('effect').all('parameter') if p.all('parameterid')}
        self.speed = abs(float(params['speed'].value('value'))) / 100 if 'speed' in params else 1.0
        self.freeze = not self.speed
        self.reverse = 'reverse' in params and params['reverse'].value('value').strip().upper() == 'TRUE'
        self.ramp = 'variablespeed' in params and params['variablespeed'].value('value').strip() == '1'
        self.graph = params.get('graphdict')
        for key in (self.graph.all('keyframe') if self.graph is not None else []):
            virtual = 'start' if key.all('speedkfstart') else 'end' if key.all('speedkfend') else None
            self.keys.append((float(key.value('when')), float(key.value('value')), key, virtual))

    def media(self, when):
        points = sorted((w, v) for w, v, _, virtual in self.keys if not virtual) or sorted((w, v) for w, v, _, _ in self.keys)
        if len(points) >= 2:
            return _interpolate(points, when)
        return when * self.speed if self.filter is not None else when

    def clip_frame(self, media):
        points = sorted((v, w) for w, v, _, virtual in self.keys if not virtual) or sorted((v, w) for w, v, _, _ in self.keys)
        if len(points) >= 2:
            return _interpolate(points, media)
        return media / self.speed if self.filter is not None else media

    def source_range(self):
        """First and last file frame the clip shows (relative to the file start)."""
        if self.ramp and self.api:
            a, b = sorted(self.api)
            return a, b - 1
        a, b = self.media(self.first_in) * self.k, self.media(self.first_out) * self.k
        return math.floor(min(a, b) + 1e-6), math.ceil(max(a, b) - 1e-6) - 1


def _interpolate(points, x):
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1 or (x1, y1) == points[-1]:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _effect(filter_node):
    try:
        return filter_node.one('effect').value('name')
    except ValueError:
        return None


def _number(value, places=3):
    value = round(float(value), places)
    return '%d' % value if value.is_integer() else ('%.*g' % (places + 3, value))


def _round(value):
    return int(math.floor(value + 0.5))


def _source_tc(definition):
    tc = definition.all('timecode')
    if not tc:
        return None
    tc = tc[0]
    if tc.all('frame'):
        return int(tc.value('frame'))
    base = int(tc.path('rate', 'timebase').text)
    drop = bool(tc.all('displayformat')) and tc.value('displayformat').strip() == 'DF'
    return tc_to_frames(tc.value('string').strip(), base, drop)


def _rate_xml(rate):
    ntsc = rate.denominator == 1001
    return '<rate><timebase>%d</timebase><ntsc>%s</ntsc></rate>' % (round(rate), 'TRUE' if ntsc else 'FALSE')


def _render_file(fid, info, name):
    rate = _rate_xml(info.rate)
    parts = ['<file id=%s>' % quoteattr(fid), '<name>%s</name>' % text(name).decode(),
             '<pathurl>%s</pathurl>' % text(Path(info.path).resolve().as_uri()).decode(),
             '<duration>%d</duration>' % info.frames, rate]
    if info.tc_start is not None:
        parts.append('<timecode>%s<string>%s</string><frame>%d</frame><displayformat>%s</displayformat></timecode>' % (
            rate, frames_to_tc(info.tc_start, info.timebase, info.drop_frame), info.tc_start,
            'DF' if info.drop_frame else 'NDF'))
    parts.append('<media><video><duration>%d</duration><samplecharacteristics><width>%d</width>'
                 '<height>%d</height></samplecharacteristics></video></media></file>' % (info.frames, info.width, info.height))
    return ''.join(parts).encode('utf-8')


def _offline_file(data, definition, fid, name):
    """The source's media facts under the new name, without a path and without audio."""
    chunk = data[definition.start:definition.end]
    node = parse(chunk)
    edits = [text_edit(node.one('name'), name)]
    edits += [remove(n) for n in node.all('pathurl')]
    edits += [remove(n) for n in (node.path('media').all('audio') if node.all('media') else [])]
    chunk = apply(chunk, edits)
    return ('<file id=%s' % quoteattr(fid)).encode() + chunk[chunk.index(b'>'):]


def _constant_remap(speed, keys, frames_media):
    """Time Remap at one speed (for ramps, whose exported graph Resolve gets wrong)."""
    flags = ('speedkfstart', 'speedkfin', 'speedkfout', 'speedkfend')
    kfs = ''.join('<keyframe><when>%s</when><value>%s</value><speedvirtualkf>TRUE</speedvirtualkf><%s>TRUE</%s></keyframe>'
                  % (_number(w), _number(v), flag, flag) for (w, v), flag in zip(keys, flags))
    return ('<filter><enabled>TRUE</enabled><start>-1</start><end>-1</end><effect><name>Time Remap</name>'
            '<effectid>timeremap</effectid><effecttype>motion</effecttype><mediatype>video</mediatype>'
            '<effectcategory>motion</effectcategory>'
            '<parameter><name>speed</name><parameterid>speed</parameterid><value>%s</value><valuemin>-10000</valuemin><valuemax>10000</valuemax></parameter>'
            '<parameter><name>reverse</name><parameterid>reverse</parameterid><value>FALSE</value></parameter>'
            '<parameter><name>frameblending</name><parameterid>frameblending</parameterid><value>FALSE</value></parameter>'
            '<parameter><name>variablespeed</name><parameterid>variablespeed</parameterid><value>0</value><valuemin>0</valuemin><valuemax>1</valuemax></parameter>'
            '<parameter><name>graphdict</name><parameterid>graphdict</parameterid>%s<valuemin>0</valuemin><valuemax>%s</valuemax>'
            '<interpolation><name>FCPCurve</name></interpolation></parameter></effect></filter>'
            % (_number(speed * 100), kfs, _number(frames_media))).encode()


def _pick(renders, named, item, timing, warnings):
    label = named.new_name
    first, last = timing.source_range()

    def covers(info):
        return (not isinstance(info, Exception) and info.tc_start is not None and timing.start is not None
                and info.tc_start <= timing.start + first and timing.start + last <= info.tc_start + info.frames - 1)

    exact = renders.named(named.new_name)
    renders.prefetch(exact)
    good = [p for p in exact if not isinstance(renders.info(p), Exception)]
    for path in exact:
        if path not in good:
            warnings.append('%s: render %s unreadable (%s).' % (label, Path(path).name, renders.info(path)))
    if len(good) == 1:
        info = renders.info(good[0])
        if info.tc_start is not None and timing.start is not None and not covers(info):
            warnings.append('%s: render %s does not cover the clip; check handles.' % (label, Path(good[0]).name))
        return info
    if len(good) > 1:
        warnings.append('%s: several renders with this name; left offline.' % label)
        return None
    if exact:
        return None
    others = renders.containing(item.clip.source)
    renders.prefetch(others)
    matches = [p for p in others if covers(renders.info(p))]
    if len(matches) == 1:
        warnings.append('%s: matched render %s by timecode.' % (label, Path(matches[0]).name))
        return renders.info(matches[0])
    warnings.append('%s: no render found; left offline.' % label)
    return None


def conform(data, names, renders=None, bypass=(), warnings=None, sources=None):
    """Return the conformed XML. `names` is the plan built from this same XML;
    `sources` optionally maps clip uid to Resolve's exact (first, end) source
    frames as absolute timecode frames."""
    warnings = warnings if warnings is not None else []
    bypass = set(bypass)
    unknown = bypass - set(BYPASS)
    if unknown:
        raise ValueError('Unknown bypass option: %s' % ', '.join(sorted(unknown)))
    root, items = read(data, [])
    sequence_base = int(root.path('sequence', 'rate', 'timebase').text)
    by_uid = {item.clip.uid: item for item in items}
    files, refs = _definitions(root)
    edits, rewritten = [], set()
    for n, named in enumerate(names, 1):
        item = by_uid.get(named.clip.uid)
        if item is None or item.clip != named.clip:
            raise Unsupported('Plan belongs to a different XML snapshot')
        if not named.clip.media:
            continue
        try:
            timing = Timing(item, sequence_base, (sources or {}).get(named.clip.uid))
            edits += _conform_item(data, item, named, n, timing, renders, bypass, warnings)
        except (ValueError, KeyError) as exc:
            try:
                # Still rename it and cut the link to the original.
                ref = item.node.one('file')
                edits += [text_edit(item.node.one('name'), named.new_name),
                          (ref.start, ref.end, _offline_file(data, item.file, 'conform-%04d' % n, named.new_name))]
                warnings.append('%s: %s; left offline.' % (named.new_name, exc))
            except (ValueError, KeyError) as exc2:
                warnings.append('%s: %s; clip left unchanged.' % (named.new_name, exc2))
                continue
        rewritten.add(id(item.node))
    # A definition that leaves with a rewritten clip must stay available to the
    # clips (usually linked audio) that still refer to it.
    for fid, definition in files.items():
        owner = next((c for node, c in refs if node is definition), None)
        if owner is None or id(owner) not in rewritten:
            continue
        keep = [node for node, c in refs if node.attrs.get('id') == fid and node is not definition
                and (c is None or id(c) not in rewritten)]
        if keep:
            edits.append((keep[0].start, keep[0].end, data[definition.start:definition.end]))
    return apply(data, edits)


def _conform_item(data, item, named, n, timing, renders, bypass, warnings):
    node, label, t = item.node, named.new_name, timing
    old_in, old_out = t.first_in, t.first_out
    length = old_out - old_in
    edits = [text_edit(node.one('name'), named.new_name)]
    fid = 'conform-%04d' % n
    info = _pick(renders, named, item, t, warnings) if renders else None
    if info is not None and round(info.rate) != round(t.k * int(node.path('rate', 'timebase').text)):
        warnings.append('%s: render frame rate %s differs from the source; left offline.' % (label, info.rate))
        info = None
    drop_retime = t.filter is not None and 'retime' in bypass
    remap = None       # replacement Time Remap filter, bytes
    if info is not None:
        if drop_retime:
            # Speed is baked into the render: centre the clip in its handles.
            spare = info.frames - length
            if spare < 0 or spare % 2:
                warnings.append('%s: retimed render has uneven or missing handles; check timing.' % label)
            new_in, new_dur = max(spare // 2, 0), info.frames
        else:
            if info.tc_start is not None and t.start is not None:
                offset = info.tc_start - t.start          # render frame 0, in file frames
            else:
                first, last = t.source_range()
                offset = first - (info.frames - (last - first + 1)) // 2
                warnings.append('%s: render has no timecode; assumed equal handles.' % label)
            lo, hi = offset / t.k, (offset + info.frames) / t.k     # render span in media time
            if t.freeze:
                raise Unsupported('freeze frame')
            if t.filter is not None and not t.reverse and not t.ramp and t.api:
                # Resolve renders from its exact source position; the exported
                # graph is rounded, so prefer the position Resolve reports.
                # Resolve floors the fractional in-point.
                new_in = int(math.floor((min(t.api) / t.k - lo) / t.speed + 1e-6))
                new_dur = max(_round((hi - lo) / t.speed), new_in + length)
            elif t.ramp:
                if not t.api:
                    raise Unsupported('speed ramp needs Resolve source range; run from Resolve')
                a, b = t.api
                speed = abs(b - a) / t.k / length
                new_in = _round((min(a, b) / t.k - lo) / speed)
                new_dur = _round((hi - lo) / speed)
                remap = _constant_remap(speed, [(0, 0), (new_in, min(a, b) / t.k - lo), (new_in + length, max(a, b) / t.k - lo),
                                                (new_dur, hi - lo)], info.frames / t.k)
                warnings.append('%s: speed ramp exported as constant %.1f%%.' % (label, speed * 100))
            else:
                origin = t.clip_frame(hi if t.reverse else lo)
                new_in = _round(old_in - origin)
                new_dur = max(_round(abs(t.clip_frame(hi) - t.clip_frame(lo))), new_in + length)
        if new_in < 0 or new_in + length > new_dur:
            warnings.append('%s: render is shorter than the clip; check handles.' % label)
        ref = node.one('file')
        edits.append((ref.start, ref.end, _render_file(fid, info, Path(info.path).name)))
        chars = item.file.all('media') and item.file.path('media').all('video')
        chars = chars and chars[0].all('samplecharacteristics')
        if chars and 'transforms' not in bypass:
            w, h = int(chars[0].value('width')), int(chars[0].value('height'))
            if abs(w * info.height - h * info.width) > 0.005 * h * info.width:
                warnings.append('%s: render %dx%d has another aspect than the source %dx%d; framing may change.'
                                % (label, info.width, info.height, w, h))
    else:
        new_in, new_dur = old_in, int(node.value('duration'))
        if drop_retime:
            new_in, new_dur = _round(t.media(old_in) * t.k), int(item.file.value('duration'))
            warnings.append('%s: retime bypass without a render refers to source frames.' % label)
        ref = node.one('file')
        edits.append((ref.start, ref.end, _offline_file(data, item.file, fid, named.new_name)))
    shift = old_in - new_in
    if (new_in, new_dur) != (old_in, int(node.value('duration'))):
        edits += [text_edit(node.one('in'), new_in), text_edit(node.one('out'), new_in + length),
                  text_edit(node.one('duration'), new_dur)]
    for f in node.all('filter'):
        effect = _effect(f)
        if any(BYPASS[b] == effect for b in bypass):
            edits.append(remove(f))
        elif f is t.filter:
            if remap is not None:
                edits.append((f.start, f.end, remap))
            elif info is not None and not drop_retime:
                edits += _move_graph(t, shift, lo, info, new_dur)
        else:
            edits += [text_edit(k.one('when'), _number(float(k.value('when')) - shift)) for k in _keyframes(f) if shift]
            if f.all('end') and f.value('end').strip() != '-1' and new_dur != int(node.value('duration')):
                edits.append(text_edit(f.one('end'), new_dur))
    if 'opacity' in bypass:
        edits += [text_edit(c, 'normal') for c in node.all('compositemode')]
    return edits


def _keyframes(node):
    found = []
    for child in node.children:
        if child.tag == 'keyframe' and child.all('when'):
            found.append(child)
        else:
            found += _keyframes(child)
    return found


def _move_graph(t, shift, lo, info, new_dur):
    """Translate a constant-speed graph onto the render: clip frames by `shift`,
    media time by `lo` (the render's first frame). Resolve's virtual end keys sit
    a frame off the line; that offset is kept."""
    edits = []
    for when, value, key, virtual in t.keys:
        quirk = value - t.media(when)
        if virtual == 'start':
            when = 0
        elif virtual == 'end':
            when = new_dur
        else:
            when, quirk = when - shift, 0
        value = t.media(when + shift) - lo + quirk
        edits += [text_edit(key.one('when'), _number(when)), text_edit(key.one('value'), _number(value))]
    if t.graph is not None and t.graph.all('valuemax'):
        edits.append(text_edit(t.graph.one('valuemax'), _number(info.frames / t.k - 1)))
    return edits
