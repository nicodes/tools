import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


class TemplateGate(unittest.TestCase):
    def test_both_archetypes_run_complete_gate_and_propagate_each_failure(self):
        templates = Path(__file__).parents[1] / 'templates'
        for archetype in ['full-stack', 'app-only']:
            with self.subTest(archetype=archetype), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy2(templates / archetype / 'Makefile', root / 'Makefile')
                (root / 'scripts').mkdir()
                (root / 'bin').mkdir()
                mise = root / 'bin/mise'
                mise.write_text('#!/bin/sh\n[ "$1" = exec ] && [ "$2" = -- ] || exit 99\nshift 2\nexec "$@"\n')
                mise.chmod(0o700)
                env = {**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH']}

                def make(*args, **extra):
                    return subprocess.run(['make', '--no-print-directory', *args], cwd=root,
                                          env={**env, **extra}, capture_output=True, text=True, timeout=20)

                self.assertEqual(make().returncode, 0)
                self.assertFalse((root / 'ran').exists(), 'default must only display help')
                self.assertNotEqual(make('check').returncode, 0, 'missing product implementation fails closed')
                for stage in ['test', 'vuln', 'build', 'e2e']:
                    (root / 'scripts' / (stage + '.sh')).write_text(
                        '#!/bin/bash\nset -eu\n'
                        f'echo {stage} >> ran\n'
                        f'[ "${{FAIL_STAGE:-}}" != {stage} ]\n')
                self.assertEqual(make('check').returncode, 0)
                self.assertEqual((root / 'ran').read_text().splitlines(), ['test', 'vuln', 'build', 'e2e'])
                for stage in ['test', 'vuln', 'build', 'e2e']:
                    self.assertNotEqual(make('check', FAIL_STAGE=stage).returncode, 0, stage)
                (root / 'ran').unlink()
                self.assertEqual(make('e2e').returncode, 0)
                self.assertEqual((root / 'ran').read_text().splitlines(), ['build', 'e2e'])
                state = root / 'pb/pb_data/keep.db'
                state.parent.mkdir(parents=True)
                state.write_text('local development state')
                self.assertEqual(make('clean').returncode, 0)
                self.assertTrue(state.exists())


def composite_run_scripts(text):
    """Extract the `run:` bodies of a composite action, in order.

    Minimal block-scalar handling so the template's assertions execute
    without a YAML dependency: `run: <line>` yields one script, `run: |`
    yields the following more-indented lines with their common indent
    stripped.
    """
    scripts = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1
        if stripped in ('run:', 'run: |'):
            indent = len(line) - len(line.lstrip())
            body = []
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                    break
                body.append(candidate)
                index += 1
            indents = [len(b) - len(b.lstrip()) for b in body if b.strip()]
            common = min(indents)
            scripts.append('\n'.join(b[common:] for b in body).strip('\n'))
        elif stripped.startswith('run: '):
            scripts.append(stripped[len('run: '):])
    return scripts


class StaticWebTemplateGate(unittest.TestCase):
    """The static-web archetype has no Makefile; its gate is the pair of
    composite actions ci.yml calls. The test action is exercised end to end:
    its steps must fail closed on every broken shape of ./dist and pass only
    a complete one."""

    def setUp(self):
        self.template = Path(__file__).parents[1] / 'templates' / 'static-web'

    def run_test_action(self, files):
        action = self.template / 'actions' / 'test' / 'action.yml'
        scripts = composite_run_scripts(action.read_text())
        self.assertGreaterEqual(len(scripts), 2, 'common assertions must ship with the archetype')
        # The biome step runs the adopter's installed devDependency against
        # its real source tree; a fixture has neither node_modules nor a
        # biome.json, so it is asserted structurally below rather than
        # executed here.
        scripts = [script for script in scripts if 'biome' not in script]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            for script in scripts:
                # Composite bash steps run with errexit and pipefail.
                result = subprocess.run(['bash', '-eo', 'pipefail', '-c', script], cwd=root,
                                        capture_output=True, text=True, timeout=20)
                if result.returncode != 0:
                    return result
        return result

    def test_missing_dist_fails(self):
        self.assertNotEqual(self.run_test_action({}).returncode, 0)

    def test_empty_index_fails(self):
        self.assertNotEqual(self.run_test_action({'dist/index.html': ''}).returncode, 0)

    def test_shipped_javascript_fails(self):
        result = self.run_test_action({'dist/index.html': '<html></html>',
                                       'dist/assets/app.js': 'console.log(1)'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('JavaScript', result.stderr + result.stdout)

    def test_complete_dist_passes(self):
        result = self.run_test_action({'dist/index.html': '<html></html>',
                                       'dist/assets/site.css': 'body{}'})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_biome_config_parses_and_pins_the_canonical_version(self):
        config = json.loads((self.template / 'biome.json').read_text())
        self.assertIn('/2.5.14/', config['$schema'],
                      'the schema URL records the pinned biome version')
        self.assertTrue(config['linter']['enabled'])
        self.assertTrue(config['formatter']['enabled'])
        self.assertEqual(config['linter']['rules'], {'preset': 'recommended'},
                         'biome 2.5 deprecates the recommended key in favor of preset')
        astro = [o for o in config['overrides'] if o['includes'] == ['**/*.astro']]
        self.assertEqual(len(astro), 1, 'exactly one Astro pragmatism override')
        correctness = astro[0]['linter']['rules']['correctness']
        self.assertEqual(correctness, {'noUnusedVariables': 'off', 'noUnusedImports': 'off'},
                         'Astro frontmatter references make template imports look unused; '
                         'biome\'s Unsafe fix would break pages, so both rules stay off')

    def test_biome_check_is_wired_into_the_test_gate(self):
        action = (self.template / 'actions' / 'test' / 'action.yml').read_text()
        scripts = composite_run_scripts(action)
        biome = [script for script in scripts if 'biome' in script]
        self.assertEqual(len(biome), 1, 'exactly one canonical biome step')
        self.assertEqual(biome[0], 'bun x biome check',
                         'the bun channel resolves the product-pinned devDependency')
        self.assertLess(action.index('bun x biome check'), action.index('dist/index.html'),
                        'the source assertion runs before the ./dist assertions')

    def test_ci_workflow_is_two_independent_jobs_building_before_testing(self):
        ci = (self.template / 'ci.yml').read_text()
        self.assertNotIn('needs:', ci, 'Build and Test stay independent')
        self.assertEqual(ci.count('\n    name: Build\n'), 1, 'exactly one Build job')
        self.assertEqual(ci.count('\n    name: Test\n'), 1, 'exactly one Test job')
        self.assertEqual(ci.count('- name: Build'), 2, 'each job builds in its own workspace')
        self.assertEqual(ci.count('uses: ./.github/actions/build'), 2)
        self.assertEqual(ci.count('uses: ./.github/actions/test'), 1)
        test_job = ci[ci.index('\n  test:'):]
        self.assertLess(test_job.index('uses: ./.github/actions/build'),
                        test_job.index('uses: ./.github/actions/test'),
                        'Test asserts on ./dist, so it builds first in its own workspace')
        for uses in re.findall(r'uses: ([^\s#]+)', ci):
            if uses.startswith('./'):
                continue
            self.assertRegex(uses.split('@', 1)[1], r'^[0-9a-f]{40}$',
                             f'{uses} must be pinned by full commit SHA')
