#!/usr/bin/env python3
"""Allow only an independently verified, routine single-dependency update."""
import os
import re

SENSITIVE = re.compile(r'clerk|pocketbase|jsonwebtoken|(?:^|[/@-])(?:auth|oauth|oidc|jwt|jose|crypto|noble|peculiar|stablelib|passport|bcrypt|scrypt|argon2|tweetnacl|libsodium|elliptic|ed25519|curve25519|rsa|openpgp|sshpk|pkijs|node-forge|firebase)(?:$|[/@-])', re.I)


def eligible(names, previous, new, update_type, group=''):
    dependencies = [value.strip() for value in names.split(',') if value.strip()]
    if group or len(dependencies) != 1 or SENSITIVE.search(dependencies[0]):
        return False
    if update_type not in {'version-update:semver-patch', 'version-update:semver-minor'}:
        return False
    versions = [re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value) for value in [previous, new]]
    if not all(versions):
        return False  # Unknown versions, prereleases and digest-only updates need review.
    old, current = [tuple(map(int, value.groups())) for value in versions]
    if current <= old or current[0] != old[0]:
        return False
    if old[0] == 0 and current[1] != old[1]:
        return False
    return True


if __name__ == '__main__':
    result = eligible(os.environ.get('DEPENDENCY_NAMES', ''), os.environ.get('PREVIOUS_VERSION', ''),
                      os.environ.get('NEW_VERSION', ''), os.environ.get('UPDATE_TYPE', ''),
                      os.environ.get('DEPENDENCY_GROUP', ''))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'eligible={str(result).lower()}\n')
    print('Routine update eligible for the full-check merge gate' if result else 'Dependency update requires review')
