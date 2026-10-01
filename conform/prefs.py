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


# What the window saves; a value of another type (a hand-edited file) is ignored,
# so the window falls back to its default instead of failing to open.
TYPES = dict(template=str, prefix=str, output=str, renders=str, bypass=list, drt=bool, xml=bool, csv=bool)


def load():
    try:
        values = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in TYPES and isinstance(value, TYPES[key])
            and (key != 'bypass' or all(isinstance(b, str) for b in value))}


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
