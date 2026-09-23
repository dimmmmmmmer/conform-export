"""Resolve adapter and export writer: <timeline>.xml|.drt|.csv in the chosen folder."""
from pathlib import Path
import csv
import shutil
import tempfile
from . import fcp7
from .media import tc_to_frames
from .naming import Settings, plan, safe

TEMP_BIN = 'Conform Export (temp)'


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
    start, found = timeline.GetStartFrame(), {}
    for key, item in video_items(timeline).items():
        try:
            clip = item.GetMediaPoolItem()
            tc, fps = clip.GetClipProperty('Start TC'), float(clip.GetClipProperty('FPS'))
            base = tc_to_frames(tc, round(fps), ';' in tc)
            found[key] = (base + item.GetSourceStartFrame(), base + item.GetSourceEndFrame())
        except Exception:
            pass
    return {i.clip.uid: found[(i.clip.track, i.clip.start)] for i in items if (i.clip.track, i.clip.start) in found}


def free_stem(root, timeline_name):
    """<timeline>, or <timeline>_2, _3... if any file of that set already exists."""
    base = (safe(timeline_name) or 'timeline')[:80]
    stem, n = base, 2
    while any((root / (stem + ext)).exists() for ext in ('.xml', '.drt', '.csv', '_warnings.txt')):
        stem, n = '%s_%d' % (base, n), n + 1
    return stem


def build_bundle(xml_path, output_root, timeline_name, settings=Settings(), export_drt=False,
                 export_xml=True, export_csv=True, renders=None, bypass=(), sources=None,
                 make_drt=None, warnings=None):
    """Conform `xml_path` (Resolve's FCP7 export) and write the chosen files.
    `make_drt(xml_file, drt_file, render_paths)` builds the DRT inside Resolve."""
    if not any((export_drt, export_xml, export_csv)):
        raise ValueError('Select at least one export format')
    warnings = warnings if warnings is not None else []
    data = Path(xml_path).read_bytes()
    root, items = fcp7.read(data, warnings)
    names = plan([i.clip for i in items], timeline_name, settings) if items else []
    render_folder = fcp7.Renders(renders) if renders else None
    conformed = fcp7.conform(data, names, render_folder, bypass, warnings, sources)
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
                for_resolve.write_bytes(fcp7.for_resolve_import(conformed))
                make_drt(for_resolve, staging / (stem + '.drt'), fcp7.linked_renders(conformed))
                for_resolve.unlink()
        if not export_xml:
            xml_out.unlink()
        if export_csv:
            with (staging / (stem + '.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['track', 'index', 'timeline_position_frames', 'duration', 'old_name', 'source', 'new_name'])
                writer.writeheader()
                for p in names:
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


def snapshot(resolve, folder):
    project, timeline = active(resolve)
    before = signature(timeline)
    path = Path(folder) / 'snapshot.xml'
    if not timeline.Export(str(path), resolve.EXPORT_FCP_7_XML) or not path.is_file():
        raise RuntimeError('Resolve failed to export FCP7 XML')
    if signature(timeline) != before or active(resolve)[1].GetUniqueId() != timeline.GetUniqueId():
        raise RuntimeError('Timeline changed during snapshot. Retry while editing is paused.')
    return project, timeline, before, path


def preview(resolve, settings, renders=None):
    with tempfile.TemporaryDirectory(prefix='ce-preview-') as temp:
        _, timeline, _, xml_path = snapshot(resolve, temp)
        warnings = []
        clips = fcp7.clips(xml_path.read_bytes(), warnings)
        names = plan(clips, timeline.GetName(), settings) if clips else []
        missing = set()
        if renders:
            folder = fcp7.Renders(renders)
            missing = {p.clip.uid for p in names if not folder.named(p.new_name)}
        return names, warnings, missing


def video_items(timeline):
    start = timeline.GetStartFrame()
    return {(track, item.GetStart() - start): item
            for track in range(1, timeline.GetTrackCount('video') + 1)
            for item in timeline.GetItemListInTrack('video', track) or []}


def flips(timeline, items):
    """Flipped clips whose transforms still travel in the XML. FCP7 XML (and so
    the DRT) cannot carry flips; for rendered clips Resolve bakes them in."""
    moving = {(i.clip.track, i.clip.start) for i in items
              if any(fcp7._effect(f) == 'Basic Motion' for f in i.node.all('filter'))}
    count = 0
    for key, item in video_items(timeline).items():
        props = item.GetProperty() or {}
        count += key in moving and bool(props.get('FlipX') or props.get('FlipY'))
    return count


def drt_maker(resolve, project, timeline):
    """Let Resolve turn the conformed XML into a native DRT, then remove every
    temporary trace: renders and timeline go into their own bin, deleted after.
    Resolve conforms only to the renders we import; it never looks for media
    itself (with one missing file it would refuse the whole import). Nothing is
    changed on the temporary timeline: editing a timeline that is not current
    and then deleting it crashed Resolve 21."""
    def make(xml_file, drt_file, render_paths):
        pool = project.GetMediaPool()
        folder_before = pool.GetCurrentFolder()
        folder = pool.AddSubFolder(pool.GetRootFolder(), TEMP_BIN)
        if not folder:
            raise RuntimeError('Could not create a temporary bin for the DRT')
        imported = None
        try:
            pool.SetCurrentFolder(folder)
            if render_paths and not pool.ImportMedia(list(render_paths)):
                raise RuntimeError('Resolve could not import the renders for the DRT')
            imported = pool.ImportTimelineFromFile(str(xml_file), {
                'timelineName': '%s (conform temp)' % timeline.GetName(), 'importSourceClips': False,
                'sourceClipsFolders': [folder]})
            if not imported:
                raise RuntimeError('Resolve could not import the conformed XML for the DRT')
            if not imported.Export(str(drt_file), resolve.EXPORT_DRT) or not Path(drt_file).is_file():
                raise RuntimeError('Resolve failed to export the DRT')
        finally:
            if imported:
                pool.DeleteTimelines([imported])
            clips = folder.GetClipList() or []
            if clips:
                pool.DeleteClips(clips)
            pool.DeleteFolders([folder])
            if folder_before:
                pool.SetCurrentFolder(folder_before)
            project.SetCurrentTimeline(timeline)
    return make


def export_current(resolve, output_root, settings=Settings(), export_drt=True, export_xml=True,
                   export_csv=True, renders=None, bypass=()):
    with tempfile.TemporaryDirectory(prefix='ce-export-') as temp:
        project, timeline, before, xml_path = snapshot(resolve, temp)
        root, items = fcp7.read(xml_path.read_bytes(), [])
        sources = source_ranges(timeline, items)
        if signature(timeline) != before or active(resolve)[1].GetUniqueId() != timeline.GetUniqueId():
            raise RuntimeError('Timeline changed during export. Retry while editing is paused.')
        warnings = []
        flipped = flips(timeline, items) if 'transforms' not in bypass else 0
        if flipped:
            warnings.append('%d clips are flipped in the Inspector; FCP7 XML and DRT cannot carry flips.' % flipped)
        return build_bundle(xml_path, output_root, timeline.GetName(), settings, export_drt, export_xml,
                            export_csv, renders, bypass, sources,
                            drt_maker(resolve, project, timeline) if export_drt else None, warnings)
