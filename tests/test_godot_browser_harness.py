"""Exercise the shared TLS fixture with native children and no browser/network calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HARNESS = Path(__file__).parents[1] / 'helpers/godot-browser.sh'


@unittest.skipIf(os.name == 'nt', 'native browser fixture runs only on the declared Linux runners')
class BrowserFixtureLifetime(unittest.TestCase):
    def command(self, directory, name, body):
        path = directory / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)
        return path

    def early_failure(self, configured):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); commands = root / 'bin'; commands.mkdir()
            self.command(commands, 'openssl', 'raise SystemExit(42)\n')
            environment = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                'GATE_URL': 'http://127.0.0.1:1', 'GODOT_BROWSER_REPORTS': 'reports', 'BROWSER_TEST_MODE': 'canonical'}
            for key in ['RUNNER_TEMP', 'TMPDIR', 'GATE_CACHE_IMAGE', 'BROWSER_RUNTIME_IMAGE', 'BROWSER_ROUTE_CYCLES', 'BROWSER_SOAK_SECONDS']:
                environment.pop(key, None)
            if configured:
                temporary = root / 'temporary files'; temporary.mkdir(); environment['RUNNER_TEMP'] = str(temporary)
            else:
                environment['MKTEMP_ARGS'] = str(root / 'mktemp-args')
                self.command(commands, 'mktemp', 'import os,sys\nfrom pathlib import Path\n'
                    'Path(os.environ["MKTEMP_ARGS"]).write_text("\\n".join(sys.argv[1:])+"\\n")\nPath("unused-tls-directory").mkdir(mode=0o700)\nprint("unused-tls-directory")\n')
            result = subprocess.run(['bash', str(HARNESS)], cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 42, result.stderr)
            if configured:
                children = list(temporary.iterdir())
                self.assertEqual(children, [])
            else:
                self.assertEqual((root / 'mktemp-args').read_text(), '-d\n/tmp/godot-browser-tls.XXXXXX\n')

    def test_default_tls_template_uses_the_bounded_fixture_directory(self): self.early_failure(False)
    def test_configured_native_temporary_directory_preserves_spaces(self): self.early_failure(True)

    def run_child_fixture(self, status, native_url=True, cache=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); commands = root / 'bin'; commands.mkdir()
            temporary = root / 'temporary files'; temporary.mkdir()
            self.command(commands, 'podman', '''import json,os,sys
from pathlib import Path
assert sys.argv[1]=='--remote=false'
with Path(os.environ['CALLS']).open('a') as stream:stream.write(json.dumps(['podman',*sys.argv[2:]])+'\\n')
sys.argv.pop(1)
if sys.argv[1]=='run':print('owned-fixture-gate')
elif sys.argv[1]=='port':print('127.0.0.1:1')
elif sys.argv[1]=='logs':print('owned fixture log')
''')
            self.command(commands, 'openssl', '''from pathlib import Path
import sys
if sys.argv[1]=='req':
 for flag in ['-keyout','-out']:Path(sys.argv[sys.argv.index(flag)+1]).write_bytes(b'owned TLS fixture')
else:
 sys.stdin.buffer.read();print('c3BraQ==')
''')
            self.command(commands, 'curl', 'raise SystemExit(0)\n')
            self.command(commands, 'node', '''import json,os,signal,sys,time
from pathlib import Path
log=Path(os.environ['CALLS'])
def record(value):
 with log.open('a') as stream:stream.write(json.dumps(value)+'\\n')
if sys.argv[1]=='fixture-proxy.mjs':
 def stop(*_args):record(['proxy-stopped',os.getpid()]);raise SystemExit(0)
 signal.signal(signal.SIGTERM,stop)
 record(['proxy-started',os.getpid()]);Path(sys.argv[4]).write_text('1')
 while True:time.sleep(.05)
else:
 record(['journey',sys.argv[1]]);raise SystemExit(int(os.environ['JOURNEY_STATUS']))
''')
            environment = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                'GATE_URL': 'http://127.0.0.1:1', 'RUNNER_TEMP': str(temporary), 'GODOT_BROWSER_REPORTS': 'reports with spaces',
                'GODOT_BROWSER_HOSTNAME': 'game.example.test', 'GODOT_BROWSER_TLS_PROXY': 'fixture-proxy.mjs',
                'GODOT_BROWSER_RUNNER': 'journey.mjs', 'GODOT_BROWSER_CACHE_RUNNER': 'cache.mjs',
                'GODOT_BROWSER_STARTUP_RUNNER': 'startup.mjs', 'BROWSER': 'webkit', 'BROWSER_TEST_MODE': 'canonical',
                'CALLS': str(root / 'calls.jsonl'), 'JOURNEY_STATUS': str(status)}
            if native_url:
                environment['GATE_URL']='http://127.0.0.1:1'
                if cache:environment['GATE_CACHE_URL']='http://127.0.0.1:2'
            for key in ['GATE_CACHE_IMAGE', 'BROWSER_RUNTIME_IMAGE', 'BROWSER_ROUTE_CYCLES', 'BROWSER_SOAK_SECONDS', 'BROWSER_STARTUP']:
                environment.pop(key, None)
            result = subprocess.run(['bash', str(HARNESS)], cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, status, result.stderr)
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            started = [row[1] for row in calls if row[0]=='proxy-started']
            stopped = [row[1] for row in calls if row[0]=='proxy-stopped']
            self.assertEqual(len(started), 1)
            self.assertEqual(stopped, started)
            self.assertEqual([row for row in calls if row[:2]==['podman','rm']], [] if native_url else [['podman','rm','--force','owned-fixture-gate']])
            if native_url:self.assertFalse(any(row[0]=='podman' for row in calls))
            self.assertIn(['journey','cache.mjs' if cache else 'journey.mjs'], calls)
            self.assertEqual(list(temporary.iterdir()), [])
            if native_url:self.assertFalse((root/'reports with spaces/gate.log').exists())
            else:self.assertEqual((root / 'reports with spaces/gate.log').read_text(), 'owned fixture log\n')

    def test_success_stops_only_owned_proxy_container_and_tls_files(self): self.run_child_fixture(0)
    def test_assertion_failure_preserves_failure_and_still_cleans_owned_children(self): self.run_child_fixture(42)

    def test_native_url_fixture_starts_no_shipping_container_and_retains_tls_cleanup(self):
        self.run_child_fixture(0,native_url=True)
        self.run_child_fixture(42,native_url=True)

    def test_native_cache_url_uses_the_cache_journey_without_starting_gate_images(self):
        self.run_child_fixture(0,native_url=True,cache=True)

    def test_retention_checks_actual_descriptor_and_source_before_any_process(self):
        with tempfile.TemporaryDirectory() as directory:
            import hashlib
            root=Path(directory);descriptor=root/'release.json'
            descriptor.write_text(json.dumps({'source':{'revision':'a'*40}},separators=(',',':')))
            digest='sha256:'+hashlib.sha256(descriptor.read_bytes()).hexdigest()
            environment={**os.environ,'PATH':'/usr/bin:/bin','GATE_URL':'http://127.0.0.1:1','GATE_DESCRIPTOR_FILE':str(descriptor),
                'EXPECTED_DESCRIPTOR_DIGEST':digest,'EXPECTED_SOURCE_COMMIT':'b'*40,'BROWSER_TEST_MODE':'retention'}
            result=subprocess.run(['bash',str(HARNESS)],cwd=root,env=environment,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('descriptor source mismatch',result.stderr)
            environment['EXPECTED_SOURCE_COMMIT']='a'*40;environment['EXPECTED_DESCRIPTOR_DIGEST']='sha256:'+'0'*64
            result=subprocess.run(['bash',str(HARNESS)],cwd=root,env=environment,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('descriptor identity mismatch',result.stderr)
            self.assertFalse((root/'reports').exists())


if __name__ == '__main__': unittest.main()
