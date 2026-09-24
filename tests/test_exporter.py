import csv
import json
from fractions import Fraction
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from conform import fcp7, media
from conform.exporter import build_bundle, export_current, TEMP_BIN
from conform.naming import Settings, Clip, plan, safe
from conform.xmlbytes import Unsupported, parse

FIX = Path(__file__).parent / 'fixtures'
SOURCE = (FIX / 'source.xml').read_bytes()                  # Resolve's FCP7 export of the timeline
REFERENCE = (FIX / 'resolve_render_conform.xml').read_bytes()  # Resolve's own XML on the renders
RAMPS = 17


def rows(data):
    root = parse(data)
    found = {}
    for t, track in enumerate(root.path('sequence', 'media', 'video').all('track'), 1):
        for ci in track.all('clipitem'):
            found[(t, int(ci.value('start')))] = ci
    return found


def rows_from(root):
    return {(t, int(ci.value('start'))): ci for t, track in enumerate(root.path('sequence', 'media', 'video').all('track'), 1)
            for ci in track.all('clipitem')}


def fake_renders(folder):
    """Empty files named like the renders; their facts come from Resolve's own XML."""
    table = {}
    for ci in rows(REFERENCE).values():
        f = ci.one('file')
        path = Path(folder) / Path(unquote(f.value('pathurl'))).name
        path.touch()
        chars = f.path('media', 'video', 'samplecharacteristics')
        base = int(f.path('rate', 'timebase').text)
        table[str(path)] = media.MediaInfo(str(path), int(f.value('duration')), Fraction(base),
                                           int(chars.value('width')), int(chars.value('height')),
                                           media.tc_to_frames(f.path('timecode').value('string'), base))
    return table


def source_ranges(items):
    """Resolve API positions (relative to the source) as absolute timecode frames."""
    ranges = {(t, s): (a, b) for t, s, a, b in json.loads((FIX / 'source_ranges.json').read_text()) if a is not None}
    return {i.clip.uid: tuple(fcp7._source_tc(i.file) + x for x in ranges[(i.clip.track, i.clip.start)])
            for i in items if (i.clip.track, i.clip.start) in ranges and fcp7._source_tc(i.file) is not None}


class ConformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.root, self.items = fcp7.read(SOURCE, [])
        self.names = plan([i.clip for i in self.items], 'full-mv')

    def conform(self, renders=True, bypass=()):
        warnings = []
        folder = None
        if renders:
            table = fake_renders(self.out)
            folder = fcp7.Renders(self.out)
            patcher = patch('conform.fcp7.probe', side_effect=lambda p: table[p])
            patcher.start(); self.addCleanup(patcher.stop)
        result = fcp7.conform(SOURCE, self.names, folder, bypass, warnings, source_ranges(self.items))
        return result, warnings

    def test_names_follow_resolve_render_numbering(self):
        self.assertEqual(len(self.names), 161)
        by_track = {}
        for n in self.names:
            by_track.setdefault(n.clip.track, []).append(n.index)
        self.assertEqual(by_track[2][0], 1)  # Resolve restarts the index on every track
        renders = {Path(unquote(ci.one('file').value('pathurl'))).stem for ci in rows(REFERENCE).values()}
        ours = {Path(n.new_name).stem for n in self.names}
        self.assertGreaterEqual(len(renders & ours), 158)

    def test_offline_cuts_every_video_path_and_keeps_audio(self):
        result, warnings = self.conform(renders=False)
        root, items = fcp7.read(result, [])
        files, refs = fcp7._definitions(root)
        for item in items:
            self.assertFalse(item.file.all('pathurl'), item.clip.old_name)
            if item.clip.media:
                self.assertEqual(item.file.value('name'), item.clip.old_name)  # clip and file carry the new name
                self.assertFalse(item.file.path('media').all('audio'))
        self.assertEqual(len({i.file.attrs['id'] for i in items}), len(items))
        self.assertTrue(all(node.attrs.get('id') in files for node, _ in refs))
        before, after = rows(SOURCE), rows(result)
        for key, ci in before.items():
            self.assertEqual((ci.value('in'), ci.value('out')), (after[key].value('in'), after[key].value('out')))
        get = lambda node, tag: [c.text if not c.children else c.attrs.get('id') for c in node.all(tag)]
        audio = lambda data: [(ci.attrs.get('id'), get(ci, 'in'), get(ci, 'out'), get(ci, 'file'))
                              for tr in parse(data).path('sequence', 'media', 'audio').all('track') for ci in tr.all('clipitem')]
        self.assertEqual(audio(SOURCE), audio(result))

    def test_renders_line_up_like_resolve_conform(self):
        result, warnings = self.conform()
        ours, ref = rows(result), rows(REFERENCE)
        ramps = {w.split(':')[0] for w in warnings if 'speed ramp' in w}
        self.assertEqual(len(ramps), RAMPS)
        same = [k for k in ref if (ref[k].value('in'), ref[k].value('out')) == (ours[k].value('in'), ours[k].value('out'))]
        others = [k for k in ref if k not in same and ours[k].value('name') not in ramps]
        # Remaining differences: one clip Resolve reports a frame off, and a clip that is offline in this project.
        self.assertLessEqual(len(others), 2, [(k, ours[k].value('name')) for k in others])
        first = ours[(1, 0)]
        self.assertEqual((first.value('in'), first.value('out'), first.value('duration')), ('10', '118', '128'))
        self.assertEqual(first.one('file').value('name'), 'V1-0001_A003C014_260817_R14E.mov')
        self.assertTrue(first.one('file').value('pathurl').startswith(self.out.resolve().as_uri() + '/'))

    def test_ramps_become_constant_speed_inside_the_render(self):
        result, warnings = self.conform()
        ci = rows(result)[(1, 517)]
        remap = next(f for f in ci.all('filter') if fcp7._effect(f) == 'Time Remap')
        params = {p.value('parameterid'): p for p in remap.one('effect').all('parameter') if p.all('parameterid')}
        self.assertEqual(params['variablespeed'].value('value'), '0')
        self.assertTrue(0 < int(ci.value('in')) and int(ci.value('out')) <= int(ci.value('duration')))

    def test_reverse_and_frame_rate_mismatch(self):
        result, _ = self.conform()
        ours = rows(result)
        self.assertEqual((ours[(1, 1397)].value('in'), ours[(1, 1397)].value('duration')), ('8', '34'))   # reverse
        self.assertEqual((ours[(1, 2074)].value('in'), ours[(1, 2074)].value('duration')), ('10', '36'))  # 50p on 25p

    def test_xml_already_on_renders(self):
        # Once a timeline has been rendered, Resolve exports its XML pointing at the renders.
        table = fake_renders(self.out)
        patcher = patch('conform.fcp7.probe', side_effect=lambda p: table[p])
        patcher.start(); self.addCleanup(patcher.stop)
        by_position = {(i.clip.track, i.clip.start): r for i in self.items for r in [source_ranges(self.items).get(i.clip.uid)] if r}
        root, items = fcp7.read(REFERENCE, [])
        names = plan([i.clip for i in items], 'full-mv')
        sources = {i.clip.uid: by_position[(i.clip.track, i.clip.start)] for i in items if (i.clip.track, i.clip.start) in by_position}
        warnings = []
        result = fcp7.conform(REFERENCE, names, fcp7.Renders(self.out), (), warnings, sources)
        self.assertFalse([w for w in warnings if 'does not cover' in w or 'shorter' in w], warnings)
        before, after = rows(REFERENCE), rows(result)
        ramps = {w.split(':')[0] for w in warnings if 'speed ramp' in w}
        moved = [k for k in before if after[k].value('name') not in ramps
                 and (before[k].value('in'), before[k].value('out')) != (after[k].value('in'), after[k].value('out'))]
        self.assertLessEqual(len(moved), 1, moved)

    def test_clips_without_media_keep_their_name_and_number(self):
        warnings = []
        clips = fcp7.clips(SOURCE, warnings)
        self.assertTrue(any('2 clips have no media' in w for w in warnings))
        names = {n.clip.start: n for n in plan(clips, 'full-mv') if n.clip.track == 1}
        slug = names[5270]
        self.assertFalse(slug.clip.media)
        self.assertEqual(slug.new_name, slug.clip.old_name)       # not renamed to "..._Slug"
        self.assertEqual(slug.index, names[5238].index + 1)       # still counted
        result, _ = self.conform(renders=False)
        self.assertEqual(rows(result)[(1, 5270)].value('name'), slug.clip.old_name)

    def test_bypass_removes_baked_effects(self):
        result, warnings = self.conform(bypass=('transforms', 'crop', 'opacity'))
        offline = {(i.clip.track, i.clip.start) for i in self.items if not i.clip.media}
        for key, ci in rows(result).items():
            if key not in offline:
                self.assertFalse({fcp7._effect(f) for f in ci.all('filter')} & {'Basic Motion', 'Crop', 'Opacity'})
                self.assertTrue(all(c.text == 'normal' for c in ci.all('compositemode')))

    def test_retime_bypass_centres_clip_in_handles(self):
        result, warnings = self.conform(bypass=('retime',))
        ci = rows(result)[(1, 845)]
        self.assertFalse(any(fcp7._effect(f) == 'Time Remap' for f in ci.all('filter')))
        spare = int(ci.value('duration')) - (int(ci.value('out')) - int(ci.value('in')))
        self.assertEqual(int(ci.value('in')), spare // 2)

    def test_shared_file_definition_stays_available(self):
        xml = (b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media>'
               b'<video><track><clipitem id="v"><name>a.mov</name><duration>50</duration><start>0</start><end>10</end><in>5</in><out>15</out>'
               b'<file id="f"><name>a.mov</name><pathurl>file:///src/a.mov</pathurl><duration>50</duration><rate><timebase>25</timebase></rate>'
               b'<media><video><duration>50</duration></video><audio><channelcount>2</channelcount></audio></media></file></clipitem></track></video>'
               b'<audio><track><clipitem id="a"><name>a.mov</name><start>0</start><end>10</end><in>5</in><out>15</out><file id="f"/></clipitem></track></audio>'
               b'</media></sequence></xmeml>')
        names = plan(fcp7.clips(xml, []), 't')
        result = fcp7.conform(xml, names)
        files, refs = fcp7._definitions(parse(result))
        self.assertIn(b'file:///src/a.mov', result)           # audio keeps its source
        self.assertEqual(files['f'].value('name'), 'a.mov')
        self.assertFalse(files['conform-0001'].all('pathurl'))

    def test_foreign_encoding_and_entities_rejected(self):
        with self.assertRaises(Unsupported):
            parse(SOURCE.replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"', 1))
        with self.assertRaises(Unsupported):
            parse(b'<!DOCTYPE x [<!ENTITY bad "oops">]><x>&bad;</x>')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        (self.out / 'in.xml').write_bytes(SOURCE)

    def test_bundle_layout(self):
        folder = self.out / 'exports'
        files, names, warnings = build_bundle(self.out / 'in.xml', folder, 'full-mv', export_drt=True)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['full-mv.csv', 'full-mv.xml', 'full-mv_warnings.txt'])
        self.assertEqual(sorted(p.name for p in files), sorted(p.name for p in folder.iterdir()))
        self.assertTrue(any('DRT is built by Resolve' in w for w in warnings))
        with (folder / 'full-mv.csv').open(encoding='utf-8-sig') as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 159)  # two clips have no media
        again, _, _ = build_bundle(self.out / 'in.xml', folder, 'full-mv')  # never overwrites
        self.assertEqual(sorted(p.name for p in again), ['full-mv_2.csv', 'full-mv_2.xml', 'full-mv_2_warnings.txt'])
        self.assertEqual(len(list(folder.iterdir())), 6)

    def test_drt_maker_gets_conformed_xml(self):
        seen = []
        def make(xml, drt, renders):
            seen.append((xml.read_bytes(), renders)); drt.write_bytes(b'drt')
        files, _, _ = build_bundle(self.out / 'in.xml', self.out, 'full-mv', export_drt=True, export_xml=False, make_drt=make)
        self.assertEqual(sorted(p.name for p in files if p.suffix != '.txt'), ['full-mv.csv', 'full-mv.drt'])
        self.assertNotIn(b'<pathurl>file:///Volumes/Media/Project/Sources/A003C014', seen[0][0])
        self.assertEqual(seen[0][1], [])  # offline: nothing to import

    def test_no_formats_rejected(self):
        with self.assertRaises(ValueError):
            build_bundle(self.out / 'in.xml', self.out, 't', export_drt=False, export_xml=False, export_csv=False)


class NamingTests(unittest.TestCase):
    def test_numbering_restarts_per_track_and_prefix_names_the_track(self):
        clips = [Clip('b', 2, 10, 20, 'x.mov', 'x.mov'), Clip('a', 1, 10, 20, 'x.mov', 'x.mov'), Clip('c', 1, 30, 20, 'y.mov', 'y.mov')]
        self.assertEqual([n.new_name for n in plan(clips, 't')], ['V1-0001_x.mov', 'V1-0002_y.mov', 'V2-0001_x.mov'])
        self.assertEqual([n.new_name for n in plan(clips, 't', Settings(prefix='A'))][2], 'A2-0001_x.mov')
        self.assertEqual(plan(clips[1:], 't', Settings(template='{INDEX}_{SOURCE}'))[0].new_name, '0001_x.mov')
        self.assertEqual(plan(clips[1:], 't', Settings(template='{INDEX:6}_{SOURCE}'))[0].new_name, '000001_x.mov')
        with self.assertRaises(ValueError):  # without {TRACK} two tracks would share names
            plan(clips, 't', Settings(template='{INDEX}_{SOURCE}'))

    def test_rejects_collision_and_unsafe_template(self):
        clips = [Clip('a', 1, 0, 20, 'a', 'same'), Clip('b', 1, 20, 20, 'a', 'same')]
        for template in ('{SOURCE}', '{INDEX.__class__}', '{INDEX!r}', '{INDEX:099}', '{unknown}'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                plan(clips, 't', Settings(template=template))

    def test_every_index_width_is_zero_filled(self):
        clips = [Clip('a', 1, 0, 20, 'x.mov', 'x.mov')]
        for template in ('{INDEX:4}_{SOURCE}', '{INDEX:4d}_{SOURCE}', '{INDEX:04}_{SOURCE}'):
            with self.subTest(template=template):
                self.assertEqual(plan(clips, 't', Settings(template=template))[0].new_name, '0001_x.mov')

    def test_file_names_are_portable(self):
        for raw, expected in (('.env', 'env'), ('CON.mov', '_CON.mov'), ('nul', '_nul'), ('a .', 'a'),
                              ('clip‮vom.exe', 'clip_vom.exe'), ('x\x85y', 'x_y')):
            with self.subTest(raw=raw):
                self.assertEqual(safe(raw), expected)
        clips = [Clip('a', 1, 0, 20, 'x', 'é.mov'), Clip('b', 1, 20, 20, 'x', 'é.mov')]
        with self.assertRaises(ValueError):
            plan(clips, 't', Settings(template='{SOURCE}'))


def quicktime(path, frames=128, timescale=25, width=2048, height=1152, tc=272476):
    """Smallest MOV the parser needs: a video track and a tmcd track."""
    def atom(kind, *parts):
        body = b''.join(parts)
        return struct.pack('>I4s', 8 + len(body), kind) + body
    def full(kind, payload):
        return atom(kind, b'\0\0\0\0' + payload)
    def track(handler, stsd_entry, stts, chunk_offset):
        stbl = atom(b'stbl', full(b'stsd', struct.pack('>I', 1) + stsd_entry), full(b'stts', stts),
                    full(b'stco', struct.pack('>II', 1, chunk_offset)))
        mdhd = full(b'mdhd', struct.pack('>IIII', 0, 0, timescale, frames) + b'\0' * 4)
        hdlr = full(b'hdlr', b'mhlr' + handler + b'\0' * 12)
        return atom(b'trak', atom(b'mdia', mdhd, hdlr, atom(b'minf', stbl)))
    video_entry = atom(b'apch', b'\0' * 6 + b'\0\1' + b'\0' * 16 + struct.pack('>HH', width, height) + b'\0' * 50)
    tmcd_entry = atom(b'tmcd', b'\0' * 6 + b'\0\1' + struct.pack('>IIIIB', 0, 0, timescale, 1, timescale) + b'\0' * 3)
    mdat = atom(b'mdat', struct.pack('>I', tc))
    head = atom(b'ftyp', b'qt  \0\0\0\0qt  ')
    offset = len(head) + 8
    moov = atom(b'moov', track(b'vide', video_entry, struct.pack('>III', 1, frames, 1), 0),
                track(b'tmcd', tmcd_entry, struct.pack('>III', 1, 1, frames), offset))
    Path(path).write_bytes(head + mdat + moov)


class MediaTests(unittest.TestCase):
    def test_quicktime_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'r.mov'
            quicktime(path)
            info = media.probe(path)
        self.assertEqual((info.frames, info.rate, info.width, info.height), (128, 25, 2048, 1152))
        self.assertEqual(media.frames_to_tc(info.tc_start, info.timebase), '03:01:39:01')

    def test_drop_frame_timecode_round_trip(self):
        for tc in ('01:00:00;00', '00:10:00;00', '00:01:00;02', '23:59:59;29'):
            self.assertEqual(media.frames_to_tc(media.tc_to_frames(tc, 30, True), 30, True), tc)


class FakeItem:
    def __init__(self, track, clip): self.track, self.c = track, clip
    def GetUniqueId(self): return self.c.uid
    def GetName(self): return self.c.old_name
    def GetStart(self): return self.c.start + 90000
    def GetDuration(self): return self.c.duration
    def GetSourceStartFrame(self): return 0
    def GetSourceEndFrame(self): return self.c.duration
    def GetProperty(self, name=None):
        props = getattr(self, 'props', {'ZoomX': 2.0, 'FlipX': True, 'Opacity': 100.0})
        return props if name is None else props.get(name)
    def SetProperty(self, name, value):
        self.props = dict(self.GetProperty(), **{name: value}); return True


class FakeFolder:
    def __init__(self, name): self.name, self.clips = name, []
    def GetClipList(self): return list(self.clips)


class FakePool:
    def __init__(self):
        self.root, self.folders, self.current, self.log = FakeFolder('Master'), [], None, []
        self.current = self.root
    def GetRootFolder(self): return self.root
    def GetCurrentFolder(self): return self.current
    def SetCurrentFolder(self, f): self.current = f; return True
    def AddSubFolder(self, parent, name):
        f = FakeFolder(name); self.folders.append(f); return f
    def ImportMedia(self, paths):
        self.current.clips += paths; return ['clip'] * len(paths)
    def ImportTimelineFromFile(self, path, options):
        self.log.append(('import', options)); self.current.clips.append('render clip')
        return FakeImported()
    def DeleteTimelines(self, t): self.log.append('delete timeline'); return True
    def DeleteClips(self, c): self.log.append(('delete clips', len(c))); return True
    def DeleteFolders(self, f): self.folders = [x for x in self.folders if x not in f]; return True


class FakeImported:
    def __init__(self): self.items = [FakeItem(1, fcp7.clips(SOURCE, [])[0])]
    def GetStartFrame(self): return 90000
    def GetTrackCount(self, kind): return 1
    def GetItemListInTrack(self, kind, track): return self.items
    def Export(self, path, kind): Path(path).write_bytes(b'drt'); return True


class FakeTimeline:
    def __init__(self):
        self.clips = fcp7.clips(SOURCE, [])
    def GetUniqueId(self): return 'timeline'
    def GetName(self): return 'full-mv'
    def GetStartFrame(self): return 90000
    def GetTrackCount(self, kind): return 2
    def GetItemListInTrack(self, kind, track): return [FakeItem(track, c) for c in self.clips if c.track == track]
    def Export(self, path, kind):
        if kind != 'xml': return False
        Path(path).write_bytes(SOURCE); return True


class FakeProject:
    def __init__(self): self.timeline, self.pool = FakeTimeline(), FakePool()
    def GetName(self): return 'project'
    def GetCurrentTimeline(self): return self.timeline
    def SetCurrentTimeline(self, t): return True
    def GetMediaPool(self): return self.pool


class FakeResolve:
    EXPORT_FCP_7_XML, EXPORT_DRT = 'xml', 'drt'
    def __init__(self): self.project = FakeProject()
    def GetProjectManager(self): return self
    def GetCurrentProject(self): return self.project


class APITests(unittest.TestCase):
    def test_export_builds_drt_in_a_temporary_bin_and_cleans_up(self):
        resolve = FakeResolve()
        with tempfile.TemporaryDirectory() as temp:
            files, names, warnings = export_current(resolve, temp)
            self.assertEqual(sorted(p.name for p in files if p.suffix != '.txt'),
                             ['full-mv.csv', 'full-mv.drt', 'full-mv.xml'])
        pool = resolve.project.pool
        self.assertEqual(pool.folders, [])
        self.assertIn('delete timeline', pool.log)
        self.assertIn(('delete clips', 1), pool.log)
        self.assertIs(pool.current, pool.root)
        self.assertEqual(TEMP_BIN, 'Conform Export (temp)')

    def test_vertical_centre_is_corrected_for_resolve_import(self):
        fixed = parse(fcp7.for_resolve_import(SOURCE))
        before = parse(SOURCE)
        def vert(root):
            ci = rows_from(root)[(1, 184)]
            f = next(f for f in ci.all('filter') if fcp7._effect(f) == 'Basic Motion')
            p = next(p for p in f.one('effect').all('parameter') if p.value('parameterid') == 'center')
            return float(p.path('value', 'vert').text)
        self.assertAlmostEqual(vert(fixed) * 1920, vert(before) * 2160, places=2)  # Tilt 777 survives the import


if __name__ == '__main__':
    unittest.main()
