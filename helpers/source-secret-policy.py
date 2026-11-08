#!/usr/bin/env python3
"""Check source-declared secret delivery and logical credential readers."""

import argparse
import re
import sys
from pathlib import Path

import tomllib


# Secret-shaped literals. Deliberately a short list of things that are secret by
# construction rather than a generic entropy guess: a false positive here blocks
# a merge, and the check people route around is worth nothing. Publishable keys
# (pk_live_, pk_test_) are absent on purpose -- they are meant to be in the
# client bundle, and flagging them is how a check earns a blanket ignore.
SECRET_SHAPES = [
    (re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{8,}"), "a Clerk/Stripe secret key"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{8,}"), "a webhook signing secret"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "a GitHub token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    # A DSN carrying a password. The password has to look like a value rather
    # than a placeholder, so ${...} interpolation and the empty password pass.
    (
        re.compile(r"\b[a-z][a-z0-9+.-]*://[A-Za-z0-9_.-]+:(?!\s*\$)[^\s:@/${}]{6,}@"),
        "a connection string with a password in it",
    ),
]

# Ways to reach the host's secret store that are not set-secrets. Matched
# against the workflow text, which is why they are written as the shapes a
# workflow actually contains rather than as parsed steps: the point is that
# NOTHING here writes secrets.env, however it is spelled.
BYPASS_SHAPES = [
    (re.compile(r"set-secret-[A-Za-z0-9_-]+"), "calls the host's set-secret command directly"),
    (re.compile(r"secrets\.env"), "names the host's secrets.env"),
    (re.compile(r"\bscp\b"), "copies a file to the host with scp"),
]


class Findings:
    """Collect every violation before exiting, so one run lists them all.

    A check that stops at the first failure turns a five-minute fix into five
    round trips through CI.
    """

    def __init__(self):
        self.errors = []
        self.notes = []

    def error(self, where, message):
        self.errors.append(f"{where}: {message}")

    def note(self, message):
        self.notes.append(message)

    def report(self):
        for note in self.notes:
            print(f"note: {note}")
        for error in self.errors:
            print(f"::error::{error}", file=sys.stderr)
        if self.errors:
            print(
                f"\n{len(self.errors)} violation(s) of the secret rule. "
                "See komizo-actions/docs/secrets.md.",
                file=sys.stderr,
            )
            return 1
        print("secret rule: ok")
        return 0


def load_application(path, findings):
    """Read logical environment-file IDs without resolving host credentials."""
    try:
        file = Path(path)
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 1 << 20:
            raise ValueError('requires a bounded regular application declaration')
        doc = tomllib.loads(file.read_text())
    except (OSError, ValueError) as exc:
        findings.error(path, f'could not be read as TOML: {exc}')
        return {}
    result = {}
    for group in ('services', 'jobs'):
        rows = doc.get(group, {})
        if not isinstance(rows, dict):
            findings.error(path, f'{group} must be a mapping')
            continue
        for name, value in rows.items():
            if name in result or not isinstance(value, dict):
                findings.error(path, 'credential readers must have distinct service/job names')
            else:
                result[name] = value
    return result


def normalise(path):
    """Strip one leading "./" and nothing else.

    `lstrip("./")` strips CHARACTERS, not a prefix: it turns
    "../secrets/current/api.env" into "secrets/current/api.env", which matches
    the scoped-env prefix and passes. A path that climbs out of the app
    directory is exactly the case this checker exists for, so it must survive
    normalisation intact and be judged on what it says.
    """
    while path.startswith("./"):
        path = path[2:]
    return path


def env_files(service):
    raw = service.get('environment_files', [])
    if (not isinstance(raw, list) or len(set(raw)) != len(raw)
            or any(not isinstance(item, str) or not re.fullmatch('[a-z][a-z0-9-]*', item) for item in raw)):
        raise ValueError('environment_files must name distinct logical credential IDs')
    return raw


def parse_allowlist(values, findings):
    """`path: service[,service]` lines -> {path: {services}}.

    Host-local secret files are named one at a time, with the services allowed
    to read them, so adding a fifth reader to postgres-owner.env is a diff in
    the workflow rather than a line in a compose file nobody reviews.
    """
    allowed = {}
    for value in values:
        value = value.strip()
        if not value or value.startswith("#"):
            continue
        if ":" not in value:
            findings.error("host-only", f"{value!r} is not 'path: service[,service]'")
            continue
        path, services = value.split(":", 1)
        allowed[normalise(path.strip())] = {
            s.strip() for s in services.split(",") if s.strip()
        }
    return allowed


def check_no_committed_secrets(paths, findings):
    """Rule 5. A secret in a tracked file is already published."""
    for path in paths:
        try:
            text = Path(path).read_text()
        except OSError:
            continue  # a missing file is reported by whoever needed it
        for number, line in enumerate(text.splitlines(), 1):
            for shape, what in SECRET_SHAPES:
                if shape.search(line):
                    findings.error(
                        f"{path}:{number}",
                        f"looks like {what}, committed. Move it to a GitHub "
                        "secret and deliver it as KOMIZO_SECRET_<NAME>, then "
                        "rotate it -- it is in the history either way.",
                    )
                    break


def check_delivery(workflow, findings):
    """Rules 1 and 2: what the workflow declares, and how.

    Returns the set of names delivered, which the drift check compares against
    the host.
    """
    try:
        text = Path(workflow).read_text()
    except OSError as exc:
        findings.error(workflow, f"could not be read: {exc}")
        return set()

    delivered = set()
    # The declaration is the env: line itself -- `KOMIZO_SECRET_X: <expr>` --
    # so name and value are checked together. Read line by line rather than
    # through the YAML parser because a workflow is also checked for the shapes
    # below, and those live in `run:` blocks as text.
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*KOMIZO_SECRET_([A-Z0-9_]+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip().strip("\"'")
        delivered.add(name)
        if not re.fullmatch(r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}", value):
            if re.fullmatch(r"\$\{\{\s*vars\.[A-Za-z0-9_]+\s*\}\}", value):
                findings.note(
                    f"{workflow}:{number}: {name} is delivered from a repository "
                    "variable, so its value is public. That is fine for a "
                    "nonsecret, but it does not need the secret store."
                )
                continue
            findings.error(
                f"{workflow}:{number}",
                f"KOMIZO_SECRET_{name} must be a ${{{{ secrets.NAME }}}} "
                f"expression; got {value!r}. A literal here is a committed "
                "secret, and a computed value has no record of what was sent.",
            )

    if not delivered:
        findings.note(
            f"{workflow} delivers no secrets. Correct for an app that needs "
            "none; suspicious for one whose host has keys (see --host-keys)."
        )

    # Rule 3. Confined to run: blocks -- the env: lines above legitimately
    # contain the string set-secrets, and the deploy action's own `uses:` line
    # names the directory.
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("uses:", "- uses:", "#")):
            continue
        if re.match(r"\s*KOMIZO_SECRET_", line):
            continue
        for shape, what in BYPASS_SHAPES:
            if shape.search(line):
                findings.error(
                    f"{workflow}:{number}",
                    f"{what}. The host's secret store is written by the "
                    "set-secrets action and by nothing else; a value placed "
                    "any other way has no provenance.",
                )
                break
    return delivered


def check_readers(services, scoped_dir, host_only, secrets_env_readers, findings):
    """Account for every logical credential ID and require exact declared readers."""
    del scoped_dir
    allowed = {key: set(value) for key, value in host_only.items()}
    if secrets_env_readers:
        if 'runtime' in allowed and allowed['runtime'] != secrets_env_readers:
            findings.error('runtime', 'has conflicting reader declarations')
        allowed['runtime'] = set(secrets_env_readers)
    observed = {}
    for name, service in sorted(services.items()):
        try:
            entries = env_files(service)
        except (ValueError, TypeError):
            findings.error(name, 'environment_files must contain distinct logical credential IDs')
            continue
        for identity in entries:
            observed.setdefault(identity, set()).add(name)
            if identity not in allowed or name not in allowed[identity]:
                findings.error(name, f'reads undeclared credential {identity!r}; declare its exact allowed readers')
    for identity, readers in sorted(allowed.items()):
        missing = readers - observed.get(identity, set())
        if missing:
            findings.error(identity, 'declares readers that receive no credential: ' + ', '.join(sorted(missing)))


def check_drift(delivered, host_keys_path, findings):
    """The half a repository cannot see: what is actually on the box.

    set-secret writes and never deletes, so removing a name from cd.yml leaves
    the value on the host indefinitely. Extra keys are the interesting
    direction: each one is either a credential nobody is rotating or a
    hand-placed value, and both are the thing this rule exists to prevent.
    """
    try:
        text = Path(host_keys_path).read_text()
    except OSError as exc:
        findings.error(host_keys_path, f"could not be read: {exc}")
        return
    host = {
        line.strip().split("=", 1)[0]
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    host.discard("")
    for name in sorted(host - delivered):
        findings.error(
            "host secrets.env",
            f"{name} is on the host but no workflow delivers it. It was placed "
            "by hand or left behind by a workflow that no longer sets it; "
            "either way nothing rotates it. Add it to the deploy step, or "
            "remove it from the host and rotate it at its provider.",
        )
    for name in sorted(delivered - host):
        findings.note(
            f"{name} is delivered by CI but is not on the host yet -- expected "
            "if it was added since the last deploy."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check an app against the komizo secret rule.",
    )
    parser.add_argument("--application", required=True, help="path to the source application TOML")
    parser.add_argument(
        "--workflow",
        required=True,
        action="append",
        help="path to a deploy workflow; repeat for preview and production",
    )
    parser.add_argument(
        "--scoped-env-dir",
        default="secrets/current",
        help="reserved namespace hint; logical IDs are explicitly declared",
    )
    parser.add_argument(
        "--host-only",
        default=[],
        action="append",
        metavar="PATH:SERVICE[,SERVICE]",
        help="a logical credential ID and the exact services/jobs allowed to read it",
    )
    parser.add_argument(
        "--secrets-env-services",
        default="",
        help="comma-separated services/jobs that receive the runtime credential ID",
    )
    parser.add_argument(
        "--host-keys",
        default="",
        help="file of key names read off the host, to check for drift",
    )
    args = parser.parse_args(argv)

    findings = Findings()
    host_only = parse_allowlist(args.host_only, findings)
    readers = {s.strip() for s in args.secrets_env_services.split(",") if s.strip()}

    check_no_committed_secrets([args.application, *args.workflow], findings)
    delivered = set()
    for workflow in args.workflow:
        delivered |= check_delivery(workflow, findings)
    services = load_application(args.application, findings)
    if services:
        check_readers(services, args.scoped_env_dir, host_only, readers, findings)
    if args.host_keys:
        check_drift(delivered, args.host_keys, findings)
    return findings.report()


if __name__ == "__main__":
    sys.exit(main())
