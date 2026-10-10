"""Check caller-owned naming definitions while ignoring prose and unrelated roles."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vendored import skip_module_if_vendored

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'helpers/fleet-baseline.mjs'

skip_module_if_vendored(globals())


def problems(files, product='nicodes/sampleapp-be'):
    """Call the real namingProblems() through bun, so the tested code ships."""
    with tempfile.TemporaryDirectory() as directory:
        for relative, text in files.items():
            path = Path(directory)/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        program = (
            f'const m = await import({json.dumps(str(HELPER))});'
            'const [root, product] = JSON.parse(process.argv[1]);'
            'console.log(JSON.stringify(m.namingProblems(root, product)));'
        )
        result = subprocess.run(['bun', '--eval', program, json.dumps([directory, product])],
                                capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


CANONICAL = {
    'scripts/build.sh': 'export EXPO_PUBLIC_API_URL="https://api.sampleapp.example.test"\n',
    'deploy/compose.yml':
        'services:\n  api:\n    environment:\n      ALLOWED_ORIGINS: "https://app.sampleapp.example.test"\n',
    '.github/workflows/cd.yml':
        'jobs:\n  d:\n    steps:\n      - env:\n          KOMIZO_SECRET_RUNTIME_DATABASE_URL: x\n',
}


class NamingContract(unittest.TestCase):
    def test_the_fleet_names_pass(self):
        # Including through KOMIZO_SECRET_, which is how CD hands a setting to
        # a host: the prefix is transport, not part of the name.
        self.assertEqual(problems(CANONICAL), [])

    def test_every_product_prefixed_spelling_is_caught(self):
        divergent = {
            'scripts/build.sh': 'export EXPO_PUBLIC_SAMPLEAPP_API_BASE="https://api.sampleapp.example.test"\n',
            'deploy/compose.yml':
                'services:\n  api:\n    environment:\n      SAMPLEAPP_WEB_ORIGINS: "https://app.sampleapp.example.test"\n',
            '.github/workflows/cd.yml':
                'jobs:\n  d:\n    steps:\n      - env:\n          KOMIZO_SECRET_SAMPLEAPP_DATABASE_URL: x\n',
        }
        self.assertEqual(len(problems(divergent)), 3)

    def test_operator_roles_are_the_products_own_business(self):
        # Only the SERVING slot is the fleet's. MIGRATOR and OWNER are real
        # DSNs in sampleservice and sampleconsole, with different credentials and different
        # blast radius; the first version of this check flagged both, which is
        # how a rule that needs a growing exception list announces itself.
        roles = {'Makefile': 'MIGRATOR_DATABASE_URL=x\nOWNER_DATABASE_URL=y\nWORKER_DATABASE_URL=z\n'}
        self.assertEqual(problems(roles), [])

    def test_a_comment_naming_the_old_spelling_is_history(self):
        # Several products deliberately record the old name in a comment
        # explaining why it changed. Matching prose would fail exactly the
        # repositories that did the work.
        prose = {
            'scripts/build.sh':
                '# used to be EXPO_PUBLIC_SAMPLEAPP_API_BASE\nexport EXPO_PUBLIC_API_URL="x"\n',
            'deploy/compose.yml':
                'services:\n  api:\n    environment:\n      # was SAMPLEAPP_WEB_ORIGINS\n      ALLOWED_ORIGINS: "y"\n',
        }
        self.assertEqual(problems(prose), [])

    def test_another_products_prefix_is_not_this_products_problem(self):
        other = {'scripts/build.sh': 'export EXPO_PUBLIC_SAMPLE_ADDON_API_URL="x"\n'}
        self.assertEqual(problems(other), [])


if __name__ == '__main__':
    unittest.main()
