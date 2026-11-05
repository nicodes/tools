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
            self.command(commands, 'docker', 'raise SystemExit(42)\n')
            environment = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                'GATE_IMAGE': 'fixture:test', 'GODOT_BROWSER_REPORTS': 'reports', 'BROWSER_TEST_MODE': 'canonical'}
            for key in ['RUNNER_TEMP', 'TMPDIR', 'GATE_CACHE_IMAGE', 'BROWSER_RUNTIME_IMAGE', 'BROWSER_ROUTE_CYCLES', 'BROWSER_SOAK_SECONDS']:
                environment.pop(key, None)
            if configured:
                temporary = root / 'temporary files'; temporary.mkdir(); environment['RUNNER_TEMP'] = str(temporary)
            else:
                environment['MKTEMP_ARGS'] = str(root / 'mktemp-args')
                self.command(commands, 'mktemp', 'import os,sys\nfrom pathlib import Path\n'
                    'Path(os.environ["MKTEMP_ARGS"]).write_text("\\n".join(sys.argv[1:])+"\\n")\nprint("unused-tls-directory")\n')
            result = subprocess.run(['bash', str(HARNESS)], cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 42, result.stderr)
            if configured:
                children = list(temporary.iterdir())
                self.assertEqual(len(children), 1)
                self.assertTrue(children[0].is_dir())
                self.assertTrue(children[0].name.startswith('godot-browser-tls.'))
            else:
                self.assertEqual((root / 'mktemp-args').read_text(), '-d\n/tmp/godot-browser-tls.XXXXXX\n')

    def test_default_tls_template_uses_the_bounded_fixture_directory(self): self.early_failure(False)
    def test_configured_native_temporary_directory_preserves_spaces(self): self.early_failure(True)

    def run_child_fixture(self, status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); commands = root / 'bin'; commands.mkdir()
            temporary = root / 'temporary files'; temporary.mkdir()
            self.command(commands, 'docker', '''import json,os,sys
from pathlib import Path
with Path(os.environ['CALLS']).open('a') as stream:stream.write(json.dumps(['docker',*sys.argv[1:]])+'\\n')
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
                'GATE_IMAGE': 'fixture:test', 'RUNNER_TEMP': str(temporary), 'GODOT_BROWSER_REPORTS': 'reports with spaces',
                'GODOT_BROWSER_HOSTNAME': 'game.example.test', 'GODOT_BROWSER_TLS_PROXY': 'fixture-proxy.mjs',
                'GODOT_BROWSER_RUNNER': 'journey.mjs', 'GODOT_BROWSER_CACHE_RUNNER': 'cache.mjs',
                'GODOT_BROWSER_STARTUP_RUNNER': 'startup.mjs', 'BROWSER': 'webkit', 'BROWSER_TEST_MODE': 'canonical',
                'CALLS': str(root / 'calls.jsonl'), 'JOURNEY_STATUS': str(status)}
            for key in ['GATE_CACHE_IMAGE', 'BROWSER_RUNTIME_IMAGE', 'BROWSER_ROUTE_CYCLES', 'BROWSER_SOAK_SECONDS', 'BROWSER_STARTUP']:
                environment.pop(key, None)
            result = subprocess.run(['bash', str(HARNESS)], cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, status, result.stderr)
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            started = [row[1] for row in calls if row[0]=='proxy-started']
            stopped = [row[1] for row in calls if row[0]=='proxy-stopped']
            self.assertEqual(len(started), 1)
            self.assertEqual(stopped, started)
            self.assertEqual([row for row in calls if row[:2]==['docker','rm']], [['docker','rm','--force','owned-fixture-gate']])
            self.assertIn(['journey','journey.mjs'], calls)
            self.assertEqual(list(temporary.iterdir()), [])
            self.assertEqual((root / 'reports with spaces/gate.log').read_text(), 'owned fixture log\n')

    def test_success_stops_only_owned_proxy_container_and_tls_files(self): self.run_child_fixture(0)
    def test_assertion_failure_preserves_failure_and_still_cleans_owned_children(self): self.run_child_fixture(42)


if __name__ == '__main__': unittest.main()
