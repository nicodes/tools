#!/usr/bin/env python3
"""Check tracked documentation layout and the reviewed usage README digest.

Only the checked-out commit is read. A legacy `[history]` table in the policy
file is accepted and ignored.
"""
import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tomllib

MARKDOWN = {'.md', '.markdown', '.mdown', '.mkd', '.mkdn', '.mdwn', '.mdx', '.rmd', '.qmd'}
OTHER_DOCS = {'.rst', '.adoc', '.asciidoc'}


def check(root):
    raw = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root)
    entries = {}
    errors = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        metadata, encoded = record.split(b'\t', 1)
        mode, _oid, stage = metadata.decode().split()
        name = encoded.decode('utf-8', 'surrogateescape')
        if stage != '0':
            errors.append('unmerged index entry: ' + name)
        entries[name] = mode
        path = PurePosixPath(name)
        if name != 'README.md' and (path.suffix.lower() in MARKDOWN | OTHER_DOCS
                                    or path.name.lower().startswith('readme.')):
            errors.append('documentation must live in the private docs repository: ' + name)
    if entries.get('README.md') != '100644' and entries.get('README.md') != '100755':
        errors.append('a regular, tracked root README.md is required')
    else:
        readme = root / 'README.md'
        if readme.is_symlink() or not readme.is_file():
            errors.append('root README.md must be a regular file')
        else:
            try:
                body = readme.read_bytes()
                if not any(line.startswith('# ') for line in body.decode('utf-8').splitlines()):
                    errors.append('root README.md needs a product heading')
                policy_path = root / '.github/readme-source.toml'
                if policy_path.is_symlink():
                    raise ValueError('README source policy cannot be a symlink')
                policy = tomllib.loads(policy_path.read_text())
                if policy.get('version') != 1:
                    raise ValueError('unsupported README source policy')
                if hashlib.sha256(body).hexdigest() != policy.get('sha256'):
                    errors.append('README differs from its reviewed publication; update the private source first')
            except (OSError, ValueError, UnicodeError) as error:
                errors.append('invalid README source policy or README: ' + str(error))
    return errors


def main():
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    errors = check(root)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print('Documentation policy passed: one root README.md, reviewed source digest matches.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
