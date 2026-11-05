#!/usr/bin/env python3
"""Generate the small pinned Make entrypoint from caller-owned recipe data."""
import argparse
import hashlib
from pathlib import Path
import re

from configuration import load, ci_settings
import orchestration
import recipe as native_recipe


def workflow_files(root, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    config = load(root)
    ci = ci_settings(config)
    model = orchestration.model(config)
    header = '# Generated cookbook caller. Change recipe data and regenerate.\n'
    files = {'.github/workflows/ci.yml': header + '''name: Build & Test
'on':
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      source:
        description: Exact source commit; defaults to this main revision
        type: string
        default: ''
      tools:
        description: Accepted cookbook commit; defaults to the pinned recipe
        type: string
        default: ''
permissions: {}
concurrency:
  group: ci-${{ github.ref }}-${{ inputs.source || 'current' }}
  cancel-in-progress: true
jobs:
  validate:
    uses: nicodes/tools/.github/workflows/cookbook-ci.yml@PIN
    permissions:
      contents: read
      actions: read
      pull-requests: read
    with:
      tools-ref: ${{ inputs.tools || 'PIN' }}
      source: ${{ inputs.source || github.sha }}
      merge-gate: ${{ vars.COOKBOOK_REQUEST_MERGE == 'enabled' }}
'''.replace('PIN', revision), '.github/workflows/request-merge.yml': header + '''name: Request merge
'on':
  workflow_dispatch:
    inputs:
      pull-request:
        description: PR number to fully validate and merge when current
        type: string
        required: true
      expected-head:
        description: Exact head selected by a dependency policy decision; paired with expected-base
        type: string
        default: ''
      expected-base:
        description: Exact main selected by a dependency policy decision; paired with expected-head
        type: string
        default: ''
permissions: {}
concurrency:
  group: cookbook-merge-${{ github.repository }}
  cancel-in-progress: false
jobs:
  request:
    uses: nicodes/tools/.github/workflows/cookbook-request-merge.yml@PIN
    permissions:
      contents: write
      pull-requests: read
      statuses: write
      actions: write
    with:
      tools-ref: PIN
      pull-request: ${{ inputs.pull-request }}
      expected-head: ${{ inputs.expected-head }}
      expected-base: ${{ inputs.expected-base }}
'''.replace('PIN', revision)}
    if model['recipe'] != 'godot-addon':
        files['.github/workflows/ci.yml'] += '      recipe: ' + model['recipe'] + '\n'
        if ci.get('preview_environment'):
            files['.github/workflows/ci.yml'] += '      preview-environment: ' + ci['preview_environment'] + '\n'
    files['.github/workflows/ci.yml'] += ('      runner: ' + ci['runner'] + '\n'
        + '      timeout-minutes: ' + str(ci['timeout_minutes']) + '\n')
    if config.get('delivery'):
        if config['recipe'] not in {'react-native', 'godot-game'}:
            raise ValueError('this delivery composition is enrolled for application images only')
        import delivery_workflows
        # Host admission authenticates the caller's cd.yml workflow identity.
        # Production validates once here; PR and requested candidates use ci.yml.
        original = files['.github/workflows/ci.yml']
        files['.github/workflows/ci.yml'] = original.replace('  push:\n    branches: [main]\n', '')
        production = original.replace('name: Build & Test\n', 'name: Deploy\n', 1)
        production = production.replace('  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n', '')
        production = production.replace('group: ci-${{ github.ref }}-', 'group: deploy-source-${{ github.sha }}-', 1).replace('cancel-in-progress: true', 'cancel-in-progress: false', 1)
        production = production.replace("merge-gate: ${{ vars.COOKBOOK_REQUEST_MERGE == 'enabled' }}", 'merge-gate: false')
        delivery=delivery_workflows.declaration(root,config)
        if delivery.get('historical_deploy'):
            production=production.replace('\npermissions: {}', '''
      version:
        description: Full commit of an already-published release; empty builds this source
        type: string
        default: ''
permissions: {}''',1)
            production=production.replace('  validate:\n',"  validate:\n    if: inputs.version == '' || inputs.version == github.sha\n",1)
        if delivery.get('validation_timeout_minutes'):
            production=production.replace('      timeout-minutes: '+str(ci['timeout_minutes'])+'\n',
                '      timeout-minutes: '+str(delivery['validation_timeout_minutes'])+'\n',1)
        files['.github/workflows/cd.yml'] = production + delivery_workflows.jobs(root, config, revision)
        if delivery_workflows.declaration(root, config).get('mode') == 'godot-static':
            import godot_static_workflows
            files.update(godot_static_workflows.files(root, config, revision))
    if config.get('preview'):
        import preview_workflows
        files.update(preview_workflows.workflow_files(root, revision, config))
    dependency_policy = Path(root) / '.github/dependency-policy.json'
    if dependency_policy.is_file():
        import json
        policy = json.loads(dependency_policy.read_text())
        if policy.get('version') == 2:
            files['.github/workflows/dependency-policy.yml'] = header + """name: Dependency Policy
'on':
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_run:
    workflows: [Build & Test]
    types: [completed]
  workflow_dispatch: {}
  schedule:
    - cron: '4 6 * * *'
permissions: {}
jobs:
  request:
    uses: nicodes/tools/.github/workflows/cookbook-dependencies.yml@PIN
    permissions:
      contents: read
      pull-requests: read
      actions: write
      checks: read
      statuses: read
    with:
      tools-ref: PIN
""".replace('PIN', revision)
    if config.get('publication') and config['recipe'] not in {'godot-addon', 'rust-cli'}:
        raise ValueError('this recipe release caller is not enrolled yet; preserve its existing publication authority')
    if config.get('publication'):
        if config['recipe'] == 'rust-cli':
            import cli_publication_workflows
            files.update(cli_publication_workflows.workflow_files(root, revision))
        else:
            import publication_workflows
            files.update(publication_workflows.workflow_files(root, revision))
    if config.get('operations'):
        import operational_workflows
        operations = operational_workflows.workflow_files(root, revision, config)
        if set(operations) & set(files):
            raise ValueError('each generated workflow requires one native lifecycle owner')
        files.update(operations)
    return files


def makefile(root, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook source must be pinned to a full commit')
    root = Path(root).resolve()
    config = load(root)
    recipe = config['recipe']
    suites = native_recipe.test_suites(root, config)
    digest = hashlib.sha256((root / 'cookbook.toml').read_bytes()).hexdigest()
    capabilities = ['dev'] if config.get('dev') else []
    capabilities += ['db:' + name for name in ['status', 'migrate', 'seed', 'reset']
                     if name in config.get('database', {})]
    help_lines = ['make help', 'make setup', 'make fmt', 'make build', 'make test',
                  *['make test:' + suite for suite in suites], 'make clean']
    if config.get('dev'):
        help_lines += ['make dev', 'make dev:stop', 'make dev:logs']
    for operation in ['status', 'migrate', 'seed', 'reset']:
        if operation in config.get('database', {}):
            help_lines.append('make db:' + operation)
    descriptions = {
        'make help': 'List implemented commands',
        'make setup': 'Prepare pinned tools and dependencies',
        'make fmt': 'Apply formatting',
        'make build': 'Compile, package and validate outputs',
        'make test': 'Run implemented test groups without building',
        'make test:lint': 'Check formatting, code and configuration',
        'make test:unit': 'Run isolated behavior tests',
        'make test:integration': 'Test real component boundaries',
        'make test:e2e': 'Run complete product journeys; requires current build',
        'make test:security': 'Run applicable security checks',
        'make clean': 'Remove declared outputs; preserve retained data',
        'make dev': 'Run the implemented local stack',
        'make dev:stop': 'Stop this checkout development session',
        'make dev:logs': 'Show this checkout development logs',
        'make db:status': 'Show migration status',
        'make db:migrate': 'Apply migrations to the selected development database',
        'make db:seed': 'Load development seed data',
        'make db:reset': 'Explicitly reset the local development database',
    }
    help_lines = [f'{command:<24}{descriptions[command]}' for command in help_lines]
    lines = ['# Generated cookbook entrypoint. Change cookbook.toml and regenerate.',
             '# Recipe data SHA256: ' + digest,
             'COOKBOOK_REF := ' + revision,
             'COOKBOOK_RECIPE := ' + recipe,
             'COOKBOOK_ROOT ?= .artifacts/cookbook/$(COOKBOOK_REF)',
             'COOKBOOK_TEST_SUITES := ' + ' '.join(suites),
             'COOKBOOK_CAPABILITIES :=' + (' ' + ' '.join(capabilities) if capabilities else ''),
             'COOKBOOK_EMPTY :=',
             'COOKBOOK_SPACE := $(COOKBOOK_EMPTY) $(COOKBOOK_EMPTY)',
             'cookbook_path = $(subst $(COOKBOOK_SPACE),\\$(COOKBOOK_SPACE),$(1))',
             'COOKBOOK_ENTRY := $(call cookbook_path,$(COOKBOOK_ROOT)/cookbook/recipes/$(COOKBOOK_RECIPE).mk)',
             '', 'ifeq ($(wildcard $(COOKBOOK_ENTRY)),)',
             '.DEFAULT_GOAL := help', '.PHONY: help setup', 'help:',
             '\t@printf \'%s\\n\' ' + ' '.join("'" + item + "'" for item in help_lines),
             '\t@printf \'%s\\n\' \'Run make setup once to fetch the pinned cookbook and prepare dependencies.\'',
             'setup:',
             '\t@mkdir -p "$(COOKBOOK_ROOT)"',
             '\t@git -C "$(COOKBOOK_ROOT)" init -q',
             '\t@git -C "$(COOKBOOK_ROOT)" fetch --quiet --depth=1 https://github.com/nicodes/tools.git "$(COOKBOOK_REF)"',
             '\t@git -C "$(COOKBOOK_ROOT)" checkout --quiet --detach "$(COOKBOOK_REF)"',
             '\t@test "$$(git -C "$(COOKBOOK_ROOT)" rev-parse HEAD)" = "$(COOKBOOK_REF)"',
             '\t@$(MAKE) --no-print-directory setup',
             'else', 'include $(COOKBOOK_ENTRY)', 'endif', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--revision', required=True)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--workflows', action='store_true')
    args = parser.parse_args()
    try:
        expected = makefile(args.root, args.revision)
        files = {'Makefile': expected}
        if args.workflows:
            files.update(workflow_files(args.root, args.revision))
        for name, expected in files.items():
            path = args.root / name
            if args.check:
                if not path.is_file() or path.read_text() != expected:
                    raise ValueError('generated entrypoint differs from its pinned recipe data: ' + name)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
