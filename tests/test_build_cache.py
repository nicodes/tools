import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).parents[1] / 'helpers/podman-build.py'
spec = importlib.util.spec_from_file_location('cache_builder', HELPER)
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
REF = 'registry.example/team/api:' + 'a' * 40


class CacheBoundaries(unittest.TestCase):
    def invoke(self, directory, arguments=(), **patches):
        with patch.dict(os.environ, {'TOOLS_IMAGE_CACHE': directory}), patch('sys.argv', ['podman-build.py', '--cache-key', 'api', *arguments, '--', '-t', REF, '.']), patch.object(builder.runtime, 'run', **patches) as engine, patch.object(builder.runtime, 'inspect_images', return_value=[{'Id': 'sha256:' + 'b' * 64}]):
            builder.main()
            return engine.call_args

    def test_failed_build_preserves_previous_success_record(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / 'api.json'; previous.write_text('reviewed previous record')
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke(directory, side_effect=subprocess.CalledProcessError(1, 'podman'))
            self.assertEqual(previous.read_text(), 'reviewed previous record')
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ['api.json', 'api.lock'])

    def test_success_uses_native_layers_and_records_config_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            call = self.invoke(directory)
            command = call.args[0]
            self.assertEqual(command[:4], ['build', '--format', 'oci', '--layers'])
            self.assertFalse({'--push', '--load', '--cache-to', '--cache-from'} & set(command))
            self.assertEqual(call.kwargs['timeout'], 3600)
            self.assertEqual(json.loads((Path(directory) / 'api.json').read_text())['images'], {REF: 'sha256:' + 'b' * 64})

    def test_publish_and_path_injection_fail_before_engine(self):
        for arguments in (['--cache-key','../escape','--','.'], ['--cache-key','api','--','--push','.'], ['--cache-key','api','--','--output=type=registry','.']):
            with patch('sys.argv',['podman-build.py',*arguments]), patch.object(builder.runtime,'run') as engine:
                with self.assertRaises(SystemExit): builder.main()
                engine.assert_not_called()

    def test_local_base_requires_exact_source_and_disables_pull(self):
        base = 'registry.example/team/builder:' + 'a' * 40
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TOOLS_IMAGE_CACHE': directory}), patch('sys.argv', ['podman-build.py', '--cache-key', 'api', '--local-base', base, '--', '--build-arg', 'BASE_IMAGE=' + base, '-t', REF, '.']), patch.object(builder.runtime, 'run') as engine, patch.object(builder.runtime, 'inspect_images', return_value=[{'Id': 'sha256:' + 'b' * 64}]) as inspect:
            builder.main()
            self.assertEqual(inspect.call_args_list[0].args, ([base],))
            self.assertIn('--pull=never', engine.call_args.args[0])
