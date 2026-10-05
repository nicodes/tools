import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
PINS = Path(__file__).parents[1] / 'helpers/pins.mjs'
UPDATER = Path(__file__).parents[1] / 'helpers/action-pins.mjs'

# Synthetic annotated-tag objects and their distinct peeled commits.
V1_TAG, V1 = '1111111111111111111111111111111111111111', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
V2_TAG, V2 = '2222222222222222222222222222222222222222', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
V3_TAG, V3 = '3333333333333333333333333333333333333333', 'cccccccccccccccccccccccccccccccccccccccc'
V8_TAG, V8 = '4444444444444444444444444444444444444444', 'dddddddddddddddddddddddddddddddddddddddd'
V10_TAG, V10 = '5555555555555555555555555555555555555555', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
CHECKOUT = 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1'
MISE = 'jdx/mise-action@c2a87611a18de5b3828c5652fe268e992400cb5c'

LS_REMOTE = ''.join(f'{tag}\trefs/tags/{name}\n{commit}\trefs/tags/{name}^{{}}\n'
                    for name, tag, commit in [('v0.0.1', V1_TAG, V1), ('v0.0.2', V2_TAG, V2),
                                              ('v0.0.3', V3_TAG, V3), ('v0.0.8', V8_TAG, V8),
                                              ('v0.0.10', V10_TAG, V10)])


def write_product(root, steps=(), composite=None, record=None, applications=('app',),
                  installed=False):
    """Build the smallest product tree that passes every other pins.mjs check.

    `installed` builds the layout a product has after it stops vendoring: the
    snapshot sits outside the repository where mise put it, and the product's
    own pin record sits at the repository root, because there is no longer a
    product-owned directory inside the snapshot to hold it.
    """
    engineering = (root.parent / 'installed-engineering') if installed else (root / 'scripts/engineering')
    (engineering / 'helpers').mkdir(parents=True)
    helper = engineering / 'helpers/pins.mjs'
    helper.write_text('// vendored helper fixture\n')
    source = {'repository': 'https://github.com/nicodes/tools', 'revision': 'a' * 40,
              'files': {'helpers/pins.mjs': hashlib.sha256(helper.read_bytes()).hexdigest()}}
    (engineering / 'SOURCE.json').write_text(json.dumps(source, indent=2) + '\n')
    (root / '.mise.toml').write_text('[tools]\nbun = "1.4.1"\n')
    for name in applications:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / 'package.json').write_text('{"packageManager": "bun@1.4.1"}')
        (directory / 'bun.lock').write_text('')
    workflows = root / '.github/workflows'
    workflows.mkdir(parents=True)
    (workflows / 'bun-updates.yml').write_text(
        'name: Bun updates\non:\n  schedule:\n    - cron: \'17 10 * * 1\'\n'
        'permissions:\n  contents: read\njobs:\n  update:\n'
        "    if: github.ref == 'refs/heads/main'\n"
        '    permissions:\n      contents: read\n      issues: write\n    steps:\n'
        f'      - uses: {MISE}\n      - run: python3 scripts/engineering/helpers/update-bun.py\n')
    (root / '.github/dependabot.yml').write_text(
        'version: 2\nupdates:\n  - package-ecosystem: github-actions\n'
        '    directory: /\n    schedule:\n      interval: weekly\n'
        '  - package-ecosystem: docker\n    directory: /deploy/images\n'
        '    schedule:\n      interval: weekly\n')
    (workflows / 'ci.yml').write_text('name: CI\non: [push]\njobs:\n  ci:\n'
                                      '    runs-on: ubuntu-24.04\n    steps:\n'
                                      + ''.join(f'      - uses: {use}\n' for use in steps))
    if composite is not None:
        action = root / '.github/actions/publish/action.yml'
        action.parent.mkdir(parents=True)
        action.write_text('runs:\n  using: composite\n  steps:\n'
                          + ''.join(f'    - uses: {use}\n' for use in composite))
    if record is not None:
        holder = root if installed else engineering
        (holder / 'ACTION-PINS.json').write_text(json.dumps(record, indent=2) + '\n')
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True, timeout=60)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True, timeout=60)


def stage(root):
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True, timeout=60)


def run_pins(root, installed=False):
    env = {**os.environ, 'ACTION_REPOSITORY': 'https://github.com/example-org/deploy-actions', 'ACTION_PIN_POLICY': json.dumps({'owners':['example-org','example-games','example-services','nicodes'], 'repositories':['example-org/deploy-actions','example-games/game-actions','nicodes/tools','nicodes/cicd']})}
    if installed:
        env['CICD_ENGINEERING'] = str(root.parent / 'installed-engineering')
    else:
        env.pop('CICD_ENGINEERING', None)
    return subprocess.run(['bun', str(PINS)], cwd=root, capture_output=True, text=True,
                          timeout=60, env=env)


def run_updater(root, arguments, remote=LS_REMOTE):
    with tempfile.TemporaryDirectory() as directory:
        shim = Path(directory) / 'git'
        shim.write_text(f'#!/bin/sh\ncat <<EOF\n{remote}EOF\n')
        shim.chmod(0o700)
        env = {**os.environ, 'ACTION_REPOSITORY': 'https://github.com/example-org/deploy-actions', 'PATH': f'{shim.parent}{os.pathsep}{os.environ["PATH"]}'}
        return subprocess.run(['bun', str(UPDATER), *arguments], cwd=root,
                              capture_output=True, text=True, timeout=60, env=env)


class ActionPinGuard(unittest.TestCase):
    def test_matching_pins_and_record_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root,
                          steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                          composite=[f'example-org/deploy-actions/publish-config@{V1} # v0.0.1'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3},
                                           'publish-config': {'tag': 'v0.0.1', 'sha': V1}}})
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_drifted_pin_is_refused_with_both_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V8} # v0.0.8'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            for expected in ['connect', V8, V3, 'v0.0.3', 'ACTION-PINS.json']:
                self.assertIn(expected, output)

    def test_missing_record_is_refused_when_actions_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'])
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn('ACTION-PINS.json', output)
            self.assertIn('action-pins.mjs', output)

    def test_unrecorded_action_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/deploy@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('deploy', result.stdout + result.stderr)

    def test_non_sha_reference_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=['example-org/deploy-actions/connect@v0.0.1'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.1', 'sha': V1}}})
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('full commit SHA', result.stdout + result.stderr)

    def test_tag_object_sha_is_refused_against_the_commit_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3_TAG} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn(V3_TAG, output)
            self.assertIn(V3, output)

    def test_products_without_komizo_actions_need_no_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT])
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': 'dd77ee87'}}})
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('peeled commit SHA', result.stdout + result.stderr)

    def test_misspelled_portfolio_org_is_refused_naming_the_correction(self):
        # One-byte org typos pass the full-SHA rule but resolve to nothing.
        for typo, correction in [('nicode', 'nicodes'), ('Nicodes', 'nicodes'),
                                 ('example-game', 'example-games'), ('example-service', 'example-services')]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[f'{typo}/some-action@{V3}'])
                result = run_pins(root)
                self.assertNotEqual(result.returncode, 0, typo)
                output = result.stdout + result.stderr
                self.assertIn(typo, output)
                self.assertIn(correction, output)
                self.assertIn('misspelling', output)

    def test_misspelled_fleet_repository_is_refused_naming_the_correction(self):
        # The samplehost-be incident (fd23b9c9): example-org/deploy-actons — 0x6f for
        # 0x69 — satisfied the generic full-SHA rule while pointing at nothing,
        # and every CD run failed at action resolution with CI blind to it.
        for typo, correction in [('example-org/deploy-actons', 'example-org/deploy-actions'),
                                 ('nicodes/cicdd', 'nicodes/cicd'),
                                 ('example-games/game-action', 'example-games/game-actions')]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[f'{typo}/some-action@{V3}'])
                result = run_pins(root)
                self.assertNotEqual(result.returncode, 0, typo)
                output = result.stdout + result.stderr
                self.assertIn(typo, output)
                self.assertIn(correction, output)

    def test_exact_fleet_and_distant_third_party_references_are_unaffected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The cicd pin matches write_product's SOURCE.json revision. This
            # test is about the misspelling guard, not revision agreement --
            # but a cicd call is now checked against what the product
            # vendors, so an arbitrary SHA here would fail for an unrelated
            # reason and this test would stop testing what it says it does.
            write_product(root, steps=['example-games/game-actions/install@'+V3,
                                       f'nicodes/tools/.github/workflows/deployed.yml@{"a" * 40}',
                                       'nico/some-action@'+V3,  # three edits from nicodes
                                       'docker/login-action@'+V1])
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ApplicationManifestScan(unittest.TestCase):
    """The per-application rules follow every tracked top-level `*/package.json`
    directory — `app/` is convention, not requirement — and a repository with no
    Bun application at all (a Godot/game repo) has nothing to check in that loop
    while the mise exact-version and workflow/action-pins rules still apply."""

    def test_app_directory_repo_passes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT])
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_app_manifest_directory_passes(self):
        # example-games/samplegame tracks no `app/`; its Bun app is `playwright/`.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=('playwright',))
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_tracked_manifest_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=('app', 'playwright'))
            (root / 'playwright/package.json').write_text('{"packageManager": "bun@1.3.9"}')
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('playwright: Bun pin differs', result.stdout + result.stderr)

    def test_manifestless_repo_passes_with_other_pins_still_enforced(self):
        # Pure Godot/game shape: no package.json anywhere, so the applications
        # loop is skipped — but mise and workflow pins keep failing loudly.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=())
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=['actions/checkout@v4'], applications=())
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('full commit SHA', result.stdout + result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=())
            (root / '.mise.toml').write_text('[tools]\nbun = "1"\n')
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not an exact version', result.stdout + result.stderr)

    def test_a_tool_table_is_read_for_its_version(self):
        """mise lets a tool carry options beside its version, and the Godot
        products pin every engine and CLI that way -- there is no other way to
        express an asset_pattern or a postinstall. Reading only the string form
        refused three products for using a supported feature."""
        for entry, ok in [
                ('"github:godotengine/godot" = { version = "4.7.2-stable", exe = "bin/godot" }', True),
                ('"github:example-games/sampleaddon" = { version = "v0.0.8", bin = "sampleaddon" }', True),
                ('"github:x/y" = { version = "cli-v0.0.5" }', True),
                # Exactness is the point, and a table must not smuggle past it.
                ('"github:x/y" = { version = "latest" }', False),
                ('"github:x/y" = { exe = "bin/y" }', False),
                ('"github:x/y" = { version = "^1.2.3" }', False),
                ('"github:x/y" = { version = "3" }', False),
        ]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[CHECKOUT], applications=())
                (root / '.mise.toml').write_text(f'[tools]\nbun = "1.4.1"\n{entry}\n')
                stage(root)
                result = run_pins(root)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, entry)

    def test_a_resolving_version_is_refused_however_it_is_spelled(self):
        # "not an exact version" had nothing to say about these: the old
        # pattern only knew the shape 1.2.3, so `latest` failed for looking
        # wrong rather than for being a moving target, and `stable` passed
        # nothing at all because no product had tried it.
        for version in ('latest', 'lts', 'stable', 'system', '^1.2.3', '~1.2.3', '1.2.x'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[CHECKOUT], applications=())
                (root / '.mise.toml').write_text(f'[tools]\nbun = "{version}"\n')
                stage(root)
                self.assertNotEqual(run_pins(root).returncode, 0, version)

    def test_a_go_mod_that_is_not_ours_can_say_so(self):
        """A repository can hold a go.mod it must not edit.

        samplegame vendors github.com/tianon/gosu at a pinned revision,
        declaring go 1.20, and holding it to this product's toolchain would
        mean rewriting upstream's module file. The rule refused the product
        for carrying vendored code faithfully.
        """
        for body, ok, why in [
                ('module x\n\n// pins: not-ours -- vendored upstream, pinned by PIN.json\ngo 1.20\n',
                 True, 'a reasoned exemption'),
                ('module x\n\n// pins: not-ours\ngo 1.20\n',
                 False, 'no reason given'),
                ('module x\n\n// pins: not-ours -- old\ngo 1.20\n',
                 False, 'a reason too short to review'),
                ('module x\n\ngo 1.20\n',
                 False, 'no exemption at all'),
        ]:
            with self.subTest(why=why), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[CHECKOUT], applications=())
                (root/'vendored').mkdir()
                (root/'vendored/go.mod').write_text(body)
                stage(root)
                result = run_pins(root)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, why)

    def test_an_exemption_does_not_excuse_the_products_own_modules(self):
        # The escape hatch must not become the way every go.mod opts out of
        # the check. One file exempting itself says nothing about the next.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=())
            (root/'vendored').mkdir()
            (root/'vendored/go.mod').write_text(
                'module x\n\n// pins: not-ours -- vendored upstream at a pinned revision\ngo 1.20\n')
            (root/'api').mkdir()
            (root/'api/go.mod').write_text('module ours\n\ngo 1.20\n')
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('api/go.mod', result.stdout + result.stderr)

    def test_a_module_that_is_not_ours_is_not_dependabots_either(self):
        # Asking dependabot to watch vendored upstream source would open PRs
        # proposing to edit somebody else's pinned revision. The coverage
        # check must read the same exemption, and the fixture below declares
        # no gomod entry for the vendored directory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=())
            (root/'vendored').mkdir()
            (root/'vendored/go.mod').write_text(
                'module github.com/someone/else\n\n'
                '// pins: not-ours -- vendored upstream, pinned by PIN.json\ngo 1.20\n')
            stage(root)
            self.assertEqual(run_pins(root).returncode, 0)

    def test_a_fixture_dockerfile_can_say_its_base_is_a_parameter(self):
        """samplearcade and sampleclient keep a Dockerfile whose whole purpose is to
        take the image under test as ARG BASE_IMAGE: a test builds it,
        asserts something, throws it away. Demanding a digest there asks for
        the one thing the fixture exists to vary."""
        for body, ok, why in [
                ('# pins: unpinned-base -- fixture; the caller supplies BASE_IMAGE\n'
                 'ARG BASE_IMAGE=scratch\nFROM ${BASE_IMAGE}\n', True, 'a reasoned exemption'),
                ('# pins: unpinned-base\nARG BASE_IMAGE=scratch\nFROM ${BASE_IMAGE}\n',
                 False, 'no reason given'),
                ('ARG BASE_IMAGE=scratch\nFROM ${BASE_IMAGE}\n', False, 'no exemption'),
        ]:
            with self.subTest(why=why), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_product(root, steps=[CHECKOUT], applications=())
                (root/'tools').mkdir()
                (root/'tools/fixture.Dockerfile').write_text(body)
                stage(root)
                result = run_pins(root)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, why)

    def test_the_fixture_marker_does_not_excuse_a_shipped_image(self):
        # One Dockerfile exempting itself says nothing about the next. A
        # shipped image with an unpinned base is the thing this rule is for.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=())
            (root/'tools').mkdir()
            (root/'tools/fixture.Dockerfile').write_text(
                '# pins: unpinned-base -- fixture; the caller supplies BASE_IMAGE\n'
                'ARG BASE_IMAGE=scratch\nFROM ${BASE_IMAGE}\n')
            (root/'deploy').mkdir()
            (root/'deploy/gate.Dockerfile').write_text('FROM caddy:2-alpine\n')
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('deploy/gate.Dockerfile', result.stdout + result.stderr)

    def test_missing_lockfile_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=('playwright',))
            (root / 'playwright/bun.lock').unlink()
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('playwright: commit bun.lock', result.stdout + result.stderr)

    def test_competing_lockfile_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[CHECKOUT], applications=('playwright',))
            (root / 'playwright/package-lock.json').write_text('{}')
            stage(root)
            result = run_pins(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('playwright: remove the competing package-lock.json', result.stdout + result.stderr)


class ActionPinUpdater(unittest.TestCase):
    def call(self, function, arguments, map_arguments=()):
        program = (f'import {{ {function} }} from {json.dumps(UPDATER.as_uri())};\n'
                   'const args = JSON.parse(process.argv[1]);\n'
                   f'for (const index of {json.dumps(list(map_arguments))}) '
                   'args[index] = new Map(Object.entries(args[index]));\n'
                   'try {\n'
                   f'  let value = {function}(...args);\n'
                   '  if (value instanceof Map) value = Object.fromEntries(value);\n'
                   "  console.log('OK ' + JSON.stringify(value));\n"
                   '} catch (error) {\n'
                   "  console.log('ERR ' + (error.message ?? String(error)));\n"
                   '}\n')
        result = subprocess.run(['bun', '--eval', program, json.dumps(list(arguments))],
                                capture_output=True, text=True, timeout=20, env={**os.environ, 'ACTION_REPOSITORY':'https://github.com/example-org/deploy-actions'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(('OK ', 'ERR ')), result.stdout)
        return result.stdout[3:].rstrip('\n'), result.stdout.startswith('OK ')

    def test_annotated_tags_resolve_to_peeled_commits(self):
        value, ok = self.call('parseTags', [LS_REMOTE])
        self.assertTrue(ok, value)
        tags = json.loads(value)
        self.assertEqual(tags['v0.0.3']['commit'], V3)
        self.assertEqual(tags['v0.0.3']['object'], V3_TAG)
        self.assertEqual(tags['v0.0.10']['commit'], V10)
        self.assertEqual(tags['v0.0.1']['commit'], V1)
        self.assertEqual(self.call('latestTag', [tags], map_arguments=(0,)), ('"v0.0.10"', True))

    def test_lightweight_and_unknown_ls_remote_output(self):
        lightweight = 'f00d' + '0' * 36
        value, ok = self.call('parseTags', [f'{lightweight}\trefs/tags/light\n'])
        self.assertTrue(ok, value)
        tags = json.loads(value)
        self.assertEqual(tags['light']['commit'], lightweight)
        self.assertEqual(tags['light']['object'], lightweight)
        for output, expected in [('', False), ('not-ls-remote\n', False),
                                 (f'{lightweight}\trefs/tags/x\n{V1}\trefs/tags/x^{{}}\n', True)]:
            value, ok = self.call('parseTags', [output])
            self.assertEqual(ok, expected, output)

    def test_record_schema(self):
        valid = {'repository': 'https://github.com/example-org/deploy-actions',
                 'pins': {'connect': {'tag': 'v0.0.10', 'sha': V10}}}
        value, ok = self.call('parseRecord', [json.dumps(valid)])
        self.assertTrue(ok, value)
        for broken in [{**valid, 'repository': 'https://example.invalid'},
                       {**valid, 'pins': None},
                       {'repository': valid['repository'], 'pins': {'connect': {'tag': 'main', 'sha': V10}}},
                       {'repository': valid['repository'], 'pins': {'connect': {'tag': 'v0.0.10', 'sha': 'dd77'}}}]:
            value, ok = self.call('parseRecord', [json.dumps(broken)])
            self.assertFalse(ok, broken)

    def test_record_rendering_is_canonical(self):
        pins = {'run-task': {'sha': V2, 'tag': 'v0.0.2'}, 'connect': {'sha': V10, 'tag': 'v0.0.10'}}
        value, ok = self.call('renderRecord', [pins])
        self.assertTrue(ok, value)
        rendered = json.loads(value)
        self.assertEqual(json.loads(rendered)['pins']['connect']['sha'], V10)
        self.assertLess(rendered.index('connect'), rendered.index('run-task'))
        value_again, _ = self.call('renderRecord', [json.loads(rendered)['pins']])
        self.assertEqual(rendered, json.loads(value_again))

    def test_workflow_scanning_and_rewriting(self):
        workflow = (f'name: CI\njobs:\n  deploy:\n    steps:\n'
                    f'      - uses: example-org/deploy-actions/connect@{V3_TAG} # v0.0.3\n'
                    f'      - uses: example-org/deploy-actions/deploy@{V3}\n'
                    f'      - uses: example-org/deploy-actions/connect@{V10} # v0.0.10\n'
                    f'      - {CHECKOUT}\n')
        value, ok = self.call('scanUses', [workflow])
        self.assertTrue(ok, value)
        self.assertEqual(json.loads(value), [
            {'action': 'connect', 'ref': V3_TAG},
            {'action': 'deploy', 'ref': V3},
            {'action': 'connect', 'ref': V10}])
        pins = {'connect': {'tag': 'v0.0.10', 'sha': V10}, 'deploy': {'tag': 'v0.0.3', 'sha': V3}}
        value, ok = self.call('rewriteUses', [workflow, pins])
        self.assertTrue(ok, value)
        result = json.loads(value)
        self.assertEqual(result['changes'], 2)
        self.assertNotIn(V3_TAG, result['text'])
        self.assertIn(f'example-org/deploy-actions/connect@{V10} # v0.0.10', result['text'])
        self.assertIn(f'example-org/deploy-actions/deploy@{V3} # v0.0.3', result['text'])
        self.assertIn(CHECKOUT, result['text'])
        value, ok = self.call('rewriteUses', [result['text'], pins])
        self.assertEqual(json.loads(value)['changes'], 0)

    def test_drift_detection(self):
        tags = json.loads(self.call('parseTags', [LS_REMOTE])[0])
        aligned = {'repository': 'x', 'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}}
        uses = [{'file': '.github/workflows/ci.yml', 'action': 'connect', 'ref': V3}]
        value, ok = self.call('drift', [aligned, tags, uses], map_arguments=(1,))
        self.assertTrue(ok, value)
        self.assertEqual(json.loads(value), [])
        moved = {'repository': 'x', 'pins': {'connect': {'tag': 'v0.0.3', 'sha': V8}}}
        vanished = {'repository': 'x', 'pins': {'deploy': {'tag': 'v0.0.6', 'sha': V1}}}
        for record, expected in [(moved, 'moved upstream'), (vanished, 'no longer exists')]:
            value, ok = self.call('drift', [record, tags, uses], map_arguments=(1,))
            self.assertTrue(ok)
            self.assertIn(expected, value)
        value, ok = self.call('drift', [aligned, tags,
                              [{'file': 'ci.yml', 'action': 'connect', 'ref': V1},
                               {'file': 'ci.yml', 'action': 'run-task', 'ref': V2}]], map_arguments=(1,))
        self.assertTrue(ok)
        self.assertIn('not recorded', value)
        self.assertIn('differs from the recorded', value)


class ActionPinUpdaterEndToEnd(unittest.TestCase):
    def test_check_bootstrap_update_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3',
                                       f'example-org/deploy-actions/deploy@{V1_TAG} # v0.0.1'])
            # Bootstrap writes to the root: the record is product-owned, and
            # a product that installs the snapshot has no directory inside it
            # to put one in.
            record = root / 'ACTION-PINS.json'
            missing = run_updater(root, ['--check'])
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('missing', missing.stdout + missing.stderr)

            bootstrapped = run_updater(root, [])
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stdout + bootstrapped.stderr)
            pins = json.loads(record.read_text())['pins']
            self.assertEqual(pins['connect'], {'tag': 'v0.0.10', 'sha': V10})
            self.assertEqual(pins['deploy'], {'tag': 'v0.0.10', 'sha': V10})

            drifting = run_updater(root, ['--check'])
            self.assertNotEqual(drifting.returncode, 0)
            self.assertIn('differs from the recorded', drifting.stdout)

            updated = run_updater(root, ['--update'])
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            workflow = (root / '.github/workflows/ci.yml').read_text()
            self.assertIn(f'example-org/deploy-actions/connect@{V10} # v0.0.10', workflow)
            self.assertIn(f'example-org/deploy-actions/deploy@{V10} # v0.0.10', workflow)
            self.assertNotIn(V3, workflow)

            aligned = run_updater(root, ['--check'])
            self.assertEqual(aligned.returncode, 0, aligned.stdout + aligned.stderr)

    def test_moved_upstream_tag_requires_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moved_remote = LS_REMOTE.replace(
                f'{V3}\trefs/tags/v0.0.3^{{}}\n', f'{V8}\trefs/tags/v0.0.3^{{}}\n')
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            refused = run_updater(root, ['--check'], moved_remote)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('moved upstream', refused.stdout)
            blocked = run_updater(root, [], moved_remote)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn('--accept-moved-tags', blocked.stdout + blocked.stderr)
            accepted = run_updater(root, ['--accept-moved-tags'], moved_remote)
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            pins = json.loads((root / 'scripts/engineering/ACTION-PINS.json').read_text())['pins']
            self.assertEqual(pins['connect']['sha'], V8)


class CicdRevisionAgreement(unittest.TestCase):
    """A product consumes nicodes/cicd twice; both must name one commit.

    Its helpers are vendored (SOURCE.json.revision) and its reusable
    workflows are called (@sha). Nothing tied those together, and they
    drifted apart across most of the portfolio -- products running helper
    code from one revision while their backup and vulnerability workflows
    came from another, in one case from three at once.

    Neither half is wrong on its own, which is why nobody saw it: the
    question "which revision of cicd is this product on" had no answer.
    """

    REVISION = 'a' * 40

    def product(self, steps):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write_product(root, steps=steps)
        stage(root)
        return run_pins(root)

    def test_a_workflow_pinned_to_the_vendored_revision_passes(self):
        result = self.product([f'nicodes/tools/.github/workflows/vuln.yml@{self.REVISION}'])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_workflow_pinned_elsewhere_is_refused(self):
        result = self.product([f'nicodes/tools/.github/workflows/vuln.yml@{"b" * 40}'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('two revisions of the same repository', result.stderr)

    def test_renamed_action_and_workflow_still_bind_the_snapshot_revision(self):
        for called in ('make', '.github/workflows/vuln.yml'):
            with self.subTest(called=called):
                result = self.product([f'nicodes/tools/{called}@{self.REVISION}'])
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.product([f'nicodes/tools/{called}@{"b" * 40}'])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('two revisions of the same repository', result.stderr)

    def test_mixed_repository_names_cannot_hide_a_mismatched_revision(self):
        result = self.product([
            f'nicodes/tools/make@{self.REVISION}',
            f'nicodes/tools/.github/workflows/backup.yml@{"b" * 40}',
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup.yml', result.stderr)

    def test_every_call_is_checked_not_just_the_first(self):
        # samplegame pinned three different cicd revisions across its
        # workflows. Checking one would have reported it clean.
        result = self.product([
            f'nicodes/tools/.github/workflows/vuln.yml@{self.REVISION}',
            f'nicodes/tools/.github/workflows/backup.yml@{"c" * 40}',
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup.yml', result.stderr + result.stdout)

    def test_a_tag_is_refused_even_though_it_is_a_valid_ref(self):
        # SOURCE.json records a commit, so a tag here cannot be compared with
        # it at all -- and three products pin komizo-actions by tag today,
        # so this is a shape the portfolio actually produces.
        result = self.product(['nicodes/tools/.github/workflows/vuln.yml@v0.1.0'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('full commit SHA', result.stderr)

    def test_a_product_that_calls_no_cicd_workflow_is_unaffected(self):
        result = self.product([CHECKOUT])
        self.assertEqual(result.returncode, 0, result.stderr)


class InstalledSnapshot(unittest.TestCase):
    """The same gate, for a product that installs the snapshot instead of copying it.

    Nothing about what is checked changes. SOURCE.json ships inside the
    release artifact, so the invariant pins.mjs exists to enforce -- the
    reusable-workflow pins equal the snapshot revision -- is still checkable
    without a copy of the snapshot in the product.
    """

    def product(self, root, **kwargs):
        write_product(root, installed=True, **kwargs)
        return run_pins(root, installed=True)

    def test_a_product_that_installed_the_snapshot_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            result = self.product(root,
                                  steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                                  record={'repository': 'https://github.com/example-org/deploy-actions',
                                          'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_renamed_manifest_identity_is_allowed_but_unrelated_identity_is_refused(self):
        for repository in ('https://github.com/nicodes/tools', 'https://github.com/other/tools'):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/'product'; root.mkdir()
                write_product(root, installed=True, steps=[f'nicodes/tools/make@{"a" * 40}'])
                manifest = root.parent/'installed-engineering/SOURCE.json'
                source = json.loads(manifest.read_text()); source['repository'] = repository
                manifest.write_text(json.dumps(source))
                result = run_pins(root, installed=True)
                self.assertEqual(result.returncode == 0, repository == 'https://github.com/nicodes/tools', result.stderr)

    def test_the_pin_record_is_read_from_the_repository_root(self):
        """It is product-owned, so it cannot live inside an installed snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            self.product(root,
                         steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                         record={'repository': 'https://github.com/example-org/deploy-actions',
                                 'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            self.assertTrue((root / 'ACTION-PINS.json').is_file())
            self.assertFalse((root / 'scripts/engineering').exists())

    def test_drift_is_still_caught_when_the_snapshot_is_installed(self):
        """The whole point: the check must not get weaker by moving."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            result = self.product(root,
                                  steps=[f'example-org/deploy-actions/connect@{V8} # v0.0.8'],
                                  record={'repository': 'https://github.com/example-org/deploy-actions',
                                          'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            self.assertEqual(result.returncode, 1)
            self.assertIn(V3, result.stdout + result.stderr)

    def test_a_missing_pin_record_names_the_root_not_the_old_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            result = self.product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'])
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1)
            self.assertIn('ACTION-PINS.json', output)
            self.assertNotIn('scripts/engineering/ACTION-PINS.json', output)

    def test_an_explicit_snapshot_that_is_not_one_is_refused(self):
        """CICD_ENGINEERING pointing somewhere wrong must fail, not fall back."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            write_product(root, steps=[])
            env = {**os.environ, 'CICD_ENGINEERING': str(Path(directory) / 'nowhere')}
            result = subprocess.run(['bun', str(PINS)], cwd=root, capture_output=True,
                                    text=True, timeout=60, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn('not an engineering snapshot', result.stdout + result.stderr)

    def test_the_declared_snapshot_wins_over_a_leftover_vendored_one(self):
        """A product mid-migration must use what it declared, not what it forgot."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'product'
            root.mkdir()
            write_product(root, steps=[], installed=True)
            stale = root / 'scripts/engineering'
            (stale / 'helpers').mkdir(parents=True)
            (stale / 'SOURCE.json').write_text(json.dumps(
                {'repository': 'https://github.com/nicodes/tools', 'revision': 'f' * 40,
                 'files': {}}) + '\n')
            result = run_pins(root, installed=True)
            self.assertNotIn('f' * 40, result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PinRecordLocation(unittest.TestCase):
    """Reading tolerates the old place; writing only ever uses the new one."""

    def test_a_record_still_in_the_snapshot_directory_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            self.assertTrue((root / 'scripts/engineering/ACTION-PINS.json').is_file())
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_root_record_wins_when_a_product_has_both(self):
        """Mid-migration, the new location is the one that counts."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.8', 'sha': V8}}})
            (root / 'ACTION-PINS.json').write_text(json.dumps(
                {'repository': 'https://github.com/example-org/deploy-actions',
                 'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}}, indent=2) + '\n')
            stage(root)
            result = run_pins(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bootstrap_never_writes_into_the_snapshot_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root, steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3'])
            self.assertEqual(run_updater(root, []).returncode, 0)
            self.assertTrue((root / 'ACTION-PINS.json').is_file())
            self.assertFalse((root / 'scripts/engineering/ACTION-PINS.json').exists())


class AdoptingANewAction(unittest.TestCase):
    """A product that starts using a samplehost action the record has never seen.

    Before this, the updater could only refresh actions already recorded, so
    adopting a new one meant hand-editing the file this helper exists to be
    the only writer of. sampleapp hit it adopting komizo-actions/preview.
    """

    def product(self, root, steps, record):
        write_product(root, steps=steps, record=record)
        return run_updater(root, [])

    def record_of(self, root):
        # write_product puts it in the snapshot directory; the updater writes
        # back to whichever location the product already uses.
        for candidate in (root / 'ACTION-PINS.json', root / 'scripts/engineering/ACTION-PINS.json'):
            if candidate.is_file():
                return json.loads(candidate.read_text())['pins']
        raise AssertionError('no pin record was written')

    def test_a_newly_used_action_joins_the_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.product(root,
                         [f'example-org/deploy-actions/connect@{V10} # v0.0.10',
                          f'example-org/deploy-actions/preview@{V10} # v0.0.10'],
                         {'repository': 'https://github.com/example-org/deploy-actions',
                          'pins': {'connect': {'tag': 'v0.0.10', 'sha': V10}}})
            pins = self.record_of(root)
            self.assertIn('preview', pins)
            self.assertEqual(pins['preview'], {'tag': 'v0.0.10', 'sha': V10})

    def test_it_joins_at_the_tag_the_rest_of_the_record_is_on(self):
        """Not the newest upstream tag: the record's point is that the fleet
        agrees on one, and adopting an action must not move the others."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.product(root,
                         [f'example-org/deploy-actions/connect@{V3} # v0.0.3',
                          f'example-org/deploy-actions/preview@{V3} # v0.0.3'],
                         {'repository': 'https://github.com/example-org/deploy-actions',
                          'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3}}})
            pins = self.record_of(root)
            self.assertEqual(pins['preview']['tag'], 'v0.0.3')
            self.assertEqual(pins['connect']['tag'], 'v0.0.3', 'the existing pin must not move')

    def test_a_record_split_across_tags_is_refused_rather_than_guessed_from(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.product(root,
                                  [f'example-org/deploy-actions/connect@{V3} # v0.0.3',
                                   f'example-org/deploy-actions/preview@{V3} # v0.0.3'],
                                  {'repository': 'https://github.com/example-org/deploy-actions',
                                   'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3},
                                            'deploy': {'tag': 'v0.0.8', 'sha': V8}}})
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn('split across', output)
            self.assertIn('preview', output)

    def test_latest_places_a_new_action_with_everything_else(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_product(root,
                          steps=[f'example-org/deploy-actions/connect@{V3} # v0.0.3',
                                 f'example-org/deploy-actions/preview@{V3} # v0.0.3'],
                          record={'repository': 'https://github.com/example-org/deploy-actions',
                                  'pins': {'connect': {'tag': 'v0.0.3', 'sha': V3},
                                           'deploy': {'tag': 'v0.0.8', 'sha': V8}}})
            self.assertEqual(run_updater(root, ['--latest']).returncode, 0)
            pins = self.record_of(root)
            self.assertEqual({pin['tag'] for pin in pins.values()}, {'v0.0.10'})
            self.assertIn('preview', pins)
