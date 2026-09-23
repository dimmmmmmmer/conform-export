from pathlib import Path
import json
import platform
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import installer
import launch_resolve

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = dict(executable='/runtime/python3', home='/runtime', version=[3,12,7], bits=64)


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
            destination = installer.install(ROOT, payload, menu, RUNTIME)
            self.assertTrue((payload/'conform/fcp7.py').is_file())
            self.assertTrue((payload/'runtime.json').is_file())
            self.assertIn(str(payload), destination.read_text())
            self.assertNotIn(str(ROOT), destination.read_text())
            installer.install(ROOT, payload, menu, RUNTIME)
            self.assertEqual(len(list(root.glob('payload.backup-*'))), 1)
            installer.install(ROOT, payload, menu, RUNTIME)
            self.assertEqual(len(list(root.glob('payload.backup-*'))), 1)

    def test_preserves_unrelated_script(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); menu = root/'menu';menu.mkdir()
            destination = menu/installer.ENTRY; destination.write_text('user script')
            with self.assertRaises(RuntimeError): installer.install(ROOT, root/'payload', menu, RUNTIME)
            self.assertEqual(destination.read_text(), 'user script')

    def test_rolls_back_failed_menu_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);payload=root/'payload';menu=root/'menu'
            destination=installer.install(ROOT,payload,menu,RUNTIME)
            old=destination.read_bytes()
            (payload/'keep.txt').write_text('previous installation')
            with patch('installer.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError): installer.install(ROOT,payload,menu,RUNTIME)
            self.assertEqual(destination.read_bytes(),old)
            self.assertEqual((payload/'keep.txt').read_text(),'previous installation')

    def test_runtime_environment_is_process_local(self):
        old={'PATH':'/usr/bin','OTHER':'value'}
        new=launch_resolve.environment(RUNTIME,old)
        self.assertNotIn('PYTHONHOME',old)
        self.assertEqual(new['PYTHON3HOME'],'/runtime')
        self.assertEqual(new['OTHER'],'value')

    def test_legacy_install_is_removed_but_foreign_files_stay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); menu = root/'menu'; menu.mkdir()
            old = root/'RenderNamedTimelineExporter'; old.mkdir(); (old/'.rnte-install.json').write_text('{}')
            foreign = root/'RenderNamedTimelineExporter.backup-x'; foreign.mkdir()
            (menu/'Render Named Timeline Exporter.py').write_text(installer.OLD_MARKER + '\n# old')
            removed = installer.remove_legacy(root/installer.APP, menu)
            self.assertEqual(len(removed), 2)
            self.assertFalse(old.exists())
            self.assertTrue(foreign.exists())

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

    @unittest.skipIf(sys.platform == 'win32', 'POSIX executable fixture')
    def test_detached_child_has_usable_standard_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / 'resolve'
            fake.write_text('#!' + sys.executable + '\n'
                            'import sys,json\n'
                            'assert sys.stdin.read() == ""\n'
                            'print(json.dumps({"streams": "ok"}), flush=True)\n'
                            'print("stderr ok", file=sys.stderr, flush=True)\n')
            fake.chmod(0o755)
            config = dict(executable=sys.executable, home=sys.base_prefix)
            process = launch_resolve.start_process(fake, config, root / 'log')
            self.assertEqual(process.wait(timeout=15), 0)
            result = (root / 'log').read_text()
            self.assertIn('Conform Export Python probe:', result)
            self.assertIn('"streams": "ok"', result)
            self.assertIn('stderr ok', result)

    def test_existing_old_pointer_launcher_can_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);menu=root/'menu';menu.mkdir()
            (menu/installer.ENTRY).write_text(installer.MARKER+'\n# old pointer')
            installer.install(ROOT,root/'payload',menu,RUNTIME)
            self.assertIn('runpy.run_path',(menu/installer.ENTRY).read_text())


if __name__ == '__main__': unittest.main()
