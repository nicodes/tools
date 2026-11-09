import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('image_user', Path(__file__).parents[1]/'helpers/image_user.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
CONFIG = 'sha256:'+'a'*64
CID = 'b'*64
PASSWD = b'root:x:0:0:root:/root:/bin/sh\npostgres:x:70:71:PostgreSQL:/var/lib/postgresql:/bin/sh\n'


def archive(content=PASSWD, name='passwd', kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as stream:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.size = len(content) if kind == tarfile.REGTYPE else 0
        stream.addfile(entry, io.BytesIO(content) if entry.size else None)
    return output.getvalue()


class ImageUser(unittest.TestCase):
    def test_account_table_requires_one_exact_nonroot_uid_and_gid(self):
        self.assertEqual(reader.passwd_identity(PASSWD), (70, 71))
        for raw in [PASSWD+PASSWD, b'root:x:0:0::/:/bin/sh\n', PASSWD.replace(b':70:', b':0:'),
                    PASSWD.replace(b':71:', b':0:'), PASSWD.replace(b':70:', b':070:'),
                    PASSWD.replace(b'postgres:', b'postgres\0:'), b'bad-row\n'+PASSWD,
                    PASSWD.replace(b':71:', b':4294967295:'), b'\xff', b'x'*65537]:
            with self.assertRaises(ValueError): reader.passwd_identity(raw)

    def test_account_archive_refuses_extra_links_paths_and_truncation(self):
        self.assertEqual(reader.archive_identity(archive(), 'postgres'), (70, 71))
        for raw in [archive(name='../passwd'), archive(kind=tarfile.SYMTYPE), archive(kind=tarfile.LNKTYPE),
                    archive(name='/etc/passwd'), archive()[:200], b'x'*131073]:
            with self.assertRaises(ValueError): reader.archive_identity(raw, 'postgres')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as stream:
            for name in ['passwd', 'foreign']:
                entry = tarfile.TarInfo(name); entry.size = len(PASSWD)
                stream.addfile(entry, io.BytesIO(PASSWD))
        with self.assertRaises(ValueError): reader.archive_identity(output.getvalue(), 'postgres')

    def inspection(self, *, failure=None):
        calls = []; state = {}; refused = False
        def capture(native, args, environment, deadline, limit=1 << 20):
            nonlocal refused
            calls.append(args)
            if args[0] == 'create':
                state.update(Id=CID, Name=args[args.index('--name')+1], Image=CONFIG,
                    Config={'Labels': {'tools.image-user': args[args.index('--label')+1].split('=', 1)[1]},
                            'User': '65534:65534', 'Entrypoint': '/bin/false' if failure == 'native-string-entrypoint' else ['/bin/false']},
                    State={'Running': False, 'Pid': 0, 'Status': 'configured'}, Mounts=[])
                if failure == 'lost-ack': return 125, b''
                return 0, (CID+'\n').encode()
            if args[:2] == ['container', 'exists']: return (0 if state else 1), b''
            if args[:2] == ['container', 'inspect']:
                if failure == 'foreign': state['Config']['Labels']['tools.image-user'] = 'foreign'
                return 0, json.dumps([state]).encode()
            if args[0] == 'cp':
                if failure == 'changed': state['Image'] = 'sha256:'+'c'*64
                return 0, archive(name='../passwd') if failure == 'archive' else archive()
            if args == ['rm', CID]: state.clear(); return 0, b''
            refused = True
            raise AssertionError(args)
        return capture, calls, state, lambda: refused

    def test_stopped_config_only_probe_cleans_up_without_start_pull_or_image_mutation(self):
        capture, calls, state, refused = self.inspection()
        with patch.object(reader, 'capture', side_effect=capture):
            self.assertEqual(reader.identity(CONFIG), (70, 71))
        self.assertFalse(state); self.assertFalse(refused())
        create = calls[0]
        self.assertEqual(create[-1], CONFIG)
        for flag in ['--pull=never', '--network=none', '--read-only', '--image-volume=ignore', '--cap-drop=ALL']:
            self.assertIn(flag, create)
        self.assertEqual({args[0] for args in calls}, {'create', 'container', 'cp', 'rm'})
        self.assertEqual(calls[-2:], [['rm', CID], ['container', 'exists', create[create.index('--name')+1]]])

    def test_lost_ack_and_invalid_passwd_still_remove_only_the_exact_owned_stopped_container(self):
        for failure in ['lost-ack', 'archive']:
            capture, calls, state, _ = self.inspection(failure=failure)
            with patch.object(reader, 'capture', side_effect=capture), self.assertRaises((ValueError, RuntimeError)):
                reader.identity(CONFIG)
            self.assertFalse(state)
            self.assertIn(['rm', CID], calls)

    def test_supported_native_entrypoint_shapes_require_the_same_single_literal_program(self):
        capture, calls, state, _ = self.inspection(failure='native-string-entrypoint')
        with patch.object(reader, 'capture', side_effect=capture):
            self.assertEqual(reader.identity(CONFIG), (70, 71))
        self.assertFalse(state)

    def test_changed_ownership_preserves_object_and_private_intent_for_explicit_recovery(self):
        for failure in ['foreign', 'changed']:
            capture, calls, state, _ = self.inspection(failure=failure)
            with tempfile.TemporaryDirectory() as root:
                private = Path(root)/'inspection'; private.mkdir(mode=0o700)
                with patch.object(reader.tempfile, 'mkdtemp', return_value=str(private)), \
                        patch.object(reader, 'capture', side_effect=capture), self.assertRaises(RuntimeError):
                    reader.identity(CONFIG)
                self.assertTrue(state)
                self.assertFalse(any(args[0] == 'rm' for args in calls))
                intent = private/'intent.json'
                self.assertEqual(intent.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(intent.read_text())['config_digest'], CONFIG)

    def test_mutable_selector_or_unbounded_deadline_refuses_before_native_effects(self):
        with patch.object(reader, 'capture') as execute:
            for config, timeout in [('image:tag', 60), ('a'*64, 60), (CONFIG, 0), (CONFIG, True), (CONFIG, float('inf'))]:
                with self.assertRaises(ValueError): reader.identity(config, timeout=timeout)
            execute.assert_not_called()

    def test_capture_enforces_byte_and_wall_clock_limits_on_actual_subprocesses(self):
        def native(program):
            return SimpleNamespace(command=lambda *args, **kwargs: ([sys.executable, '-I', '-B', '-c', program], dict(os.environ)),
                                   anonymous_authfile=lambda: None)
        with self.assertRaises(ValueError):
            reader.capture(native('import os; os.write(1,b"x"*100000)'), ['unused'], {}, time.monotonic()+5, 64)
        with self.assertRaises(TimeoutError):
            reader.capture(native('import time; time.sleep(5)'), ['unused'], {}, time.monotonic()+.1)
        self.assertEqual(reader.capture(native('print("ok")'), ['unused'], {}, time.monotonic()+5), (0, b'ok\n'))


if __name__ == '__main__':
    unittest.main()
