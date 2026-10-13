import csv
import json
from fractions import Fraction
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from conform import fcp7, media
from conform.exporter import build_bundle, drt_maker, export_current, preview, TEMP_BIN
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


def clipitem(uid, source, start, tc, first, filters=''):
    """A 50-frame clip from `first` of a source whose timecode starts at frame `tc`."""
    return ('<clipitem id="%s"><name>%s</name><duration>3000</duration><start>%d</start><end>%d</end><in>%d</in><out>%d</out>'
            '<file id="f%s"><name>%s</name><pathurl>file:///src/%s</pathurl><duration>3000</duration><rate><timebase>25</timebase></rate>'
            '<timecode><rate><timebase>25</timebase></rate><string>x</string><frame>%d</frame></timecode></file>%s</clipitem>'
            % (uid, source, start, start + 50, first, first + 50, uid, source, source, tc, filters))


def sequence(*tracks):
    return ('<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media><video>%s'
            '</video></media></sequence></xmeml>' % ''.join('<track>%s</track>' % ''.join(t) for t in tracks)).encode()


REMAP_100 = ('<filter><start>-1</start><end>-1</end><effect><name>Time Remap</name>'
             '<parameter><parameterid>speed</parameterid><value>100</value></parameter></effect></filter>')


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

    def test_render_names_match_whatever_the_extension(self):
        for name in ('V1-0001_a.mxf.mov', 'V1-0002_b.MXF', 'V1-0003_c.avi', 'V2-0009_d.mov', 'notes.txt'):
            (self.out / name).touch()
        folder = fcp7.Renders(self.out)
        self.assertTrue(folder.named('V1-0001_a.mxf'))          # render kept the source extension
        self.assertTrue(folder.named('V1-0002_b.mov'))          # other extension, other case
        self.assertTrue(folder.named('V1-0003_c.mov'))          # any container
        self.assertFalse(folder.named('notes.mov'))              # text files are never renders
        self.assertTrue(folder.containing('d.mov'))              # numbered differently: found by source name
        self.assertFalse(folder.numbered('V3-0001_'))            # nothing with that number
        self.assertFalse(folder.numbered(''))                    # no number in the template: no guessing

    def test_stills_find_their_one_frame_render(self):
        # Resolve renders a still as a one-frame "sequence" and rewrites the source
        # part of the name; the clip number still identifies it.
        for name in ('V2-0008_0.mov', 'V2-0009_title.00000000.mov', 'V2-0001_badge 00+.mov'):
            (self.out / name).touch()
        folder = fcp7.Renders(self.out)
        by_number = lambda track, index, src: [Path(p).name for p in folder.numbered(
            plan([Clip(str(i), track, i, 1, src, src) for i in range(index)], 't')[-1].key)]
        self.assertEqual(by_number(2, 8, '1.png'), ['V2-0008_0.mov'])
        self.assertEqual(by_number(2, 9, 'title.png'), ['V2-0009_title.00000000.mov'])
        self.assertEqual(by_number(2, 1, 'badge 18+.png'), ['V2-0001_badge 00+.mov'])

    STILL = (b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track>'
             b'<clipitem id="s"><name>1.png</name><duration>1500001</duration><start>0</start><end>125</end><in>90000</in><out>90125</out>'
             b'<file id="fs"><name>1.png</name><pathurl>file:///src/1.png</pathurl><duration>1</duration><rate><timebase>25</timebase></rate>'
             b'<timecode><string>00:00:00:00</string><rate><timebase>25</timebase></rate></timecode></file>'
             b'<filter><start>-1</start><end>-1</end><effect><name>Time Remap</name><parameter><parameterid>speed</parameterid><value>0</value></parameter></effect></filter>'
             b'</clipitem></track></video></media></sequence></xmeml>')

    def test_still_links_to_its_render_and_keeps_holding_the_frame(self):
        (self.out / 'V1-0001_0.mov').touch()
        render = media.MediaInfo(str(self.out / 'V1-0001_0.mov'), 1, Fraction(25), 3840, 2160, 0)
        for bypass in ((), ('retime',)):    # a hold is not a speed the render could bake in
            with self.subTest(bypass=bypass):
                warnings = []
                with patch('conform.fcp7.probe', return_value=render):
                    result = fcp7.conform(self.STILL, plan(fcp7.clips(self.STILL, []), 't'), fcp7.Renders(self.out),
                                          bypass, warnings)
                clip = rows(result)[(1, 0)]
                self.assertEqual(clip.one('file').value('name'), 'V1-0001_0.mov')
                self.assertEqual((clip.value('in'), clip.value('out')), ('90000', '90125'))
                self.assertEqual([fcp7._effect(f) for f in clip.all('filter')], ['Time Remap'])
                self.assertFalse(warnings)
        offline = rows(fcp7.conform(self.STILL, plan(fcp7.clips(self.STILL, []), 't'), None, ('retime',)))[(1, 0)]
        self.assertEqual([fcp7._effect(f) for f in offline.all('filter')], ['Time Remap'])

    def link(self, xml, renders, bypass=()):
        """Conform against renders {name: (tc, frames)} (None: unreadable); return clip -> linked render, warnings."""
        table = {}
        for name, facts in renders.items():
            (self.out / name).touch()
            table[str(self.out / name)] = facts and media.MediaInfo(str(self.out / name), facts[1], Fraction(25), 1920, 1080, facts[0])
        def probe(path):
            if table[path] is None:
                raise media.MediaError('Corrupt QuickTime file')
            return table[path]
        warnings = []
        with patch('conform.fcp7.probe', side_effect=probe):
            result = fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), fcp7.Renders(self.out), bypass, warnings)
        return {k: ci.one('file').value('name') if ci.one('file').all('pathurl') else None
                for k, ci in rows(result).items()}, warnings

    def test_renumbered_render_folder_links_each_clip_to_its_own_render(self):
        # b was inserted after rendering, so x and y (two cuts of a.mov) moved up a
        # number: V1-0002_a is now y's render under x's new name. Only timecode
        # tells them apart; a render of another source is never taken for its number.
        xml = sequence([clipitem('b', 'b.mov', 0, 5000, 10), clipitem('x', 'a.mov', 50, 1000, 10, REMAP_100),
                        clipitem('y', 'a.mov', 100, 1000, 500)], [clipitem('d', 'd.mov', 0, 7000, 10)])
        renders = {'V1-0001_a.mov': (1000, 70), 'V1-0002_a.mov': (1490, 70), 'V2-0001_e.mov': (7000, 100)}
        linked, warnings = self.link(xml, renders)
        self.assertEqual(linked, {(1, 0): None, (1, 50): 'V1-0001_a.mov', (1, 100): 'V1-0002_a.mov', (2, 0): None})
        self.assertEqual(sorted(warnings), sorted([
            'V1-0001_b.mov: no render found; left offline.',
            "V1-0002_a.mov: render V1-0002_a.mov holds none of the clip's frames; matched render V1-0001_a.mov by timecode.",
            'V1-0003_a.mov: matched render V1-0002_a.mov by timecode.',
            'V2-0001_d.mov: no render found; left offline.']))
        # With the speed baked into the renders their timecode proves nothing: x keeps its name's render.
        linked, warnings = self.link(xml, renders, ('retime',))
        self.assertEqual(linked[(1, 50)], 'V1-0002_a.mov')
        self.assertIn('V1-0002_a.mov: render V1-0002_a.mov does not cover the clip; check handles.', warnings)

    def test_several_render_folders(self):
        # The renders, and trims re-rendered later into a second folder; b was rendered into both.
        main, trims = self.out / 'renders', self.out / 'trims'
        main.mkdir(); trims.mkdir()
        xml = sequence([clipitem('x', 'a.mov', 0, 1000, 10), clipitem('y', 'b.mov', 50, 2000, 10),
                        clipitem('z', 'c.mov', 100, 3000, 10)])
        files = {main / 'V1-0001_a.mov': 1000, main / 'V1-0002_b.mov': 2000, trims / 'V1-0002_b.mov': 2000,
                 trims / 'V1-0003_c.mov': 3000}
        for path in files:
            path.touch()
        os.utime(main / 'V1-0002_b.mov', (1, 1))       # the first render; trims holds the newer one
        table = {str(p): media.MediaInfo(str(p), 100, Fraction(25), 1920, 1080, tc) for p, tc in files.items()}
        warnings = []
        with patch('conform.fcp7.probe', side_effect=lambda p: table[p]):
            folders = fcp7.Renders([main, str(trims), main])   # a folder given twice counts once
            result = fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), folders, (), warnings)
        linked = {k: unquote(ci.one('file').value('pathurl')) for k, ci in rows(result).items()}
        self.assertTrue(linked[(1, 0)].endswith('/renders/V1-0001_a.mov'))
        self.assertTrue(linked[(1, 50)].endswith('/trims/V1-0002_b.mov'))
        self.assertTrue(linked[(1, 100)].endswith('/trims/V1-0003_c.mov'))
        self.assertEqual(warnings, ['V1-0002_b.mov: rendered into 2 folders; took the newest, %s.'
                                    % os.path.join('trims', 'V1-0002_b.mov')])

    def test_render_in_two_folders_counts_once_when_matched_by_timecode(self):
        # Renumbered as in the test above, and x's render was also re-rendered into trims.
        main, trims = self.out / 'renders', self.out / 'trims'
        main.mkdir(); trims.mkdir()
        xml = sequence([clipitem('b', 'b.mov', 0, 5000, 10), clipitem('x', 'a.mov', 50, 1000, 10),
                        clipitem('y', 'a.mov', 100, 1000, 500)])
        files = {main / 'V1-0001_a.mov': 1000, main / 'V1-0002_a.mov': 1490, trims / 'V1-0001_a.mov': 1000}
        for path in files:
            path.touch()
        os.utime(main / 'V1-0001_a.mov', (1, 1))
        table = {str(p): media.MediaInfo(str(p), 70, Fraction(25), 1920, 1080, tc) for p, tc in files.items()}
        warnings = []
        with patch('conform.fcp7.probe', side_effect=lambda p: table[p]):
            result = fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), fcp7.Renders([main, trims]), (), warnings)
        linked = {k: unquote(ci.one('file').value('pathurl')) if ci.one('file').all('pathurl') else None
                  for k, ci in rows(result).items()}
        self.assertTrue(linked[(1, 50)].endswith('/trims/V1-0001_a.mov'))
        self.assertTrue(linked[(1, 100)].endswith('/renders/V1-0002_a.mov'))
        self.assertIn('V1-0002_a.mov: rendered into 2 folders; took the newest, %s.'
                      % os.path.join('trims', 'V1-0001_a.mov'), warnings)

    def test_several_different_renders_covering_a_clip_are_named_as_such(self):
        xml = sequence([clipitem('x', 'a.mov', 0, 1000, 10)])
        linked, warnings = self.link(xml, {'V1-0007_a.mov': (1000, 100), 'V1-0008_a.mov': (1000, 100)})
        self.assertIsNone(linked[(1, 0)])
        self.assertEqual(warnings, ['V1-0001_a.mov: 2 different renders cover the clip; left offline.'])

    def test_render_short_after_a_trim_stays_the_clips_own(self):
        # x was trimmed past its handles after rendering; the V2 layer of the same
        # take covers it, but carries another grade.
        xml = sequence([clipitem('x', 'a.mov', 0, 1000, 10)], [clipitem('y', 'a.mov', 0, 1000, 0)])
        linked, warnings = self.link(xml, {'V1-0001_a.mov': (1012, 60), 'V2-0001_a.mov': (990, 100)})
        self.assertEqual(linked[(1, 0)], 'V1-0001_a.mov')
        self.assertIn('V1-0001_a.mov: render V1-0001_a.mov does not cover the clip; check handles.', warnings)

    def test_decomposed_unicode_render_names_match(self):
        # macOS network shares list й, ё, í decomposed; Resolve's XML has them composed.
        nfd = unicodedata.normalize('NFD', 'V1-0001_белый.mov')
        self.assertNotEqual(nfd, 'V1-0001_белый.mov')
        xml = sequence([clipitem('x', 'белый.mov', 0, 1000, 10)])
        linked, warnings = self.link(xml, {nfd: (1000, 70)})
        self.assertEqual(linked[(1, 0)], nfd)
        self.assertFalse(warnings)

    def test_unreadable_renders_found_by_timecode_are_named(self):
        xml = sequence([clipitem('x', 'a.mov', 0, 1000, 10)])
        linked, warnings = self.link(xml, {'V1-0009_a.mov': None})
        self.assertIsNone(linked[(1, 0)])
        self.assertEqual(warnings, ['V1-0001_a.mov: no render found (unreadable: V1-0009_a.mov); left offline.'])

    def test_ramp_without_resolve_ranges_keeps_the_render_with_its_name(self):
        # Resolve's exported ramp graph is wrong, so without the API's source range
        # the timecode cannot prove that another render is the clip's own.
        table = fake_renders(self.out)
        warnings = []
        with patch('conform.fcp7.probe', side_effect=lambda p: table[p]):
            fcp7.conform(SOURCE, self.names, fcp7.Renders(self.out), (), warnings)
        self.assertFalse([w for w in warnings if 'matched render' in w])

    def test_still_takes_only_its_own_one_frame_render_by_its_number(self):
        # Renders made before the edit changed: another clip's render now has the still's number.
        for render, frames in (('V1-0001_c.mov', 300), ('V1-0001_title.00000000.mov', 1)):
            warning = 'V1-0001_1.png: render %s has this clip number but is not this still; left offline.' % render
            with self.subTest(render=render), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / render).touch()
                info = media.MediaInfo(str(Path(temp) / render), frames, Fraction(25), 3840, 2160, 0)
                warnings = []
                with patch('conform.fcp7.probe', return_value=info):
                    result = fcp7.conform(self.STILL, plan(fcp7.clips(self.STILL, []), 't'), fcp7.Renders(temp), (), warnings)
                self.assertFalse(rows(result)[(1, 0)].one('file').all('pathurl'))
                self.assertEqual(warnings, [warning])

    def test_still_with_an_unreadable_render_is_not_matched_by_timecode(self):
        # A still covers no frames, so any render of a source ending in "_1" starting at 0 would do.
        xml = self.STILL.replace(b'</track>', b'</track><track>' + clipitem('v', 'intro_1.mov', 0, 0, 0).encode() + b'</track>')
        (self.out / 'V1-0001_0.mov').touch()
        (self.out / 'V2-0001_intro_1.mov').touch()
        intro = media.MediaInfo(str(self.out / 'V2-0001_intro_1.mov'), 100, Fraction(25), 1920, 1080, 0)
        def probe(path):
            if path.endswith('_0.mov'):
                raise media.MediaError('Corrupt QuickTime file')
            return intro
        warnings = []
        with patch('conform.fcp7.probe', side_effect=probe):
            result = fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), fcp7.Renders(self.out), (), warnings)
        self.assertFalse(rows(result)[(1, 0)].one('file').all('pathurl'))
        self.assertEqual(warnings, ['V1-0001_1.png: render V1-0001_0.mov unreadable (Corrupt QuickTime file).'])

    def test_any_probe_failure_is_an_unreadable_render(self):
        (self.out / 'V1-0001_1.mov').touch()
        warnings = []
        with patch('conform.fcp7.probe', side_effect=IndexError('list index out of range')):
            result = fcp7.conform(self.STILL, plan(fcp7.clips(self.STILL, []), 't'), fcp7.Renders(self.out), (), warnings)
        self.assertFalse(rows(result)[(1, 0)].one('file').all('pathurl'))
        self.assertIn('V1-0001_1.png: render V1-0001_1.mov unreadable (list index out of range).', warnings)

    def test_render_handles_must_cover_the_transition(self):
        # A centred 20-frame dissolve on the cut at 50: a plays 10 frames past its
        # out point, b starts 10 frames before its in point, out of their handles.
        def item(uid, source, start, end, first, tc):
            return ('<clipitem id="%s"><name>%s</name><duration>3000</duration><start>%d</start><end>%d</end><in>%d</in><out>%d</out>'
                    '<file id="f%s"><name>%s</name><pathurl>file:///src/%s</pathurl><duration>3000</duration><rate><timebase>25</timebase></rate>'
                    '<timecode><rate><timebase>25</timebase></rate><string>x</string><frame>%d</frame></timecode></file></clipitem>'
                    % (uid, source, start, end, first, first + 50, uid, source, source, tc))
        xml = sequence([item('a', 'a.mov', 0, -1, 100, 1000),
                        '<transitionitem><start>40</start><end>60</end><alignment>center</alignment></transitionitem>',
                        item('b', 'b.mov', -1, 100, 100, 5000)])
        self.assertEqual([(i.lead, i.trail) for i in fcp7.read(xml, [])[1]], [(0, 10), (10, 0)])
        for handles, expected in ((10, []), (5, [
                'V1-0001_a.mov: the transition needs 0 frames of handle before and 10 after; the render has 5 and 5.',
                'V1-0002_b.mov: the transition needs 10 frames of handle before and 0 after; the render has 5 and 5.'])):
            with self.subTest(handles=handles), tempfile.TemporaryDirectory() as temp:
                table = {}
                for name, tc in (('V1-0001_a.mov', 1100), ('V1-0002_b.mov', 5100)):
                    path = str(Path(temp) / name); Path(path).touch()
                    table[path] = media.MediaInfo(path, 50 + 2 * handles, Fraction(25), 1920, 1080, tc - handles)
                warnings = []
                with patch('conform.fcp7.probe', side_effect=lambda p: table[p]):
                    fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), fcp7.Renders(temp), (), warnings)
                self.assertEqual(warnings, expected)

    def test_rebuilt_ramp_keeps_frame_blending(self):
        ci = rows(SOURCE)[(1, 517)]
        chunk = SOURCE[ci.start:ci.end]
        at = chunk.index(b'<value>FALSE</value>', chunk.index(b'<parameterid>frameblending</parameterid>'))
        chunk = chunk[:at] + b'<value>TRUE</value>' + chunk[at + len(b'<value>FALSE</value>'):]
        blended = SOURCE[:ci.start] + chunk + SOURCE[ci.end:]
        table = fake_renders(self.out)
        with patch('conform.fcp7.probe', side_effect=lambda p: table[p]):
            names = plan(fcp7.clips(blended, []), 'full-mv')
            items = fcp7.read(blended, [])[1]
            result = fcp7.conform(blended, names, fcp7.Renders(self.out), (), [], source_ranges(items))
        remap = next(f for f in rows(result)[(1, 517)].all('filter') if fcp7._effect(f) == 'Time Remap')
        params = {p.value('parameterid'): p.value('value') for p in remap.one('effect').all('parameter') if p.all('value')}
        self.assertEqual((params['variablespeed'], params['frameblending']), ('0', 'TRUE'))

    def test_what_resolve_interpolates_and_what_the_drt_must_import(self):
        retimed = fcp7.retimed(SOURCE)
        self.assertIn((1, 2074), retimed)          # 50p at 50% (Time Remap)
        self.assertIn((1, 4608), retimed)          # 30p on 25p, no Time Remap
        self.assertIn((1, 3237), retimed)          # 50p on 25p, no Time Remap
        self.assertIn((1, 517), retimed)           # a ramp
        self.assertNotIn((1, 0), retimed)
        self.assertNotIn((2, 0), retimed)          # the logo: a still at 24 fps holds one frame
        self.assertEqual(len(retimed), 62)
        self.assertNotIn((1, 0), fcp7.retimed(self.STILL))
        song = self.out / 'song.wav'
        song.write_bytes(b'')
        xml = (b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track/></video>'
               b'<audio><track><clipitem id="m"><name>song</name><start>0</start><end>10</end><in>0</in><out>10</out>'
               b'<file id="s"><name>song.wav</name><pathurl>' + song.resolve().as_uri().encode() + b'</pathurl></file></clipitem>'
               b'<clipitem id="g"><name>gone</name><start>10</start><end>20</end><in>0</in><out>10</out>'
               b'<file id="g"><name>gone.wav</name><pathurl>file:///nowhere/gone.wav</pathurl></file></clipitem>'
               b'<clipitem id="m2"><name>song</name><start>20</start><end>30</end><in>0</in><out>10</out><file id="s"/></clipitem>'
               b'</track></audio></media></sequence></xmeml>')
        self.assertEqual(fcp7.audio_media(xml), ([str(song.resolve())], 1))
        self.assertEqual(fcp7.sequence_size(fcp7.with_size(SOURCE, 3996, 2160)), (3996, 2160))
        warnings = []
        fcp7.read(SOURCE, warnings)
        self.assertIn('1 titles: FCP7 XML carries no text or styling, so they arrive empty or as basic text.', warnings)

    def test_composite_bypass_keeps_filters_it_cannot_name(self):
        xml = self.STILL.replace(b'</clipitem>', b'<filter><enabled>TRUE</enabled></filter></clipitem>')
        result = fcp7.conform(xml, plan(fcp7.clips(xml, []), 't'), None, ('composite',))
        self.assertEqual(len(rows(result)[(1, 0)].all('filter')), 2)

    def test_clips_next_to_a_transition_are_conformed(self):
        xml = (b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track>'
               b'<clipitem id="a"><name>a.mov</name><duration>100</duration><start>0</start><end>-1</end><in>0</in><out>60</out>'
               b'<file id="fa"><name>a.mov</name><pathurl>file:///src/a.mov</pathurl><duration>100</duration><rate><timebase>25</timebase></rate></file></clipitem>'
               b'<transitionitem><start>40</start><end>60</end><alignment>center</alignment></transitionitem>'
               b'<clipitem id="b"><name>b.mov</name><duration>100</duration><start>-1</start><end>100</end><in>10</in><out>70</out>'
               b'<file id="fb"><name>b.mov</name><pathurl>file:///src/b.mov</pathurl><duration>100</duration><rate><timebase>25</timebase></rate></file></clipitem>'
               b'</track></video></media></sequence></xmeml>')
        warnings = []
        clips = fcp7.clips(xml, warnings)
        self.assertEqual([(c.start, c.duration) for c in clips], [(0, 50), (50, 50)])  # cut in the middle
        self.assertFalse(warnings)
        result = fcp7.conform(xml, plan(clips, 't'))
        self.assertNotIn(b'file:///src/', result)

    def test_composite_mode_has_its_own_bypass(self):
        mode = lambda data: {ci.one('compositemode').text for ci in rows(data).values() if ci.all('compositemode')}
        blended = SOURCE.replace(b'<compositemode>normal</compositemode>', b'<compositemode>screen</compositemode>')
        names = plan(fcp7.clips(blended, []), 'full-mv')
        self.assertEqual(mode(fcp7.conform(blended, names, None, ('opacity',))), {'screen'})
        self.assertEqual(mode(fcp7.conform(blended, names, None, ('composite',))) - {'screen'}, {'normal'})

    def test_bypass_removes_baked_effects(self):
        result, warnings = self.conform(bypass=('transforms', 'crop', 'opacity', 'composite'))
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
        def make(xml, drt, renders, sounds=()):
            seen.append((xml.read_bytes(), renders)); drt.write_bytes(b'drt')
        files, _, _ = build_bundle(self.out / 'in.xml', self.out, 'full-mv', export_drt=True, export_xml=False, make_drt=make)
        self.assertEqual(sorted(p.name for p in files if p.suffix != '.txt'), ['full-mv.csv', 'full-mv.drt'])
        self.assertNotIn(b'<pathurl>file:///Volumes/Media/Project/Sources/A003C014', seen[0][0])
        self.assertEqual(seen[0][1], [])  # offline: nothing to import

    def test_renders_field_holds_several_folders(self):
        from conform.gui import folders
        self.assertEqual(folders(' /a/renders ; /a/trims;; '), ['/a/renders', '/a/trims'])
        self.assertEqual(folders('/a/renders'), ['/a/renders'])
        self.assertEqual(folders('  '), [])

    def test_no_formats_rejected(self):
        with self.assertRaises(ValueError):
            build_bundle(self.out / 'in.xml', self.out, 't', export_drt=False, export_xml=False, export_csv=False)


class PrefsTests(unittest.TestCase):
    def test_settings_round_trip_and_bad_files(self):
        from conform import prefs
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'Conform Export' / 'settings.json'
            with patch('conform.prefs.path', return_value=target):
                self.assertEqual(prefs.load(), {})
                prefs.save({'output': '/x', 'bypass': ['crop']})
                self.assertEqual(prefs.load(), {'output': '/x', 'bypass': ['crop']})
                target.write_text('not json')
                self.assertEqual(prefs.load(), {})
                target.write_text(json.dumps({'output': '/x', 'csv': False, 'drt': 'yes', 'bypass': 'crop', 'zoom': 2}))
                self.assertEqual(prefs.load(), {'output': '/x', 'csv': False})   # wrong types fall back to defaults


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

    def test_key_is_the_name_up_to_the_source_only_when_it_holds_the_number(self):
        clips = [Clip('a', 2, 0, 20, 'x.mov', 'x.mov')]
        for template, key in (('{TRACK}-{INDEX:4}_{SOURCE}', 'V2-0001_'), ('{INDEX}_{SOURCE}_{TIMELINE}', '0001_'),
                              ('{TRACK}_{SOURCE}_{INDEX}', ''), ('{SOURCE}-{INDEX}', ''),
                              ('{TIMELINE}_{INDEX}_{SOURCE}', 'Reel 1_2_0001_'), (' {TRACK}-{INDEX}_{SOURCE}', 'V2-0001_'),
                              ('{TRACK}-{INDEX:1} {SOURCE}', 'V2-1 ')):   # 'V2-1' would also match V2-12
            with self.subTest(template=template):
                named = plan(clips, 'Reel 1/2', Settings(template=template))[0]
                self.assertEqual(named.key, key)
                self.assertTrue(named.new_name.startswith(key))   # cleaned like the name it must match

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

    def test_ffprobe_takes_the_length_from_the_headers(self):
        def answer(streams, timecode=None, container='mxf'):
            out = {'streams': streams, 'format': {'format_name': container, 'tags': {'timecode': timecode} if timecode else {}}}
            return subprocess.CompletedProcess([], 0, json.dumps(out), '')
        mxf = {'width': 1920, 'height': 1080, 'r_frame_rate': '25/1', 'time_base': '1/25', 'duration_ts': 300}
        with patch('conform.media._ffprobe_path', return_value='ffprobe'):
            with patch('conform.media.subprocess.run', return_value=answer([mxf], '01:00:00:00')) as run:
                info = media.probe('r.mxf')
            self.assertEqual((info.frames, info.rate, info.tc_start), (300, 25, 90000))
            self.assertEqual(run.call_count, 1)                     # no full read of the file
            for streams, container in (([dict(mxf, duration_ts=None)], 'mxf'), ([mxf], 'mpeg')):
                # no length in the header, or a duration that is only an estimate: count
                counted = [answer(streams, None, container), answer([dict(mxf, nb_read_packets='299')], None, container)]
                with self.subTest(container=container), patch('conform.media.subprocess.run', side_effect=counted) as run:
                    self.assertEqual(media.probe('r.mpg').frames, 299)
                    self.assertIn('-count_packets', run.call_args[0][0])
            for failure in (answer([]), subprocess.TimeoutExpired('ffprobe', 60), answer([dict(mxf, r_frame_rate='0/0')])):
                with self.subTest(failure=failure), self.assertRaises(media.MediaError):
                    with patch('conform.media.subprocess.run', side_effect=[failure]):
                        media.probe('audio.mxf')

    def test_file_urls_become_paths_on_windows_too(self):
        import nturl2path
        from pathlib import PureWindowsPath
        with patch('urllib.request.url2pathname', nturl2path.url2pathname):
            for path in (r'\\server\raid\renders\V1-0001_a.mov', r'C:\renders\V1-0001_a.mov'):
                with self.subTest(path=path):
                    self.assertEqual(fcp7._path(PureWindowsPath(path).as_uri()), path)
        self.assertEqual(fcp7._path('file://localhost/Volumes/raid/%D0%B9.mov'), '/Volumes/raid/\u0439.mov')

    def test_drop_frame_timecode_round_trip(self):
        for tc in ('01:00:00;00', '00:10:00;00', '00:01:00;02', '23:59:59;29'):
            self.assertEqual(media.frames_to_tc(media.tc_to_frames(tc, 30, True), 30, True), tc)


class Forbidden(BaseException):
    """A broken safety rule; BaseException so that no `except Exception` hides it."""


CLIP_DEFAULTS = {'ZoomX': 2.0, 'FlipX': True, 'Opacity': 100.0, 'RetimeProcess': 0, 'MotionEstimation': 0,
                 'Scaling': 0, 'ResizeFilter': 0, 'CompositeMode': 0}


class FakeItem:
    def __init__(self, track, clip, props=None, owner=None):
        self.track, self.c, self.owner = track, clip, owner
        self.props = dict(CLIP_DEFAULTS, **(props or {}))
    def GetUniqueId(self): return self.c.uid
    def GetName(self): return self.c.old_name
    def GetStart(self): return self.c.start + 90000
    def GetDuration(self): return self.c.duration
    def GetSourceStartFrame(self): return 0
    def GetSourceEndFrame(self): return self.c.duration
    def GetProperty(self, name=None):
        return dict(self.props) if name is None else self.props.get(name)
    def SetProperty(self, name, value):
        if self.owner is None:
            raise Forbidden("the user's timeline must never change")
        self.owner.written()
        self.props[name] = value; return True


class FakeFolder:
    def __init__(self, name): self.name, self.clips = name, []
    def GetClipList(self): return list(self.clips)


class FakePool:
    def __init__(self, project):
        self.project, self.root, self.folders, self.log, self.imported = project, FakeFolder('Master'), [], [], None
        self.current = self.root
    def GetRootFolder(self): return self.root
    def GetCurrentFolder(self): return self.current
    def SetCurrentFolder(self, f): self.current = f; return True
    def AddSubFolder(self, parent, name):
        f = FakeFolder(name); self.folders.append(f); return f
    def ImportMedia(self, paths):
        self.log.append(('media', list(paths))); self.current.clips += paths; return ['clip'] * len(paths)
    def ImportTimelineFromFile(self, path, options):
        self.log.append(('import', options)); self.current.clips.append('render clip')
        self.imported = FakeImported(self.project, options['timelineName'])
        self.project.timelines.append(self.imported)
        # Resolve 21 gives the project the resolution of an imported XML.
        try:
            width, height = fcp7.sequence_size(Path(path).read_bytes())
            self.project.settings.update(timelineResolutionWidth=str(width), timelineResolutionHeight=str(height))
        except ValueError:
            pass    # an XML without a sequence format
        self.project.settings.update(self.project.import_changes)
        return self.imported
    def DeleteTimelines(self, timelines):
        for t in timelines:
            if self.project.current is t:
                raise Forbidden('a timeline was deleted while current')
            t.deleted = True; self.project.timelines.remove(t)
        self.log.append('delete timeline'); return True
    def DeleteClips(self, c): self.log.append(('delete clips', len(c))); return True
    def DeleteFolders(self, f): self.folders = [x for x in self.folders if x not in f]; return True


class FakeImported:
    """Resolve's timeline from the XML: the project's settings, default clip properties."""
    def __init__(self, project, name):
        self.project, self.name, self.deleted, self.start, self.names = project, name, False, '00:00:00:00', {}
        self.settings = {'useCustomSettings': '0', 'timelineResolutionWidth': '3840', 'timelineResolutionHeight': '2160',
                         'timelinePixelAspectRatio': 'square', 'timelineFrameRate': '25'}
        self.items = {t: [FakeItem(t, c, owner=self) for c in fcp7.clips(SOURCE, []) if c.track == t] for t in (1, 2)}
    def written(self):
        if self.project.current is not self or self.deleted:
            raise Forbidden('the temporary timeline was written while not current')
        self.project.writes += 1
        if self.project.switch_away_after == self.project.writes:
            self.project.current = self.project.timeline     # the user clicks their own timeline
    def item(self, key):
        return next(i for i in self.items[key[0]] if i.c.start == key[1])
    def GetUniqueId(self): return 'imported'
    def GetName(self): return self.name
    def GetStartFrame(self): return 90000
    def GetTrackCount(self, kind): return 2
    def GetItemListInTrack(self, kind, track): return self.items[track] if kind == 'video' else []
    def GetSetting(self, key=None): return self.settings.get(key, '')
    def SetSetting(self, key, value):
        self.written()
        if key in self.project.refuse_settings or (key != 'useCustomSettings' and self.settings['useCustomSettings'] != '1'):
            return False    # "useCustomSettings needs to be set to change timeline properties"
        self.settings[key] = value; return True
    def GetStartTimecode(self): return self.start
    def SetStartTimecode(self, tc): self.written(); self.start = tc; return True
    def GetTrackName(self, kind, index): return self.names.get((kind, index), '%s %d' % (kind.title(), index))
    def SetTrackName(self, kind, index, name): self.written(); self.names[(kind, index)] = name; return True
    def Export(self, path, kind): Path(path).write_bytes(b'drt'); return True


class FakeTimeline:
    """The user's timeline: read-only for the tool."""
    def __init__(self, settings=None, props=None, start='00:00:00:00', names=None):
        self.clips = fcp7.clips(SOURCE, [])
        self.settings = dict({'useCustomSettings': '0', 'timelineResolutionWidth': '3840',
                              'timelineResolutionHeight': '1920', 'timelineFrameRate': '25'}, **(settings or {}))
        self.props, self.start, self.names = props or {}, start, names or {}
    def GetUniqueId(self): return 'timeline'
    def GetName(self): return 'full-mv'
    def GetStartFrame(self): return 90000
    def GetTrackCount(self, kind): return 2
    def GetItemListInTrack(self, kind, track):
        return [FakeItem(track, c, self.props.get((track, c.start))) for c in self.clips if c.track == track] if kind == 'video' else []
    def GetSetting(self, key=None): return self.settings.get(key, '')
    def GetStartTimecode(self): return self.start
    def GetTrackName(self, kind, index): return self.names.get((kind, index), '%s %d' % (kind.title(), index))
    def Export(self, path, kind):
        if kind != 'xml': return False
        Path(path).write_bytes(SOURCE); return True


class FakeProject:
    def __init__(self, project=None, refuse_switch=False, refuse_back=False, refuse_settings=(), switch_away_after=None,
                 import_changes=None, **timeline):
        self.timeline = FakeTimeline(**timeline)
        self.current, self.timelines, self.refuse_switch = self.timeline, [self.timeline], refuse_switch
        self.refuse_back, self.refuse_settings, self.writes = refuse_back, set(refuse_settings), 0
        self.switch_away_after, self.import_changes = switch_away_after, import_changes or {}
        self.settings = dict({'timelineResolutionWidth': '3840', 'timelineResolutionHeight': '2160',
                              'timelineFrameRate': '25'}, **(project or {}))
        self.pool = FakePool(self)
    def GetName(self): return 'project'
    def GetCurrentTimeline(self): return self.current
    def SetCurrentTimeline(self, t):
        if (self.refuse_switch and t is not self.timeline) or (self.refuse_back and t is self.timeline):
            return False
        self.current = t; return True
    def GetSetting(self, key): return self.settings.get(key, '')
    def GetTimelineCount(self): return len(self.timelines)
    def GetTimelineByIndex(self, index): return self.timelines[index - 1]
    def GetMediaPool(self): return self.pool


class FakeResolve:
    EXPORT_FCP_7_XML, EXPORT_DRT = 'xml', 'drt'
    # As Resolve 21 hands them over: floats.
    RETIME_USE_PROJECT, RETIME_NEAREST, RETIME_FRAME_BLEND, RETIME_OPTICAL_FLOW = 0.0, 1.0, 2.0, 3.0
    MOTION_EST_SPEED_WARP_BETTER, MOTION_EST_METAL = 5.0, 7.0
    def __init__(self, **project): self.project = FakeProject(**project)
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

    def test_preview_marks_exactly_what_the_export_leaves_offline(self):
        for bypass in ((), ('retime',)):    # retime bypass links the ramps the API ranges would otherwise be needed for
            with self.subTest(bypass=bypass), tempfile.TemporaryDirectory() as temp:
                resolve = FakeResolve()
                folder, out = Path(temp) / 'renders', Path(temp) / 'out'
                folder.mkdir()
                table = fake_renders(folder)
                twin = folder / 'V1-0001_A003C014_260817_R14E.mxf'     # a second render with the first clip's name
                twin.touch()
                table[str(twin)] = table[str(folder / 'V1-0001_A003C014_260817_R14E.mov')]
                broken = sorted(table)[5]                                # a render whose header cannot be read
                def probe(path):
                    if path == broken:
                        raise media.MediaError('Corrupt QuickTime file')
                    return table[path]
                with patch('conform.fcp7.probe', side_effect=probe):
                    names, warnings, missing = preview(resolve, Settings(), str(folder), bypass)
                    files, _, _ = export_current(resolve, out, renders=str(folder), bypass=bypass)
                exported = next(p for p in files if p.suffix == '.xml').read_bytes()
                offline = {n.clip.uid for n in names if n.clip.media} - fcp7.linked_clips(exported)
                self.assertEqual(missing, offline)
                by_name = {Path(n.new_name).stem: n.clip.uid for n in names}
                self.assertIn(by_name['V1-0001_A003C014_260817_R14E'], missing)
                self.assertIn(by_name[Path(broken).stem], missing)
                self.assertTrue(any('several renders' in w for w in warnings))
                # twin, broken and the logo, which has no render; without the bypass, ramps need Resolve's ranges
                self.assertEqual(len(missing), 3 if bypass else 3 + RAMPS)

    def export(self, resolve, bypass=()):
        with tempfile.TemporaryDirectory() as temp:
            files, names, warnings = export_current(resolve, temp, bypass=bypass)
            xml = next(p for p in files if p.suffix == '.xml').read_bytes()
        return resolve.project.pool.imported, xml, warnings

    def test_drt_gets_back_what_fcp7_xml_cannot_carry(self):
        retimed = sorted(fcp7.retimed(SOURCE))
        own, on_project, plain = retimed[0], (1, 4608), (1, 0)     # (1, 4608): 30p on 25p, no Time Remap
        self.assertNotIn(plain, retimed)
        resolve = FakeResolve(
            settings={'useCustomSettings': '1', 'timelineResolutionWidth': '3996', 'timelineResolutionHeight': '2160',
                      'timelineInputResMismatchBehavior': 'scaleToCrop', 'timelineOutputResMatchTimelineRes': '1'},
            props={own: {'RetimeProcess': 3, 'MotionEstimation': 6}, plain: {'Scaling': 3, 'CompositeMode': 11}},
            start='01:00:00:00', names={('video', 1): 'PLATES'},
            project={'imageRetimeInterpolation': 'Optical Flow', 'imageMotionEstimationMode': 'Speed Warp Better'})
        imported, xml, warnings = self.export(resolve)
        self.assertEqual(imported.name, 'full-mv conform')
        self.assertEqual({k: imported.settings[k] for k in ('useCustomSettings', 'timelineResolutionWidth',
                                                             'timelineResolutionHeight', 'timelineInputResMismatchBehavior')},
                         {'useCustomSettings': '1', 'timelineResolutionWidth': '3996', 'timelineResolutionHeight': '2160',
                          'timelineInputResMismatchBehavior': 'scaleToCrop'})
        self.assertEqual((imported.start, imported.GetTrackName('video', 1)), ('01:00:00:00', 'PLATES'))
        props = lambda key, *keys: [imported.item(key).props[k] for k in keys]
        self.assertEqual(props(own, 'RetimeProcess', 'MotionEstimation'), [3, 6])           # its own setting
        self.assertEqual(props(on_project, 'RetimeProcess', 'MotionEstimation'), [3, 5])    # the project's, made explicit
        self.assertEqual(props(plain, 'RetimeProcess', 'Scaling', 'CompositeMode'), [0, 3, 11])
        self.assertIs(resolve.project.current, resolve.project.timeline)                     # the user's timeline again
        self.assertEqual(resolve.project.timelines, [resolve.project.timeline])
        # The XML: the real resolution, and a word about what it cannot carry.
        self.assertEqual(fcp7.sequence_size(xml), (3996, 2160))
        self.assertIn("Resolve's XML gave the timeline as 3840x1920; set to its real 3996x2160.", warnings)
        self.assertEqual(resolve.project.settings['timelineResolutionWidth'], '3840')   # the import left the project alone
        self.assertFalse([w for w in warnings if 'changed the project' in w])
        self.assertIn('62 retimed clips use Optical Flow; FCP7 XML cannot say so (the DRT keeps it).', warnings)
        self.assertEqual([w for w in warnings if w.startswith('DRT:')],
                         ['DRT: 4 audio files the timeline plays are not on disk; that audio is offline.'])
        self.assertEqual(props((2, 0), 'RetimeProcess'), [0])      # the logo still: nothing to interpolate

    def test_drt_bin_gets_the_audio_too(self):
        resolve = FakeResolve()
        with tempfile.TemporaryDirectory() as temp:
            song = Path(temp) / 'song.wav'
            song.write_bytes(b'')
            xml = Path(temp) / 'conformed.xml'
            xml.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media>'
                            b'<video><track/></video><audio><track><clipitem id="m"><name>song</name><start>0</start><end>10</end>'
                            b'<in>0</in><out>10</out><file id="s"><name>song.wav</name><pathurl>' + song.resolve().as_uri().encode() +
                            b'</pathurl></file></clipitem></track></audio></media></sequence></xmeml>')
            drt_maker(resolve, resolve.project, resolve.project.timeline)(
                xml, Path(temp) / 'out.drt', ['/r/V1-0001_a.mov'], [str(song.resolve())])
        media_calls = [entry[1] for entry in resolve.project.pool.log if entry[0] == 'media']
        self.assertEqual(media_calls, [['/r/V1-0001_a.mov'], [str(song.resolve())]])

    def test_drt_leaves_out_the_camera_files_of_clips_without_a_render(self):
        # Resolve links a clip without a render to its camera file in the bin (by
        # timecode), so that file stays out even though its sound is wanted.
        with tempfile.TemporaryDirectory() as temp:
            camera, song = Path(temp) / 'a.mov', Path(temp) / 'song.wav'
            camera.write_bytes(b''); song.write_bytes(b'')
            uri = lambda p: p.resolve().as_uri().encode()
            xml = Path(temp) / 'in.xml'
            xml.write_bytes(
                b'<?xml version="1.0" encoding="UTF-8"?><xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track>'
                b'<clipitem id="v"><name>a.mov</name><duration>100</duration><start>0</start><end>50</end><in>10</in><out>60</out>'
                b'<file id="fa"><name>a.mov</name><pathurl>' + uri(camera) + b'</pathurl><duration>100</duration>'
                b'<rate><timebase>25</timebase></rate><media><video/><audio/></media></file></clipitem></track></video>'
                b'<audio><track><clipitem id="x"><name>a.mov</name><start>0</start><end>50</end><in>10</in><out>60</out>'
                b'<file id="fa"/></clipitem><clipitem id="m"><name>song</name><start>0</start><end>50</end><in>0</in><out>50</out>'
                b'<file id="s"><name>song.wav</name><pathurl>' + uri(song) + b'</pathurl><media><audio/></media></file>'
                b'</clipitem></track></audio></media></sequence></xmeml>')
            seen = []
            def make(xml_file, drt, renders, sounds=()):
                seen.append(list(sounds)); drt.write_bytes(b'drt')
            _, _, warnings = build_bundle(xml, Path(temp) / 'out', 't', export_drt=True, make_drt=make)
        self.assertEqual(seen, [[str(song.resolve())]])
        self.assertIn('DRT: the sound of clips without a render stays offline (1 camera files), or their picture would '
                      'link back to the camera originals.', warnings)

    def test_drt_respects_bypass_and_says_what_it_could_not_restore(self):
        retimed = sorted(fcp7.retimed(SOURCE))
        resolve = FakeResolve(props={retimed[0]: {'RetimeProcess': 3}, (1, 0): {'CompositeMode': 11}},
                              project={'imageRetimeInterpolation': 'Optical Flow'})
        imported, _, warnings = self.export(resolve, ('retime', 'composite'))
        self.assertEqual(imported.item(retimed[0]).props['RetimeProcess'], 0)   # speed baked into the render
        self.assertEqual(imported.item((1, 0)).props['CompositeMode'], 0)
        resolve = FakeResolve(project={'imageRetimeInterpolation': 'something new'})
        _, _, warnings = self.export(resolve)
        self.assertIn("DRT: the project's Frame Interpolation (something new) was not recognised; %d retimed clips "
                      "follow the project that opens the DRT for it." % len(retimed), warnings)
        resolve = FakeResolve(refuse_switch=True, settings={'useCustomSettings': '1', 'timelineResolutionWidth': '3996'})
        imported, _, warnings = self.export(resolve)
        self.assertIn('DRT: the temporary timeline could not be made current, so its settings were not restored.', warnings)
        self.assertEqual(imported.settings['timelineResolutionWidth'], '3840')

    def test_restoring_stops_when_the_user_switches_timelines(self):
        retimed = sorted(fcp7.retimed(SOURCE))
        # Timeline writes come first (useCustomSettings, height, start timecode), then the clips.
        for after in (1, 3, 5):      # writes made before the user clicks their own timeline
            with self.subTest(after=after):
                resolve = FakeResolve(switch_away_after=after, start='01:00:00:00',
                                      props={key: {'RetimeProcess': 3} for key in retimed})
                imported, _, warnings = self.export(resolve)    # Forbidden if any write came after the click
                changed = [key for key in retimed if imported.item(key).props['RetimeProcess'] == 3]
                self.assertEqual(len(changed), max(0, after - 3))
        self.assertIn('DRT: you switched timelines while the DRT was being built, so the rest of its settings were not '
                      'restored; export again to get them.', warnings)
        self.assertIn('62 retimed clips use Optical Flow; FCP7 XML cannot say so.', warnings)   # no promise kept

    def test_drt_says_what_went_wrong(self):
        resolve = FakeResolve(settings={'useCustomSettings': '1', 'timelineResolutionWidth': '3996',
                                        'timelineInputResMismatchBehavior': 'scaleToCrop'},
                              refuse_settings={'timelineInputResMismatchBehavior'},
                              import_changes={'timelineResolutionWidth': '1920'})
        _, _, warnings = self.export(resolve)
        self.assertIn('DRT: could not restore timelineInputResMismatchBehavior.', warnings)
        self.assertIn("Resolve changed the project's settings while building the DRT (timelineResolutionWidth 3840 -> "
                      "1920); check Project Settings.", warnings)
        resolve = FakeResolve(refuse_back=True)
        imported, _, warnings = self.export(resolve)
        self.assertIn(imported, resolve.project.timelines)    # never deleted while current
        self.assertIn("DRT: Resolve did not switch back to your timeline, so the temporary timeline 'full-mv conform' "
                      "and the bin 'Conform Export (temp)' were left in the project; delete them by hand.", warnings)
        with patch('conform.exporter.RESTORE_DRT', False):
            resolve = FakeResolve(settings={'useCustomSettings': '1', 'timelineResolutionWidth': '3996'},
                                  project={'imageRetimeInterpolation': 'opticalFlow'})
            imported, _, warnings = self.export(resolve)
        self.assertEqual(imported.settings['timelineResolutionWidth'], '3840')
        self.assertIn('62 retimed clips use Optical Flow; FCP7 XML cannot say so.', warnings)

    def test_project_retime_settings_map_to_clip_constants(self):
        from conform.exporter import project_retime
        resolve = FakeResolve()
        for process, motion, expected in (('opticalFlow', 'speedWarpBetter', (3, 5)), ('opticalFlow', 'speedWarpMetal', (3, 7)),
                                          ('frameBlend', '', (2, None)), ('nearest', 'enhancedFaster', (1, None))):
            with self.subTest(process=process, motion=motion):
                found = {'project': {'imageRetimeInterpolation': process, 'imageMotionEstimationMode': motion}}
                self.assertEqual(project_retime(resolve, found), expected)

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
