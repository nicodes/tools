#!/usr/bin/env python3
"""Generate the small pinned Make entrypoint from caller-owned recipe data."""
import argparse
import hashlib
from pathlib import Path
import re

from configuration import load


def workflow_files(root, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    config = load(root)
    if config['recipe'] != 'godot-addon':
        raise ValueError('workflow callers for this recipe are not implemented yet')
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
'''.replace('PIN', revision)}
    if config.get('publication'):
        import publication_workflows
        files.update(publication_workflows.workflow_files(root, revision))
    return files


def makefile(root, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook source must be pinned to a full commit')
    root = Path(root).resolve()
    config = load(root)
    recipe = config['recipe']
    suites = ['lint', 'unit', 'security', *[s for s in ['integration', 'e2e'] if s in config.get('tests', {})]]
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
