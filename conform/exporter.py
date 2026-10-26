"""Resolve adapter and export writer: <timeline>.xml|.drt|.csv in the chosen folder."""
from pathlib import Path
import csv
import gc
import os
import shutil
import tempfile
import time
from . import fcp7
from .media import tc_to_frames
from .naming import Settings, plan, safe

TEMP_BIN = 'Conform Export (temp)'
# FCP7 XML has no place for these, and Resolve's XML import gives a new timeline
# the project's settings, so the DRT's timeline gets them back from the user's
# timeline. Output keys follow the timeline unless that is switched off.
FORMAT = ('timelineResolutionWidth', 'timelineResolutionHeight', 'timelinePixelAspectRatio',
          'timelineInputResMismatchBehavior', 'timelineInputResMismatchUseCustomPreset',
          'timelineInputResMismatchCustomPreset', 'timelineOutputResMatchTimelineRes')
OUTPUT = ('timelineOutputResolutionWidth', 'timelineOutputResolutionHeight', 'timelineOutputPixelAspectRatio',
          'timelineOutputResMismatchBehavior', 'timelineOutputResMismatchUseCustomPreset',
          'timelineOutputResMismatchCustomPreset')
# Per clip: how frames are interpolated, and how the render fits the frame.
RETIME = ('RetimeProcess', 'MotionEstimation')
CLIP = RETIME + ('Scaling', 'ResizeFilter', 'CompositeMode')
# Restoring them writes to the temporary timeline; False goes back to a DRT straight from the XML.
RESTORE_DRT = True
KEPT = ' (the DRT keeps it)'


def active(resolve):
    if not resolve:
        raise RuntimeError('Resolve API unavailable. Run from Workspace > Scripts inside Resolve.')
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise RuntimeError('Open a project in Resolve')
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise RuntimeError('Open a timeline in Resolve')
    return project, timeline


def signature(timeline):
    """Detect edits during export without touching clips or project state."""
    return (timeline.GetUniqueId(), timeline.GetStartFrame(), tuple(
        (track, item.GetUniqueId(), item.GetName(), item.GetStart(), item.GetDuration())
        for track in range(1, timeline.GetTrackCount('video') + 1)
        for item in (timeline.GetItemListInTrack('video', track) or [])))


def source_ranges(timeline, items):
    """Resolve's own source positions as absolute timecode frames (exported speed
    ramps cannot be trusted). Absolute, because the XML Resolve exports may
    already point at renders while these positions count from the source."""
    found = {}
    for key, item in video_items(timeline).items():
        try:
            clip = item.GetMediaPoolItem()
            tc, fps = clip.GetClipProperty('Start TC'), float(clip.GetClipProperty('FPS'))
            base = tc_to_frames(tc, round(fps), ';' in tc)
            found[key] = (base + item.GetSourceStartFrame(), base + item.GetSourceEndFrame())
        except Exception:
            pass
    return {i.clip.uid: found[(i.clip.track, i.clip.start)] for i in items if (i.clip.track, i.clip.start) in found}


def timeline_size(timeline):
    """The timeline's own resolution (Timeline Settings), read-only; None if the API has none."""
    try:
        width, height = (int(float(timeline.GetSetting(key))) for key in FORMAT[:2])
    except Exception:
        return None
    return (width, height) if width > 0 and height > 0 else None


def project_size(found):
    try:
        width, height = (int(float(found['project'][key])) for key in FORMAT[:2])
    except (KeyError, TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def corrected_size(data, size, warnings):
    """The XML with the timeline's real resolution in its sequence format."""
    try:
        written = fcp7.sequence_size(data)
    except ValueError:
        return data
    if not size or tuple(size) == written:
        return data
    warnings.append("Resolve's XML gave the timeline as %dx%d; set to its real %dx%d." % (written + tuple(size)))
    return fcp7.with_size(data, *size)


def facts(project, timeline):
    """Everything the DRT gets back from the user's timeline, read-only: timeline
    settings, start timecode, track names and per-clip properties (by track and
    position), plus the project's retime settings that clips on default follow."""
    def safe_call(call, *args):
        try:
            return call(*args)
        except Exception:
            return None
    found = dict(settings={}, start=safe_call(timeline.GetStartTimecode), tracks={}, clips={},
                 project={key: safe_call(project.GetSetting, key)
                          for key in ('imageRetimeInterpolation', 'imageMotionEstimationMode',
                                      'timelineResolutionWidth', 'timelineResolutionHeight', 'timelineFrameRate')})
    for key in ('useCustomSettings', 'timelineFrameRate') + FORMAT + OUTPUT:
        value = safe_call(timeline.GetSetting, key)
        if value not in (None, ''):
            found['settings'][key] = str(value)
    for kind in ('video', 'audio'):
        for index in range(1, (safe_call(timeline.GetTrackCount, kind) or 0) + 1):
            found['tracks'][(kind, index)] = safe_call(timeline.GetTrackName, kind, index)
    for key, item in video_items(timeline).items():
        props = safe_call(item.GetProperty) or {}
        found['clips'][key] = {k: props[k] for k in CLIP if isinstance(props.get(k), int)}
    return found


def project_retime(resolve, found):
    """The per-clip constants matching the project's Frame Interpolation and Motion
    Estimation, for clips left on 'Project Settings'; None where unrecognised."""
    def constant(name):
        return _constant(resolve, name)
    process = str(found['project'].get('imageRetimeInterpolation') or '').lower().replace(' ', '')
    motion = str(found['project'].get('imageMotionEstimationMode') or '').lower().replace(' ', '')
    retime = (constant('RETIME_OPTICAL_FLOW') if 'optical' in process else constant('RETIME_FRAME_BLEND')
              if 'blend' in process else constant('RETIME_NEAREST') if 'nearest' in process else None)
    if 'metal' in motion:       # AI Speed Warp Metal (macOS), no "better"/"faster" variant
        return retime, constant('MOTION_EST_METAL')
    speed = 'SPEED_WARP' if 'warp' in motion else 'ENHANCED' if 'enhanced' in motion else 'STANDARD' if 'standard' in motion else None
    pace = 'BETTER' if 'better' in motion else 'FASTER' if 'fast' in motion else None
    return retime, constant('MOTION_EST_%s_%s' % (speed, pace)) if speed and pace else None


def _constant(resolve, name):
    """A RETIME_*/MOTION_EST_* constant as an int: Resolve hands them over as floats (3.0)."""
    value = getattr(resolve, name, None)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == int(value) else None


def free_timeline_name(project, base):
    try:
        taken = {project.GetTimelineByIndex(i).GetName() for i in range(1, project.GetTimelineCount() + 1)}
    except Exception:
        taken = set()
    name, n = base, 2
    while name in taken:
        name, n = '%s %d' % (base, n), n + 1
    return name


def free_stem(root, timeline_name):
    """<timeline>, or <timeline>_2, _3... if any file of that set already exists."""
    base = (safe(timeline_name) or 'timeline')[:80]
    stem, n = base, 2
    while any((root / (stem + ext)).exists() for ext in ('.xml', '.drt', '.csv', '_warnings.txt')):
        stem, n = '%s_%d' % (base, n), n + 1
    return stem


def build_bundle(xml_path, output_root, timeline_name, settings=Settings(), export_drt=False,
                 export_xml=True, export_csv=True, renders=None, bypass=(), sources=None,
                 make_drt=None, warnings=None, size=None, import_size=None, checks=None):
    """Conform `xml_path` (Resolve's FCP7 export) and write the chosen files.
    `make_drt(xml_file, drt_file, render_paths)` builds the DRT inside Resolve;
    `size` is the timeline's real resolution, `import_size` the project's: the
    XML Resolve imports for the DRT carries that one, because importing an XML
    of another size changes the project's resolution (drt_maker then gives the
    temporary timeline the real one). `checks(conformed)` gives warnings about
    the conformed XML, listed first."""
    if not any((export_drt, export_xml, export_csv)):
        raise ValueError('Select at least one export format')
    warnings = warnings if warnings is not None else []
    first = len(warnings)
    data = corrected_size(Path(xml_path).read_bytes(), size, warnings)
    root, items = fcp7.read(data, warnings)
    names = plan([i.clip for i in items], timeline_name, settings) if items else []
    render_folder = fcp7.Renders(renders) if renders else None
    conformed = fcp7.conform(data, names, render_folder, bypass, warnings, sources)
    if checks:
        warnings[first:first] = checks(conformed)
    output_root = Path(output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stem = free_stem(output_root, timeline_name)
    # Build everything in a hidden folder first, so a failure leaves nothing half-written.
    staging = Path(tempfile.mkdtemp(prefix='.ce-', dir=output_root))
    try:
        xml_out = staging / (stem + '.xml')
        xml_out.write_bytes(conformed)
        if export_drt:
            if make_drt is None:
                warnings.append('DRT is built by Resolve; run the export from inside Resolve.')
            else:
                for_resolve = staging / '.for-resolve.xml'
                for_resolve.write_bytes(fcp7.for_resolve_import(
                    fcp7.with_size(conformed, *import_size) if import_size else conformed))
                make_drt(for_resolve, staging / (stem + '.drt'), fcp7.linked_renders(conformed),
                         drt_sound(data, conformed, warnings))
                for_resolve.unlink()
        if not export_xml:
            xml_out.unlink()
        if export_csv:
            with (staging / (stem + '.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['track', 'index', 'timeline_position_frames', 'duration', 'old_name', 'source', 'new_name'])
                writer.writeheader()
                for p in (p for p in names if p.clip.media):
                    writer.writerow(dict(track='V%d' % p.clip.track, index=p.index, timeline_position_frames=p.clip.start,
                                         duration=p.clip.duration, old_name=p.clip.old_name, source=p.clip.source,
                                         new_name=p.new_name))
        if warnings:
            (staging / (stem + '_warnings.txt')).write_text('\n'.join(warnings) + '\n', encoding='utf-8')
        written = []
        for path in sorted(staging.iterdir()):
            target = output_root / path.name
            if target.exists():
                raise FileExistsError('Refusing to overwrite ' + str(target))
            path.rename(target)
            written.append(target)
        return written, names, warnings
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def drt_sound(original, conformed, warnings):
    """The audio files the DRT's bin gets with the renders. Not the camera file of
    a clip left without a render: Resolve would link that clip's picture back to
    it (by timecode), so its sound stays offline like its picture."""
    sounds, gone = fcp7.audio_media(conformed)
    if gone:
        warnings.append('DRT: %d audio files the timeline plays are not on disk; that audio is offline.' % gone)
    held = fcp7.unrendered_sources(original, conformed)
    kept = [p for p in sounds if p not in held]
    if len(kept) < len(sounds):
        warnings.append('DRT: the sound of clips without a render stays offline (%d camera files), or their picture '
                        'would link back to the camera originals.' % (len(sounds) - len(kept)))
    # A copy of a source goes into the bin for the clips linked to it; the
    # same source's clips left without a render may take it as well.
    names = {fcp7._fold(os.path.basename(p)) for p in held}
    caught = sorted({os.path.basename(p) for p in fcp7.source_copies(conformed).values()
                     if fcp7._fold(os.path.basename(p)) in names})
    if caught:
        warnings.append('DRT: clips without a render may link to %s, the copy of their source other clips use.'
                        % ', '.join(caught))
    return kept


def snapshot(resolve, folder):
    project, timeline = active(resolve)
    before = signature(timeline)
    path = Path(folder) / 'snapshot.xml'
    if not timeline.Export(str(path), resolve.EXPORT_FCP_7_XML) or not path.is_file():
        raise RuntimeError('Resolve failed to export FCP7 XML')
    if signature(timeline) != before or active(resolve)[1].GetUniqueId() != timeline.GetUniqueId():
        raise RuntimeError('Timeline changed during snapshot. Retry while editing is paused.')
    return project, timeline, before, path


def preview(resolve, settings, renders=None, bypass=()):
    """Names, warnings and the clips left without a render, from a dry run of the
    export's own conform, so the preview cannot promise a render the export skips."""
    with tempfile.TemporaryDirectory(prefix='ce-preview-') as temp:
        _, timeline, _, xml_path = snapshot(resolve, temp)
        warnings = []
        data = corrected_size(xml_path.read_bytes(), timeline_size(timeline), warnings)
        root, items = fcp7.read(data, warnings)
        names = plan([i.clip for i in items], timeline.GetName(), settings) if items else []
        missing = set()
        if names:
            conformed = fcp7.conform(data, names, fcp7.Renders(renders) if renders else None, bypass, warnings,
                                     source_ranges(timeline, items))
            if renders:
                missing = {p.clip.uid for p in names if p.clip.media} - fcp7.linked_clips(conformed)
        return names, warnings, missing


def video_items(timeline):
    start = timeline.GetStartFrame()
    return {(track, item.GetStart() - start): item
            for track in range(1, timeline.GetTrackCount('video') + 1)
            for item in timeline.GetItemListInTrack('video', track) or []}


def flips(timeline, items):
    """Flipped clips whose transforms still travel in the XML (`items`, read from
    the conformed XML). FCP7 XML (and so the DRT) cannot carry flips; for
    rendered clips Resolve bakes them in."""
    moving = {(i.clip.track, i.clip.start) for i in items
              if any(fcp7._effect(f) == 'Basic Motion' for f in i.node.all('filter'))}
    count = 0
    for key, item in video_items(timeline).items():
        try:
            props = item.GetProperty() or {}
        except Exception:  # older Resolve versions: no Inspector properties in the API
            return 0
        count += key in moving and bool(props.get('FlipX') or props.get('FlipY'))
    return count


def drt_maker(resolve, project, timeline, found=None, bypass=(), warnings=None):
    """Let Resolve turn the conformed XML into a native DRT, then remove every
    temporary trace: renders, audio and timeline go into their own bin, deleted
    after. Resolve conforms only to the media we import; it never looks for media
    itself (with one missing file it would refuse the whole import).

    The imported timeline has the project's settings and no retime process, so
    what FCP7 XML cannot carry is copied from `found` (facts() of the user's
    timeline) before the DRT is exported. Only the temporary timeline is written
    to, and only while it is the current one: editing a timeline that is not
    current and then deleting it crashed Resolve 21. It is never deleted while
    current."""
    warnings = warnings if warnings is not None else []

    def make(xml_file, drt_file, render_paths, sound_paths=()):
        pool = project.GetMediaPool()
        before = _project_format(project)
        folder_before = pool.GetCurrentFolder()
        folder = pool.AddSubFolder(pool.GetRootFolder(), TEMP_BIN)
        if not folder:
            raise RuntimeError('Could not create a temporary bin for the DRT')
        data = Path(xml_file).read_bytes()
        imported, kept = None, False
        try:
            pool.SetCurrentFolder(folder)
            if render_paths and not pool.ImportMedia(list(render_paths)):
                raise RuntimeError('Resolve could not import the renders for the DRT')
            if sound_paths:
                added = len(pool.ImportMedia(list(sound_paths)) or [])
                if added < len(sound_paths):
                    warnings.append('DRT: %d of %d audio files could not be imported; that audio is offline.'
                                    % (len(sound_paths) - added, len(sound_paths)))
            imported = pool.ImportTimelineFromFile(str(xml_file), {
                'timelineName': free_timeline_name(project, '%s conform' % timeline.GetName()),
                'importSourceClips': False, 'sourceClipsFolders': [folder]})
            if not imported:
                raise RuntimeError('Resolve could not import the conformed XML for the DRT')
            if found is not None and RESTORE_DRT:
                try:
                    kept = restore(resolve, project, imported, found, fcp7.retimed(data), bypass, warnings,
                                   fcp7.source_copies(data))
                except Exception as exc:  # the DRT is still worth having
                    warnings.append('DRT: timeline settings and clip properties were not fully restored (%s).' % exc)
            if not kept:
                warnings[:] = [w.replace(KEPT, '') for w in warnings]
            if not imported.Export(str(drt_file), resolve.EXPORT_DRT) or not Path(drt_file).is_file():
                raise RuntimeError('Resolve failed to export the DRT')
        finally:
            back = _switch(project, timeline)
            if imported and not back and _is_current(project, imported):
                warnings.append("DRT: Resolve did not switch back to your timeline, so the temporary timeline '%s' and "
                                "the bin '%s' were left in the project; delete them by hand." % (imported.GetName(), TEMP_BIN))
            else:
                if imported:
                    gc.collect()        # no clip of the temporary timeline is referenced any more
                    time.sleep(0.5)     # let Resolve finish switching before the delete
                    pool.DeleteTimelines([imported])
                clips = folder.GetClipList() or []
                if clips:
                    pool.DeleteClips(clips)
                pool.DeleteFolders([folder])
                if folder_before:
                    pool.SetCurrentFolder(folder_before)
        after = _project_format(project)
        if after != before:
            warnings.append("Resolve changed the project's settings while building the DRT (%s); check Project Settings."
                            % ', '.join('%s %s -> %s' % (k, before[k], after[k]) for k in before if before[k] != after[k]))
    return make


def _is_current(project, timeline):
    try:
        return project.GetCurrentTimeline().GetUniqueId() == timeline.GetUniqueId()
    except Exception:
        return False


def _switch(project, timeline):
    try:
        project.SetCurrentTimeline(timeline)
    except Exception:
        pass
    return _is_current(project, timeline)


def _project_format(project):
    found = {}
    for key in ('timelineResolutionWidth', 'timelineResolutionHeight', 'timelineFrameRate'):
        try:
            found[key] = str(project.GetSetting(key))
        except Exception:
            found[key] = None
    return found


class _Switched(Exception):
    pass


def restore(resolve, project, imported, found, retimed, bypass, warnings, copies=()):
    """Copy onto the imported timeline what the XML lost (see drt_maker). Every
    single write is made while it is the current timeline; if the user switches
    away, restoring stops. True when every retimed clip got its retime process.
    `copies`: clips on a copy of their source, which nothing is bypassed for."""
    if not _switch(project, imported):
        warnings.append('DRT: the temporary timeline could not be made current, so its settings were not restored.')
        return False
    try:
        return _restore(resolve, project, imported, found, retimed, bypass, warnings, copies)
    except _Switched:
        warnings.append('DRT: you switched timelines while the DRT was being built, so the rest of its settings '
                        'were not restored; export again to get them.')
        return False


def _restore(resolve, project, imported, found, retimed, bypass, warnings, copies=()):
    def write(call, *args):
        if not _is_current(project, imported):
            raise _Switched()
        return call(*args)
    failed = []
    source = found['settings']
    keys = FORMAT + (OUTPUT if source.get('timelineOutputResMatchTimelineRes') == '0' else ())
    keys = [k for k in keys if not k.endswith('CustomPreset') or k.endswith('UseCustomPreset')
            or source.get(k.replace('CustomPreset', 'UseCustomPreset')) == '1']
    differ = lambda: [k for k in keys if k in source and str(imported.GetSetting(k)) != source[k]]
    if differ():
        if str(imported.GetSetting('useCustomSettings')) != '1' and not write(imported.SetSetting, 'useCustomSettings', '1'):
            failed.append('useCustomSettings')
        for key in differ():        # again: switching to custom settings resets some (3840x1080 for 3840x2160)
            if not write(imported.SetSetting, key, source[key]) or str(imported.GetSetting(key)) != source[key]:
                failed.append(key)
    size = [source.get(k) for k in FORMAT[:2]]
    got = [str(imported.GetSetting(k)) for k in FORMAT[:2]]
    if None not in size and got != size:
        warnings.append('DRT: its timeline is %s, the timeline is %s.' % ('x'.join(got), 'x'.join(size)))
    rate = [_rate(source.get('timelineFrameRate')), _rate(imported.GetSetting('timelineFrameRate'))]
    if None not in rate and abs(rate[0] - rate[1]) > 1e-3:
        warnings.append('DRT: its timeline runs at %s fps, the timeline at %s fps.' % (rate[1], rate[0]))
    if found.get('start') and imported.GetStartTimecode() != found['start']:
        if not write(imported.SetStartTimecode, found['start']):
            failed.append('start timecode')
    for (kind, index), name in found['tracks'].items():
        if name and index <= (imported.GetTrackCount(kind) or 0) and imported.GetTrackName(kind, index) != name:
            if not write(imported.SetTrackName, kind, index, name):
                failed.append('%s track %d name' % (kind, index))
    # Clips on 'Project Settings' follow whichever project opens the DRT; the
    # project's own choice is written into each retimed clip instead.
    on_project = dict(zip(RETIME, project_retime(resolve, found)))
    flow = _constant(resolve, 'RETIME_OPTICAL_FLOW')
    unknown = {k: set() for k in RETIME}
    for key, item in video_items(imported).items():
        props = dict(found['clips'].get(key, {}))
        baked = key not in copies       # a render; nothing is baked into a copy of the source
        if 'retime' in bypass and baked:
            props = {k: v for k, v in props.items() if k not in RETIME}
        elif key in retimed:
            for k in RETIME:
                if k in props and not props[k]:
                    if on_project.get(k):
                        props[k] = on_project[k]
                    elif k == 'RetimeProcess' or props.get('RetimeProcess') == flow:
                        unknown[k].add(key)     # motion estimation matters only to Optical Flow
        if 'composite' in bypass and baked:
            props.pop('CompositeMode', None)
        for k, value in props.items():
            if item.GetProperty(k) != value and (not write(item.SetProperty, k, value) or item.GetProperty(k) != value):
                failed.append(k)
    labels = dict(zip(RETIME, ('Frame Interpolation', 'Motion Estimation')))
    for k, keys_ in unknown.items():
        if keys_:
            warnings.append("DRT: the project's %s (%s) was not recognised; %d retimed clips follow the project that "
                            "opens the DRT for it." % (labels[k], found['project'].get(
                                'imageRetimeInterpolation' if k == 'RetimeProcess' else 'imageMotionEstimationMode'), len(keys_)))
    if failed:
        counts = {}
        for k in failed:
            counts[k] = counts.get(k, 0) + 1
        warnings.append('DRT: could not restore %s.' % ', '.join(k if n == 1 else '%s (%d clips)' % (k, n)
                                                                   for k, n in sorted(counts.items())))
    return not unknown['RetimeProcess'] and 'RetimeProcess' not in failed


def _rate(value):
    try:
        return float(str(value).replace('DF', '').strip())
    except ValueError:
        return None


def export_current(resolve, output_root, settings=Settings(), export_drt=True, export_xml=True,
                   export_csv=True, renders=None, bypass=()):
    with tempfile.TemporaryDirectory(prefix='ce-export-') as temp:
        project, timeline, before, xml_path = snapshot(resolve, temp)
        root, items = fcp7.read(xml_path.read_bytes(), [])
        sources = source_ranges(timeline, items)
        found = facts(project, timeline)
        if signature(timeline) != before or active(resolve)[1].GetUniqueId() != timeline.GetUniqueId():
            raise RuntimeError('Timeline changed during export. Retry while editing is paused.')
        warnings = []
        retimed = fcp7.retimed(xml_path.read_bytes())

        def checks(conformed):
            # What the conformed XML still carries: bypass leaves the transforms
            # and retime of clips on a copy of their source.
            notes = []
            flipped = flips(timeline, fcp7.read(conformed, [])[1])
            if flipped:
                notes.append('%d clips are flipped in the Inspector; FCP7 XML and DRT cannot carry flips.' % flipped)
            flow = optical_flow(resolve, found, retimed if 'retime' not in bypass
                                else retimed & set(fcp7.source_copies(conformed))) if export_xml else 0
            if flow:
                # drt_maker takes the promise back if it could not keep it
                notes.append('%d retimed clips use Optical Flow; FCP7 XML cannot say so%s.'
                             % (flow, KEPT if export_drt and RESTORE_DRT else ''))
            return notes
        return build_bundle(xml_path, output_root, timeline.GetName(), settings, export_drt, export_xml,
                            export_csv, renders, bypass, sources,
                            drt_maker(resolve, project, timeline, found, bypass, warnings) if export_drt else None,
                            warnings, timeline_size(timeline), project_size(found), checks)


def optical_flow(resolve, found, retimed):
    """How many retimed clips Resolve interpolates with Optical Flow, by their own
    setting or, on 'Project Settings', the project's."""
    flow = _constant(resolve, 'RETIME_OPTICAL_FLOW')
    if flow is None:
        return 0
    on_project = project_retime(resolve, found)[0]
    return sum(1 for key in retimed
               if (found['clips'].get(key, {}).get('RetimeProcess') or on_project) == flow)
