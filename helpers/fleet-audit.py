#!/usr/bin/env python3
"""Refuse when the products sharing this repository's engineering snapshot disagree.

pins.mjs makes ONE product internally consistent: its lockfiles, its Go
version, its image digests and its recorded-action pins all have to agree with
each other. Nothing has ever checked that products agree with EACH OTHER, and
the cost of that gap was not theoretical -- an audit in September 2026 found
five different snapshot revisions live at once, the vendored tree present in
five different sizes, and the recorded-action pin gate in four incompatible
states across nine products, including one that carried a second pin record in
its own format at its own path that nothing read.

None of that was anybody's mistake. It is what happens when the only feedback
is per-repository: every product was individually green.

WHAT THIS ASSERTS IS AGREEMENT, NOT A CONSTANT. There is no table here saying
which caddy digest is correct. The rule is that every product using caddy uses
the SAME one, so dependabot bumping the first product turns this red and the
answer is to bump the rest. A table of expected values would need editing on
every bump and would be wrong between the edit and the bump.

The exception is the small set of absolute rules -- a product that uses
recorded-actions must carry the record where pins.mjs looks for it, and must
actually run pins.mjs -- because those are not matters of agreement. A fleet
that unanimously fails to check something is still not checking it.

Facts come in from the GitHub API; the judgement is a pure function over them,
so the tests exercise it without a network.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A product either vendors the snapshot or installs it, and during the
# migration the fleet has both. Each is read in its own way and then compared
# on the same ground: the cicd COMMIT the product is running.
CANONICAL_PIN_RECORDS = ('ACTION-PINS.json', 'scripts/engineering/ACTION-PINS.json')
CANONICAL_PIN_RECORD = CANONICAL_PIN_RECORDS[0]
SNAPSHOT = 'scripts/engineering/SOURCE.json'
INSTALLED = re.compile(r'"http:cicd-engineering"\s*=\s*\{[^}]*?version\s*=\s*"([^"]+)"', re.S)


class Finding:
    """One disagreement, with the products on each side of it.

    Carries the split rather than a sentence, because "six say 1.27.0 and
    three say 1.27.1" is the thing an operator acts on and a bare "go
    versions differ" is not.
    """

    def __init__(self, kind, subject, groups, absolute=False):
        self.kind = kind
        self.subject = subject
        self.groups = groups          # value -> [product, ...]
        self.absolute = absolute

    def __str__(self):
        if self.absolute:
            who = ', '.join(sorted(self.groups.get('', [])))
            return f'{self.kind}: {self.subject}: {who}'
        parts = [f'{value or "(absent)"} = {", ".join(sorted(products))}'
                 for value, products in sorted(self.groups.items(), key=lambda kv: -len(kv[1]))]
        return f'{self.kind}: {self.subject}: ' + ' | '.join(parts)


def disagreement(kind, subject, values):
    """values: product -> value. A Finding when they are not unanimous."""
    groups = defaultdict(list)
    for product, value in values.items():
        groups[value].append(product)
    if len(groups) <= 1:
        return None
    return Finding(kind, subject, dict(groups))


def audit(facts, fleet):
    """facts: product -> {snapshot_revision, snapshot_files, tools, images,
    action_pins, pin_record_path, uses_recorded_actions, runs_pin_gate}."""
    findings = []
    products = sorted(facts)
    profile = {p: fleet['products'].get(p, {}).get('profile', 'service') for p in products}

    missing = [p for p in fleet['products'] if p not in facts]
    if missing:
        findings.append(Finding('unreachable', 'declared in FLEET.json but not read',
                                {'': missing}, absolute=True))

    # --- the snapshot itself ------------------------------------------
    found = disagreement('snapshot', 'vendored cicd revision',
                         {p: facts[p]['snapshot_revision'] for p in products})
    if found:
        findings.append(found)
    # The file LIST, not the hashes: SOURCE.json already pins the contents of
    # each file it names, and cannot say anything about a file it does not.
    # Five different subsets of the same snapshot all validated individually.
    # Only among products that still vendor. One that installs the snapshot
    # has no copied files, and reporting "0 files" against everyone else's 66
    # would be a finding about the mechanism rather than about drift.
    found = disagreement('snapshot', 'vendored file set',
                         {p: '%d files' % len(facts[p]['snapshot_files']) for p in products
                          if facts[p]['snapshot_files'] is not None})
    if found:
        findings.append(found)

    # --- sample-host-action pins -------------------------------------------
    wrong_place = [p for p in products
                   if facts[p]['uses_recorded_actions']
                   and facts[p]['pin_record_path'] not in CANONICAL_PIN_RECORDS + (None,)]
    if wrong_place:
        findings.append(Finding('pins', 'pin record is somewhere pins.mjs does not look: '
                                        f'it reads {" or ".join(CANONICAL_PIN_RECORDS)}',
                                {'': wrong_place}, absolute=True))
    no_record = [p for p in products
                 if facts[p]['uses_recorded_actions'] and facts[p]['pin_record_path'] is None]
    if no_record:
        findings.append(Finding('pins', 'uses recorded-actions with no pin record',
                                {'': no_record}, absolute=True))
    # Two different failures, and saying so matters: one is a wiring mistake,
    # the other is a product that was never given the gate. Reporting both as
    # "never runs it" sends somebody looking for a call site that does not
    # exist.
    decorative = [p for p in products if facts[p]['uses_recorded_actions']
                  and facts[p].get('has_pin_gate', True) and not facts[p]['runs_pin_gate']]
    if decorative:
        findings.append(Finding('pins', 'vendors pins.mjs but never invokes it, so the gate is decorative',
                                {'': decorative}, absolute=True))
    ungated = [p for p in products if facts[p]['uses_recorded_actions']
               and not facts[p].get('has_pin_gate', True)]
    if ungated:
        findings.append(Finding('pins', 'uses recorded-actions and does not vendor pins.mjs at all',
                                {'': ungated}, absolute=True))
    # One tag per action across the fleet. Products that do not use an action
    # say nothing about it.
    for action in sorted({a for p in products for a in facts[p]['action_pins']}):
        found = disagreement('pins', f'recorded-actions/{action}',
                             {p: facts[p]['action_pins'][action]
                              for p in products if action in facts[p]['action_pins']})
        if found:
            findings.append(found)

    # --- tools ----------------------------------------------------------
    for tool in fleet['shared_tools']:
        found = disagreement('tools', tool,
                             {p: facts[p]['tools'].get(tool) for p in products
                              if tool in facts[p]['tools'] or facts[p]['tools']})
        if found:
            findings.append(found)
    for tool in fleet['profile_tools']:
        for name in sorted(set(profile.values())):
            members = [p for p in products if profile[p] == name]
            found = disagreement('tools', f'{tool} ({name})',
                                 {p: facts[p]['tools'].get(tool) for p in members})
            if found:
                findings.append(found)

    # --- base images ------------------------------------------------------
    # Keyed on the image name AND tag, so caddy:2-alpine is compared with
    # caddy:2-alpine. The name alone was wrong: sample-service builds its Go API on
    # golang:1.27.1-bookworm because it needs a C toolchain for cgo and the
    # Swiss Ephemeris, and everyone else builds on golang:1.27.1-alpine. Those
    # digests can never match, so the name-keyed check reported a permanent
    # disagreement about a deliberate, documented difference -- the kind of
    # finding that teaches people to stop reading the report. A product that
    # does not use an image is not asked about it.
    for image in sorted({i for p in products for i in facts[p]['images']}):
        found = disagreement('images', image,
                             {p: facts[p]['images'][image] for p in products if image in facts[p]['images']})
        if found:
            findings.append(found)
    return findings


# --- reading a product ----------------------------------------------------

def gh_json(path):
    out = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def release_commit(version, _cache={}):
    """The commit a cicd release tag peels to, for comparing with a vendored revision.

    An installed product declares a version; a vendored one records a commit.
    They are the same fact in two notations, and resolving one into the other
    is what lets a half-migrated fleet still be checked for agreement.

    Peeled, not the tag object: an annotated tag and its commit are different
    40-hex strings, and this value is compared as a string.
    """
    if version not in _cache:
        got = gh_json(f'repos/nicodes/tools/commits/v{version}')
        _cache[version] = (got or {}).get('sha', '')[:10] or None
    return _cache[version]


def gh_file(repo, path, ref='HEAD'):
    got = gh_json(f'repos/{repo}/contents/{path}?ref={ref}')
    if got is None or 'content' not in got:
        return None
    import base64
    return base64.b64decode(got['content']).decode('utf-8', 'replace')


def gh_tree(repo, ref='HEAD'):
    got = gh_json(f'repos/{repo}/git/trees/{ref}?recursive=1')
    if got is None:
        return []
    return [entry['path'] for entry in got.get('tree', []) if entry['type'] == 'blob']


def read_product(repo, action_repository=None):
    """Everything the audit needs, from one product's default branch."""
    tree = gh_tree(repo)
    if not tree:
        return None
    facts = {'snapshot_revision': None, 'snapshot_files': [], 'tools': {},
             'images': {}, 'action_pins': {}, 'pin_record_path': None,
             'uses_recorded_actions': False, 'runs_pin_gate': False,
             'has_pin_gate': 'scripts/engineering/helpers/pins.mjs' in tree,
             'snapshot_source': None}

    mise = gh_file(repo, '.mise.toml') or ''
    installed = INSTALLED.search(mise)
    source = gh_file(repo, SNAPSHOT)
    if installed:
        # The release tag is what the product declares; the commit is what
        # the vendoring products declare. Compare on the commit, or a
        # migrating fleet reads as a fleet in disagreement with itself.
        facts['snapshot_source'] = 'installed'
        facts['snapshot_revision'] = release_commit(installed.group(1))
        # Nothing is copied in, so there is no file set to compare. Saying
        # "0 files" against everyone else's 66 would be a finding about the
        # mechanism, not about drift.
        facts['snapshot_files'] = None
        facts['has_pin_gate'] = True
    elif source:
        facts['snapshot_source'] = 'vendored'
        facts['snapshot_revision'] = json.loads(source).get('revision', '')[:10]
        facts['snapshot_files'] = [p for p in tree if p.startswith('scripts/engineering/')]

    for tool in ('go', 'bun', 'node', 'python', 'actionlint', 'shellcheck'):
        match = re.search(rf'^{tool}\s*=\s*"([^"]+)"', mise, re.M)
        if match:
            facts['tools'][tool] = match.group(1)

    # The pin record, wherever it is. Finding it somewhere else is a finding.
    for candidate in CANONICAL_PIN_RECORDS:
        if candidate in tree:
            facts['pin_record_path'] = candidate
            body = gh_file(repo, candidate)
            try:
                record = json.loads(body)
                if action_repository is not None and record.get('repository') != action_repository:
                    raise ValueError('action record differs from the caller-selected repository')
                action_repository = action_repository or record.get('repository')
                pins = record.get('pins', {})
            except (json.JSONDecodeError, AttributeError):
                pins = {}
            if isinstance(pins, dict):
                facts['action_pins'] = {k: v.get('tag') for k, v in pins.items()
                                        if isinstance(v, dict)}
            break

    for path in tree:
        if not path.startswith('.github/'):
            continue
        body = gh_file(repo, path) or ''
        if action_repository and action_repository.removeprefix('https://github.com/') + '/' in body:
            facts['uses_recorded_actions'] = True
        for name, tag, digest in re.findall(r'^FROM\s+([a-z0-9./-]+):([\w.-]+)@(sha256:[a-f0-9]{64})',
                                            body, re.M):
            facts['images'][f'{name}:{tag}'] = digest[:19]

    for path in tree:
        if path.endswith(('Dockerfile',)) or '.Dockerfile' in path or path.endswith('Dockerfile.dev'):
            for name, tag, digest in re.findall(r'^FROM\s+([a-z0-9./-]+):([\w.-]+)@(sha256:[a-f0-9]{64})',
                                                gh_file(repo, path) or '', re.M):
                facts['images'][f'{name}:{tag}'] = digest[:19]

    # Invoked, not merely present. A gate nothing calls is not a gate.
    for path in tree:
        if path.startswith('scripts/engineering/'):
            continue
        if path.startswith(('scripts/', '.github/', 'Makefile')):
            if 'pins.mjs' in (gh_file(repo, path) or ''):
                facts['runs_pin_gate'] = True
                break
    return facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fleet', type=Path, required=True)
    parser.add_argument('--facts', type=Path,
                        help='read facts from this JSON instead of the GitHub API')
    parser.add_argument('--write-facts', type=Path, help='save what was read, for a later --facts run')
    args = parser.parse_args()
    fleet = json.loads(args.fleet.read_text())

    if args.facts:
        facts = json.loads(args.facts.read_text())
    else:
        facts = {}
        for repo in fleet['products']:
            print(f'reading {repo}', file=sys.stderr)
            repository = fleet['baseline'].get('action_repository')
            if not isinstance(repository, str) or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+', repository):
                raise ValueError('caller policy requires a reviewed baseline.action_repository')
            got = read_product(repo, repository)
            if got is not None:
                facts[repo] = got
        if args.write_facts:
            args.write_facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + '\n')

    findings = audit(facts, fleet)
    for finding in findings:
        print(finding)
    print(f'\n{len(findings)} disagreement(s) across {len(facts)} product(s).')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
