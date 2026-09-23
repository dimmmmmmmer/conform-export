"""Resolve menu entry point. Startup errors are also written beside this file."""
import os
import sys
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, 'startup.log')


def log(message):
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write('%s %s\n' % (datetime.now().isoformat(), message))


def script_api():
    if sys.platform == 'darwin':
        api_root = '/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting'
    elif sys.platform == 'win32':
        api_root = os.path.join(os.environ.get('PROGRAMDATA', r'C:\ProgramData'), 'Blackmagic Design', 'DaVinci Resolve', 'Support', 'Developer', 'Scripting')
    else:
        api_root = '/opt/resolve/Developer/Scripting'
    modules = os.path.join(os.environ.get('RESOLVE_SCRIPT_API', api_root), 'Modules')
    if os.path.isdir(modules):
        sys.path.insert(0, modules)
    import DaVinciResolveScript
    return DaVinciResolveScript


try:
    log('Starting Conform Export; Python=%s; executable=%s' % (sys.version, sys.executable))
    sys.path.insert(0, ROOT)
    from conform.gui import launch

    # Resolve's Scripts menu injects these; the API module is only a fallback.
    g = globals()
    resolve_app, fusion_app, bmd_module = g.get('resolve'), g.get('fusion') or g.get('fu'), g.get('bmd')
    if not (resolve_app and bmd_module):
        api = script_api()
        resolve_app = resolve_app or api.scriptapp('Resolve')
        bmd_module = bmd_module or api
    fusion_app = fusion_app or (resolve_app.Fusion() if resolve_app else None)
    log('Bindings: resolve=%s fusion=%s bmd=%s' % (bool(resolve_app), bool(fusion_app), bool(bmd_module)))
    launch(resolve_app, fusion_app, bmd_module)
    log('GUI closed normally')
except Exception:
    detail = traceback.format_exc()
    log(detail)
    print('Render-Named Timeline Exporter failed. Details: ' + LOG)
    print(detail)
    raise
