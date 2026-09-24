"""Per-user, standard-library-only installer for macOS, Windows and Linux."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

MARKER = '# Conform Export managed launcher'
INSTALLED = '.conform-export.json'
# Earlier builds (Render Named Timeline Exporter) used these; still recognised.
OLD_MARKER, OLD_INSTALLED = '# RNTE managed launcher', '.rnte-install.json'
OLD_AGENTS = ('org.rnte.python3home', 'com.github.enrvate.conform-export.python3home')
APP = 'ConformExport'
ENTRY = 'Conform Export.py'
LEGACY = [('RenderNamedTimelineExporter', 'Render Named Timeline Exporter.py')]
AGENT_LABEL = 'conform-export.python3home'


def locations(platform=None, home=None, environ=None):
    platform, home, environ = platform or sys.platform, Path(home or Path.home()), os.environ if environ is None else environ
    if platform == 'darwin':
        support = home / 'Library/Application Support/Blackmagic Design/DaVinci Resolve'
        return support / APP, support / 'Fusion/Scripts/Utility'
    if platform == 'win32':
        support = Path(environ.get('APPDATA', str(home / 'AppData/Roaming'))) / 'Blackmagic Design/DaVinci Resolve/Support'
        return support / APP, support / 'Fusion/Scripts/Utility'
    if platform.startswith('linux'):
        support = home / '.local/share/DaVinciResolve'
        return support / APP, support / 'Fusion/Scripts/Utility'
    raise RuntimeError('Supported systems: macOS, Windows, Linux')


def native_machine(run=subprocess.run):
    """CPU of this Mac, even when the caller itself runs under Rosetta."""
    r = run(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'], capture_output=True, text=True)
    return 'arm64' if r.returncode == 0 and r.stdout.strip() == '1' else 'x86_64'


def probe(executable, machine=None):
    code = 'import sys,json,struct,platform;print(json.dumps(dict(executable=sys.executable,home=sys.base_prefix,version=list(sys.version_info[:3]),bits=struct.calcsize("P")*8,machine=platform.machine())))'
    try:
        r = subprocess.run([str(executable), '-c', code], capture_output=True, text=True, timeout=8)
        data = json.loads(r.stdout)
        if r.returncode == 0 and data['bits'] == 64 and (3, 9) <= tuple(data['version'][:2]) <= (3, 12):
            # Resolve loads libpython into its own process, so an Intel-only
            # Python (e.g. Homebrew under /usr/local) is unusable on Apple Silicon.
            if machine is None or data['machine'] == machine:
                return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def choose_python():
    candidates = []
    if sys.platform == 'darwin':
        candidates += [Path('/Library/Frameworks/Python.framework/Versions') / version / 'bin/python3'
                       for version in ('3.12', '3.11', '3.10', '3.9')]
    if sys.platform == 'win32':
        launcher = shutil.which('py')
        if launcher:
            for version in ('3.12', '3.11', '3.10', '3.9'):
                try:
                    p = subprocess.run([launcher, '-' + version, '-c', 'import sys;print(sys.executable)'],
                                       capture_output=True, text=True, timeout=8)
                    if p.returncode == 0:
                        candidates.append(p.stdout.strip())
                except (OSError, subprocess.TimeoutExpired):
                    pass
    candidates += [p for name in ('python3.12','python3.11','python3.10','python3.9') if (p := shutil.which(name))]
    candidates += [sys.executable]
    machine = native_machine() if sys.platform == 'darwin' else None
    for executable in candidates:
        info = probe(executable, machine)
        if info:
            return info
    raise RuntimeError('No suitable 64-bit Python found (on Apple Silicon it must be arm64/universal). Install Python 3.12 from https://www.python.org/downloads/ and run this installer again. Python is not downloaded automatically.')


def ours(launcher):
    head = launcher.read_text(encoding='utf-8', errors='replace').split('\n', 1)[0]
    return head in (MARKER, OLD_MARKER)


def installed(folder):
    return (folder / INSTALLED).is_file() or (folder / OLD_INSTALLED).is_file()


def launch_content(payload):
    return MARKER + '\nimport runpy\nrunpy.run_path(%r, init_globals=globals(), run_name="__main__")\n' % str(payload / ENTRY)


def agent_path(home=None, label=AGENT_LABEL):
    return Path(home or Path.home()) / 'Library/LaunchAgents' / (label + '.plist')


def agent_content(python_home):
    return plistlib.dumps(dict(Label=AGENT_LABEL, RunAtLoad=True,
                               ProgramArguments=['/bin/launchctl', 'setenv', 'PYTHON3HOME', python_home]))


def configure_macos_python(python_home, home=None, run=subprocess.run):
    """Resolve on macOS probes only $PYTHON3HOME or /usr/local/bin/python3.
    Set PYTHON3HOME for apps started from Dock/Finder now and at every login.
    CPython itself ignores this variable, so other Python tools are unaffected."""
    path = agent_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(agent_content(python_home))
    for label in OLD_AGENTS:
        old = agent_path(home, label)
        if old.is_file():
            old.unlink()
    run(['/bin/launchctl', 'setenv', 'PYTHON3HOME', python_home], check=True)
    return path


def install(source, payload, menu):
    source, payload, menu = map(Path, (source, payload, menu))
    if payload.resolve() == source.resolve():
        raise RuntimeError('Installation destination must differ from source')
    destination = menu / ENTRY
    if destination.exists() and not ours(destination):
        raise RuntimeError('An unrelated script already uses this menu name; refusing to overwrite: ' + str(destination))
    if payload.exists() and not installed(payload):
        raise RuntimeError('Destination is not a Conform Export installation: ' + str(payload))
    payload.parent.mkdir(parents=True, exist_ok=True)
    menu.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.ce-install-', dir=payload.parent))
    backup = None
    old_menu = destination.read_bytes() if destination.exists() else None
    published = False
    try:
        shutil.copytree(source / 'conform', stage / 'conform', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(source / ENTRY, stage / ENTRY)
        (stage / INSTALLED).write_text(json.dumps(dict(version=2, source=str(source))), encoding='utf-8')
        if payload.exists():
            backup = payload.with_name(payload.name + '.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            payload.rename(backup)
        stage.rename(payload); published = True
        temporary_menu = destination.with_suffix('.ce-tmp')
        temporary_menu.write_text(launch_content(payload), encoding='utf-8')
        os.replace(temporary_menu, destination)
    except BaseException:
        if published:
            shutil.rmtree(payload)
        if backup:
            backup.rename(payload)
        if stage.exists():
            shutil.rmtree(stage)
        if old_menu is not None:
            destination.write_bytes(old_menu)
        raise
    # Keep only the newest previous version.
    for old in payload.parent.glob(payload.name + '.backup-*'):
        if old != backup:
            shutil.rmtree(old)
    return destination


def remove_legacy(payload, menu):
    """Remove earlier installs under the old name; only our own files are touched."""
    removed = []
    for app, entry in LEGACY:
        launcher = menu / entry
        if launcher.is_file() and ours(launcher):
            launcher.unlink()
            removed.append(launcher)
        for old in [payload.parent / app] + sorted(payload.parent.glob(app + '.backup-*')):
            if installed(old):
                shutil.rmtree(old)
                removed.append(old)
    return removed


def main(argv=None):
    p = argparse.ArgumentParser(description='Install Conform Export for the current user. No admin rights required.')
    p.add_argument('--target', type=Path, help='Custom installation root (for testing/portable deployment)')
    p.add_argument('--check', action='store_true', help='Check runtime and paths without installing')
    args = p.parse_args(argv)
    source = Path(__file__).resolve().parent
    runtime = choose_python()
    payload, menu = locations()
    if args.target:
        payload, menu = args.target / APP, args.target / 'Scripts/Utility'
    print('Python: %s (%s)' % (runtime['executable'], '.'.join(map(str, runtime['version']))))
    print('Application: ' + str(payload))
    print('Scripts: ' + str(menu))
    current = None
    if sys.platform == 'darwin':
        current = subprocess.run(['/bin/launchctl', 'getenv', 'PYTHON3HOME'], capture_output=True, text=True).stdout.strip()
        print('PYTHON3HOME for apps: ' + (current or '(not set)'))
    if args.check:
        return
    destination = install(source, payload, menu)
    for old in remove_legacy(payload, menu):
        print('Removed old version: ' + str(old))
    print('\nInstalled: ' + str(destination))
    if sys.platform == 'darwin':
        if not args.target:
            agent = configure_macos_python(runtime['home'])
            print('PYTHON3HOME=%s set for this session and at login (%s).' % (runtime['home'], agent))
        if current != runtime['home']:
            print('Quit Resolve completely (Cmd+Q) and open it normally from the Dock or Finder.')
    else:
        print('Quit Resolve completely and open it again.')
    print('In Resolve: Workspace > Scripts > Conform Export')
    print('The installed copy is self-contained; you can move/delete the downloaded folder.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('\nINSTALL FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
