import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_runtime', Path(__file__).parents[1] / 'helpers/container_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class NativeRuntime(unittest.TestCase):
    def test_bind_mount_ownership_maps_to_the_exact_nonroot_image_account(self):
        with patch.object(runtime.os, 'geteuid', return_value=1001):
            self.assertEqual(runtime.bind_mount_user_namespace(70, 71), ['--userns=keep-id:uid=70,gid=71'])
            self.assertEqual(runtime.bind_mount_user_namespace(), ['--userns=keep-id'])
        with patch.object(runtime.os, 'geteuid', return_value=0):
            self.assertEqual(runtime.bind_mount_user_namespace(70, 71), [])
        for uid, gid in [(None, 70), (70, None), (0, 70), (70, 0), (True, 70), ('70', 70), (2**32, 70)]:
            with self.assertRaises(ValueError): runtime.bind_mount_user_namespace(uid, gid)

    def test_remote_selectors_cannot_redirect_execution(self):
        source = {key: 'foreign-endpoint' for key in runtime.REMOTE_ENVIRONMENT}
        source.update(PATH='/native/bin', XDG_RUNTIME_DIR='/owned/runtime')
        args, values = runtime.command(['image', 'inspect', 'example.invalid/app:source'], source)
        self.assertEqual(args[:3], ['podman', '--remote=false', 'image'])
        self.assertFalse(runtime.REMOTE_ENVIRONMENT & values.keys())
        self.assertEqual(values['XDG_RUNTIME_DIR'], '/owned/runtime')
        self.assertEqual(source['CONTAINER_HOST'], 'foreign-endpoint')

    def test_registry_operations_use_only_explicit_private_authfile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.json'
            path.write_text('{"auths":{}}')
            path.chmod(0o600)
            args, values = runtime.command(['pull', 'registry.invalid/app@sha256:' + 'a' * 64], {}, authfile=path)
            self.assertEqual(args[2:5], ['pull', '--authfile', str(path)])
            self.assertEqual(values['REGISTRY_AUTH_FILE'], str(path))

    def test_ambient_auth_is_not_used_when_anonymous(self):
        _, values = runtime.command(['pull', 'registry.invalid/app:source'], {})
        path = Path(values['REGISTRY_AUTH_FILE'])
        self.assertEqual(json.loads(path.read_text()), {'auths': {}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_authfile_rejects_permissions_links_and_credential_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'auth.json'
            path.write_text('{"auths":{}}')
            for mode in [0o644, 0o640]:
                path.chmod(mode)
                with self.assertRaises(ValueError):
                    runtime.command(['pull', 'image'], {}, authfile=path)
            path.chmod(0o600)
            link = root / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                runtime.command(['pull', 'image'], {}, authfile=link)
            path.write_text('{"auths":{},"credHelpers":{"registry.invalid":"foreign"}}')
            with self.assertRaises(ValueError):
                runtime.command(['pull', 'image'], {}, authfile=path)

    def test_credential_and_global_options_are_not_caller_arguments(self):
        for args in [['--remote', 'true'], ['pull', '--creds=user:secret', 'image'], ['build', '--authfile', '/foreign', '.']]:
            with self.assertRaises(ValueError):
                runtime.command(args, {})

    def test_container_program_options_are_not_registry_authority(self):
        arguments=['exec','a'*64,'pg_restore','--username=restore_owner','--dbname=synthetic','/recovery/database.dump']
        argv,_=runtime.command(arguments,{})
        self.assertEqual(argv,['podman','--remote=false',*arguments])
        self.assertNotIn('--authfile',argv)

    def test_native_image_config_identity_is_normalized_without_manifest_confusion(self):
        image = {'Id': 'a' * 64, 'Digest': 'sha256:' + 'b' * 64,
                 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'Labels': None},
                 'RootFS': {'Type': 'layers', 'Layers': ['sha256:' + 'c' * 64]},
                 'RepoTags': ['registry.invalid/app:source'], 'RepoDigests': []}
        with patch.object(runtime, 'output', return_value=json.dumps([image])):
            value = runtime.inspect_images(['registry.invalid/app:source'], environment={})[0]
        self.assertEqual(value['Id'], 'sha256:' + 'a' * 64)
        self.assertEqual(value['Digest'], 'sha256:' + 'b' * 64)
        self.assertEqual(value['Config']['Labels'], {})

    def test_ambiguous_or_incomplete_metadata_refuses(self):
        for body in ['[{"Id":"a","Id":"b"}]', '[]', '[{"Id":"' + 'a' * 64 + '"}]']:
            with patch.object(runtime, 'output', return_value=body):
                with self.assertRaises(ValueError):
                    runtime.inspect_images(['image'], environment={})

    def test_runtime_keeps_callers_environment_and_deadline_explicit(self):
        with patch.object(runtime.subprocess, 'run') as execute:
            runtime.run(['ps', '--all'], environment={'PATH': '/native'}, timeout=7, check=True)
            arguments = execute.call_args
        self.assertEqual(arguments.args[0], ['podman', '--remote=false', 'ps', '--all'])
        self.assertEqual(arguments.kwargs['timeout'], 7)
        self.assertEqual(arguments.kwargs['env']['PATH'], '/native')
        for timeout in [0, -1, float('inf')]:
            with self.assertRaises(ValueError):
                runtime.run(['ps'], environment={}, timeout=timeout)


if __name__ == '__main__':
    unittest.main()
