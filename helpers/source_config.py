"""Read and write bounded TOML source data, separate from machine JSON protocols."""
import argparse
import json
import math
import os
from pathlib import Path
import stat
import sys
import tomllib


DEFAULT_MAXIMUM = 8 << 20


def _value(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('source table keys must be strings')
        for item in value.values():
            _value(item)
    elif isinstance(value, list):
        for item in value:
            _value(item)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError('source numbers must be finite')
    elif type(value) is int:
        if not -(2 ** 63) <= value < 2 ** 63:
            raise ValueError('source integers must remain signed 64-bit values')
    elif type(value) not in {str, bool, int}:
        raise ValueError('source values must be strings, booleans, numbers, arrays or tables')


def loads(raw, *, maximum=DEFAULT_MAXIMUM):
    if type(maximum) is not int or maximum < 1:
        raise ValueError('source byte limit must be a positive integer')
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise ValueError('TOML source exceeds its byte bound')
    result = tomllib.loads(raw.decode('utf-8'))
    _value(result)
    return result


def read_bytes(path, *, maximum=DEFAULT_MAXIMUM):
    path = Path(path)
    if path.suffix != '.toml' or path.is_symlink():
        raise ValueError('source must be a regular TOML file')
    if type(maximum) is not int or maximum < 1:
        raise ValueError('source byte limit must be a positive integer')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('source must be a bounded unlinked regular file')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    current = path.stat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if (len(raw) > maximum or path.is_symlink() or identity(before) != identity(after)
            or identity(before) != identity(current) or current.st_nlink != 1):
        raise ValueError('source changed while it was read')
    return raw


def read(path, *, maximum=DEFAULT_MAXIMUM):
    return loads(read_bytes(path, maximum=maximum), maximum=maximum)


def _literal(value):
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(key, ensure_ascii=False) + ' = ' + _literal(item)
                               for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(map(_literal, value)) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def dumps(data):
    """Encode one source table; each family explicitly owns any array wrapper."""
    if not isinstance(data, dict):
        raise ValueError('TOML source requires a root table')
    _value(data)
    text = ''.join(json.dumps(key, ensure_ascii=False) + ' = ' + _literal(value) + '\n'
                   for key, value in data.items())
    raw = text.encode('utf-8')
    if not equal(data, loads(raw, maximum=max(1, len(raw)))):
        raise ValueError('source serialization changed its typed values')
    return text


def equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if type(left) is float and left == right == 0:
        return math.copysign(1, left) == math.copysign(1, right)
    return left == right


def write(path, data):
    path = Path(path)
    if (path.suffix != '.toml' or path.is_symlink()
            or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1))):
        raise ValueError('source output must be a regular TOML path')
    path.write_text(dumps(data), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--maximum', type=int, default=DEFAULT_MAXIMUM)
    args = parser.parse_args()
    try:
        value = read(args.path, maximum=args.maximum)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    json.dump(value, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
