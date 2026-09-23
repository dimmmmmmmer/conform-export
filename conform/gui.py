"""Native Fusion UIManager frontend; no third-party Python dependencies."""
from pathlib import Path
import traceback
from .exporter import active, preview, export_current
from .naming import Settings, DEFAULT_TEMPLATE

BYPASS = (('transforms', 'Transforms'), ('crop', 'Crop'), ('retime', 'Retime'), ('opacity', 'Opacity'))


def launch(resolve, fusion, bmd):
    if not fusion or not bmd:
        raise RuntimeError('Fusion UIManager unavailable. Run the launcher inside Resolve with Python 3.')
    ui = fusion.UIManager
    dispatcher = bmd.UIDispatcher(ui)
    window_id = 'ConformExport'
    existing = ui.FindWindow(window_id)
    if existing:
        existing.Show(); existing.Raise()
        return

    def label(text):
        return ui.Label({'Text': text, 'Weight': 0})

    def folder(caption, id_, value):
        # Resolve's button style keeps buttons at least ~95 px wide; the style
        # rule shrinks the size hint itself, so the layout gives the room back.
        return [label(caption), ui.LineEdit({'ID': id_, 'Text': value, 'Weight': 1}),
                ui.Button({'ID': id_ + 'Browse', 'Text': '…', 'Weight': 0,
                           'StyleSheet': 'QPushButton { min-width: 0px; padding: 1px 8px; }'})]

    # UIManager ignores MinimumSize (on widgets and on the window) and Weight 0
    # collapses a LineEdit, so widths are expressed with weights. The resize
    # limit comes from fixed spacers: macOS will not shrink the window below
    # the layout's minimum; the window opens at that size.
    width, preview_height = 760, 290
    win = dispatcher.AddWindow({'ID': window_id, 'WindowTitle': 'Conform Export',
                                'Geometry': [140, 120, width, 470]}, ui.VGroup({'Spacing': 8}, [
        ui.Label({'ID': 'Context', 'Text': '', 'Weight': 0, 'Alignment': {'AlignHCenter': True, 'AlignVCenter': True}}),
        ui.HGroup({'Weight': 0, 'Spacing': 6}, [
            label('Naming'), ui.LineEdit({'ID': 'Template', 'Text': DEFAULT_TEMPLATE, 'Weight': 7}),
            ui.HGap(8, 0), label('Prefix'), ui.LineEdit({'ID': 'Prefix', 'Text': 'V', 'Weight': 1}),
            ui.HGap(16, 0), label('Bypass'), ui.HGap(4, 0)] + [
            ui.CheckBox({'ID': 'Bypass_' + key, 'Text': text, 'Checked': False, 'Weight': 0}) for key, text in BYPASS]),
        ui.HGroup({'Weight': 0, 'Spacing': 6}, folder('Output', 'Folder', str(Path.home() / 'Movies'))
                  + [ui.HGap(8, 0)] + folder('Renders', 'Renders', '')),
        ui.HGroup({'Spacing': 0}, [ui.TextEdit({'ID': 'PreviewText', 'ReadOnly': True, 'PlainText': ''}),
                                   ui.VGap(preview_height, 0)]),
        ui.Label({'ID': 'Status', 'Text': '', 'WordWrap': True, 'Weight': 0}),
        ui.VGroup({'Weight': 0, 'Spacing': 0}, [
            ui.HGroup({'Weight': 0, 'Spacing': 12}, [
                ui.CheckBox({'ID': 'DRT', 'Text': 'DRT', 'Checked': True, 'Weight': 0}),
                ui.CheckBox({'ID': 'XML', 'Text': 'FCP7 XML', 'Checked': True, 'Weight': 0}),
                ui.CheckBox({'ID': 'CSV', 'Text': 'CSV', 'Checked': True, 'Weight': 0}),
                ui.HGap(0, 1),
                ui.Button({'ID': 'Preview', 'Text': 'Refresh / Preview', 'Weight': 0}),
                ui.Button({'ID': 'Export', 'Text': 'Export', 'Weight': 0})]),
            ui.HGap(width - 20, 0)])]))
    items = win.GetItems()

    def settings():
        return Settings(items['Template'].Text, items['Prefix'].Text)

    def renders():
        return items['Renders'].Text.strip() or None

    def bypass():
        return [key for key, _ in BYPASS if items['Bypass_' + key].Checked]

    def context():
        try:
            project, timeline = active(resolve)
            items['Context'].Text = 'Project: %s  |  Timeline: %s' % (project.GetName(), timeline.GetName())
        except Exception as exc:
            items['Context'].Text = str(exc)

    def show(names, warnings, missing=()):
        lines = ['V%d  @%d  %s → %s%s' % (p.clip.track, p.clip.start, p.clip.old_name, p.new_name,
                                            '   · no render' if p.clip.uid in missing else '') for p in names]
        items['PreviewText'].PlainText = '\n'.join(lines) + ('\n\nWARNINGS\n' + '\n'.join(warnings) if warnings else '')

    def perform(fn):
        items['Export'].Enabled = False; items['Preview'].Enabled = False
        try:
            context(); fn()
        except Exception as exc:
            items['Status'].Text = 'Error: ' + str(exc)
            traceback.print_exc()
        finally:
            items['Export'].Enabled = True; items['Preview'].Enabled = True

    def refresh(event):
        def work():
            names, warnings, missing = preview(resolve, settings(), renders())
            show(names, warnings, missing)
            items['Status'].Text = '%d clips%s; %d warnings.' % (
                len(names), '; %d without render' % len(missing) if renders() else '', len(warnings))
        perform(work)

    def export(event):
        def work():
            if not items['Folder'].Text.strip():
                raise ValueError('Choose an output folder')
            dest, names, warnings = export_current(resolve, items['Folder'].Text, settings(),
                bool(items['DRT'].Checked), bool(items['XML'].Checked), bool(items['CSV'].Checked),
                renders(), bypass())
            show(names, warnings)
            items['Status'].Text = 'Saved %d clips, %d warnings: %s' % (len(names), len(warnings), ', '.join(p.name for p in dest))
        perform(work)

    def browse(field):
        def handler(event):
            path = fusion.RequestDir(items[field].Text or str(Path.home()))
            if path:
                items[field].Text = str(path)
        return handler

    def dirty(event):
        items['Status'].Text = 'Settings changed. Refresh preview before exporting.'

    win.On[window_id].Close = lambda ev: dispatcher.ExitLoop()
    win.On['Preview'].Clicked = refresh
    win.On['Export'].Clicked = export
    win.On['FolderBrowse'].Clicked = browse('Folder')
    win.On['RendersBrowse'].Clicked = browse('Renders')
    for id_ in ('Template', 'Prefix', 'Renders'):
        win.On[id_].TextChanged = dirty
    context()
    win.Show()
    try:
        dispatcher.RunLoop()
    finally:
        win.Hide()
