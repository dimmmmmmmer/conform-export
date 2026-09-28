"""Remember the window's settings between runs, outside the install folder."""
import json
import os
import sys
from pathlib import Path


def path():
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or Path.home())
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'Conform Export' / 'settings.json'


def load():
    try:
        values = json.loads(path().read_text(encoding='utf-8'))
        return values if isinstance(values, dict) else {}
    except (OSError, ValueError):
        return {}


def save(values):
    """Best effort: failing to remember settings must never stop an export."""
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, target)
    except OSError:
        pass
