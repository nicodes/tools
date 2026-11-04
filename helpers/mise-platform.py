#!/usr/bin/env python3
"""Select the reviewed Mise executable identity for an Actions runner."""
import argparse

VERSION = '2026.9.12'
# Official v2026.9.12 SHASUMS256.txt, executable hashes (not archive hashes).
CHECKSUMS = {
    ('Linux', 'X64'): 'e79ae57945034903aee8aa2ea66b4c7ca9cd4f4edd5a8a78a589cbae6d0f428a',
    ('Linux', 'ARM64'): 'f344c6961190ed2f68e595ed7cb4f03c36c17812bd608886bec799a3082180ff',
    ('macOS', 'X64'): 'fa1e35456f1e27a4555a1ccec82d5367d7037f3b4f46d48b83c4a3bceb4d78f2',
    ('macOS', 'ARM64'): 'f20d7cc555a5b0ee7b8a504acbc2583be1b0848d64115cfbe84119ceb705025b',
    ('Windows', 'X64'): '8bc3a2c46246bb00fc49f467c678cf9ff321f707c533ad707061d0f8c7b639b2',
    ('Windows', 'ARM64'): '6beeb61b829ba60ef3bb1fdda5717a41b8fb91bd400109de8b05b1700a107d8e',
}


def select(system, architecture):
    try:
        return {'version': VERSION, 'sha256': CHECKSUMS[(system, architecture)]}
    except KeyError:
        raise ValueError('unsupported SDK runner platform or architecture') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', required=True)
    parser.add_argument('--arch', required=True)
    args = parser.parse_args()
    try:
        for name, value in select(args.os, args.arch).items():
            print(name + '=' + value)
    except ValueError as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
