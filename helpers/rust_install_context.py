"""Select a stable Cargo home before explicit SDK installation, never activation."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def environment(values=None):
    result = dict(os.environ if values is None else values)
    if 'CARGO_HOME' not in result:
        home = result.get('USERPROFILE') if os.name == 'nt' else result.get('HOME')
        directory = Path(home) if home else Path.home()
        if not directory.is_absolute():
            raise ValueError('explicit Rust setup requires an absolute caller home')
        result['CARGO_HOME'] = str(directory / '.cargo')
    return result


def needs_rust(directory, selected):
    root = Path(directory)
    path = next((root / name for name in ['mise.toml', '.mise.toml'] if (root / name).is_file()), None)
    if path is None:
        return False
    if path.is_symlink() or path.stat().st_size > 262144:
        raise ValueError('explicit SDK setup requires bounded regular caller Mise source')
    declared = tomllib.loads(path.read_text()).get('tools', {})
    names = selected.split() if selected else list(declared)
    if any(name not in declared for name in names):
        raise ValueError('explicit SDK setup must retain declared tool identifiers')
    return any(name == 'rust' or name.startswith('cargo:') for name in names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    parser.add_argument('--tools', default='')
    parser.add_argument('--github-env', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = environment() if needs_rust(args.directory, args.tools) else dict(os.environ)
    if args.github_env and 'CARGO_HOME' not in os.environ and 'CARGO_HOME' in values:
        value = values['CARGO_HOME']
        if any(character in value for character in '\r\n\0'):
            raise ValueError('explicit Rust home must remain one environment value')
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write('CARGO_HOME=' + value + '\n')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if command:
        if args.github_env or command[:2] != ['mise', 'install']:
            raise ValueError('this setup boundary only preserves explicit mise install argv')
        code = subprocess.run(command, cwd=args.directory, env=values).returncode
        if code < 0:
            import signal
            signal.signal(-code, signal.SIG_DFL)
            os.kill(os.getpid(), -code)
        raise SystemExit(code)


if __name__ == '__main__':
    main()
