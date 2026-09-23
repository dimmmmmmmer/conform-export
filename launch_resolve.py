"""Windows/Linux: launch Resolve so its script engine finds the selected Python.
On macOS the installer sets PYTHON3HOME for apps started normally. Never stops a running app."""
import json
import os
from pathlib import Path
import subprocess
import sys


def resolve_executable(platform, env):
    if platform == 'win32':
        return Path(env.get('PROGRAMFILES', r'C:\Program Files')) / 'Blackmagic Design/DaVinci Resolve/Resolve.exe'
    return Path('/opt/resolve/bin/resolve')


def running(platform):
    if platform == 'win32':
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Resolve.exe', '/FO', 'CSV', '/NH'], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Could not check whether Resolve is running')
        return 'resolve.exe' in result.stdout.lower()
    result = subprocess.run(['pgrep', '-x', 'resolve'], capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Could not check whether Resolve is running')
    return result.returncode == 0


def environment(config, original):
    # Resolve's fusionscript reads PYTHON3HOME. PYTHONHOME is deliberately not
    # set: it would leak into every Python process Resolve starts.
    result = dict(original)
    result['PYTHON3HOME'] = config['home']
    result['PATH'] = str(Path(config['executable']).parent) + os.pathsep + result.get('PATH', '')
    return result


def start_process(executable, config, log_path):
    env = environment(config, os.environ)
    # Detached apps must not inherit Terminal's input descriptor.
    with open(log_path, 'ab') as log:
        check = subprocess.run(
            [config['executable'], '-c',
             'import sys; print("Conform Export Python probe:", sys.version, sys.prefix, flush=True)'],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, timeout=15)
        if check.returncode:
            raise RuntimeError('Python startup check failed; see resolve-launch.log')
        return subprocess.Popen(
            [str(executable)], env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, cwd=str(executable.parent),
            start_new_session=True)


def main():
    if sys.platform == 'darwin':
        raise RuntimeError('Not needed on macOS: run Install, then open Resolve normally.')
    root = Path(__file__).resolve().parent
    config_path = root / 'runtime.json'
    if not config_path.is_file():
        raise RuntimeError('Run Install first to select an available Python runtime.')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    executable = Path(config.get('resolve_path') or resolve_executable(sys.platform, os.environ))
    if not executable.is_file():
        raise RuntimeError('Resolve executable not found: %s. Reinstall with --resolve PATH.' % executable)
    if not Path(config['executable']).is_file():
        raise RuntimeError('Selected Python has moved. Run Install again.')
    if running(sys.platform):
        raise RuntimeError('Resolve is still running. Save your work, quit Resolve, then run Start Resolve again.')
    print('Starting Resolve with Python ' + '.'.join(map(str, config['version'])))
    process = start_process(executable, config, root / 'resolve-launch.log')
    (root / 'launch-status.json').write_text(json.dumps(dict(started=True, pid=process.pid, python=config['executable'], home=config['home']), indent=2), encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        (Path(__file__).resolve().parent / 'launch-status.json').write_text(json.dumps(dict(started=False, error=str(exc)), indent=2), encoding='utf-8')
        print('START FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
