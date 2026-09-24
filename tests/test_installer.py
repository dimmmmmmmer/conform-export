from pathlib import Path
import platform
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import installer

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_platform_locations(self):
        home = Path('/users/test')
        mac = installer.locations('darwin', home, {})
        windows = installer.locations('win32', home, {'APPDATA': '/roaming'})
        linux = installer.locations('linux', home, {})
        self.assertEqual(mac[1], home/'Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility')
        self.assertEqual(windows[1], Path('/roaming/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility'))
        self.assertEqual(linux[1], home/'.local/share/DaVinciResolve/Fusion/Scripts/Utility')

    def test_install_and_update_keep_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, menu = root/'payload', root/'menu'
            destination = installer.install(ROOT, payload, menu)
            self.assertTrue((payload/'conform/fcp7.py').is_file())
            written = lambda p: repr(str(p))[1:-1]  # as it appears inside the launcher's string literal
            self.assertIn(written(payload), destination.read_text())
            self.assertNotIn(written(ROOT), destination.read_text())
            installer.install(ROOT, payload, menu)
            self.assertEqual(len(list(root.glob('payload.backup-*'))), 1)
            installer.install(ROOT, payload, menu)
            self.assertEqual(len(list(root.glob('payload.backup-*'))), 1)

    def test_preserves_unrelated_script(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); menu = root/'menu';menu.mkdir()
            destination = menu/installer.ENTRY; destination.write_text('user script')
            with self.assertRaises(RuntimeError): installer.install(ROOT, root/'payload', menu)
            self.assertEqual(destination.read_text(), 'user script')

    def test_rolls_back_failed_menu_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);payload=root/'payload';menu=root/'menu'
            destination=installer.install(ROOT,payload,menu)
            old=destination.read_bytes()
            (payload/'keep.txt').write_text('previous installation')
            with patch('installer.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError): installer.install(ROOT,payload,menu)
            self.assertEqual(destination.read_bytes(),old)
            self.assertEqual((payload/'keep.txt').read_text(),'previous installation')

    def test_missing_interpreter_probe(self):
        self.assertIsNone(installer.probe('/does-not-exist/python'))

    def test_probe_rejects_foreign_architecture(self):
        info = installer.probe(sys.executable, platform.machine())
        if info is None:
            self.skipTest('test interpreter is outside the supported version range')
        self.assertEqual(info['machine'], platform.machine())
        self.assertIsNone(installer.probe(sys.executable, 'not-this-cpu'))

    def test_macos_python_home_persists_for_gui_apps(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            path = installer.configure_macos_python('/runtime', temp, lambda cmd, **kw: calls.append(cmd))
            agent = plistlib.loads(path.read_bytes())
        self.assertEqual(path, Path(temp) / 'Library/LaunchAgents/conform-export.python3home.plist')
        self.assertTrue(agent['RunAtLoad'])
        self.assertEqual(agent['ProgramArguments'], ['/bin/launchctl', 'setenv', 'PYTHON3HOME', '/runtime'])
        self.assertEqual(calls, [['/bin/launchctl', 'setenv', 'PYTHON3HOME', '/runtime']])

    def test_existing_old_pointer_launcher_can_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);menu=root/'menu';menu.mkdir()
            (menu/installer.ENTRY).write_text(installer.MARKER+'\n# old pointer')
            installer.install(ROOT,root/'payload',menu)
            self.assertIn('runpy.run_path',(menu/installer.ENTRY).read_text())


if __name__ == '__main__': unittest.main()
