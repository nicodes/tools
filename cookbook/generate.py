#!/usr/bin/env python3
"""Generate the small pinned Make entrypoint from caller-owned recipe data."""
import argparse
import hashlib
from pathlib import Path
import re

from configuration import load


def makefile(root, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook source must be pinned to a full commit')
    root = Path(root).resolve()
    config = load(root)
    recipe = config['recipe']
    suites = ['lint', 'unit', 'security', *[s for s in ['integration', 'e2e'] if s in config.get('tests', {})]]
    digest = hashlib.sha256((root / 'cookbook.toml').read_bytes()).hexdigest()
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
    args = parser.parse_args()
    try:
        expected = makefile(args.root, args.revision)
        path = args.root / 'Makefile'
        if args.check:
            if not path.is_file() or path.read_text() != expected:
                raise ValueError('generated Makefile differs from its pinned recipe data')
        else:
            path.write_text(expected)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
