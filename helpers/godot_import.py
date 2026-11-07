"""Serialize native editor imports without changing authored project settings."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

MAXIMUM = 1 << 20
PROPERTY = 'editor/import/use_multiple_threads'


def _identity(info):
    return tuple(getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid'))


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or hasattr(os, 'geteuid') and info.st_uid != os.geteuid()):
        raise ValueError('Godot project settings require an owned unlinked regular file')


class _Settings:
    """Borrow one owned inode; reject replacement and unexpected native writes."""
    def __init__(self, path):
        self.path = Path(path)
        with self._open() as stream:
            self.info = os.fstat(stream.fileno())
            self.original = self._read(stream)
        self.expected = self.original

    def _open(self):
        lexical = self.path.lstat()
        _regular(lexical)
        descriptor = os.open(self.path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            opened = os.fstat(descriptor)
            _regular(opened)
            if _identity(lexical) != _identity(opened) or (
                    hasattr(self, 'info') and _identity(opened) != _identity(self.info)):
                raise ValueError('Godot project settings file identity changed')
            return os.fdopen(descriptor, 'r+b')
        except BaseException:
            os.close(descriptor)
            raise

    def _read(self, stream):
        before = os.fstat(stream.fileno())
        body = stream.read(MAXIMUM + 1)
        after = os.fstat(stream.fileno())
        stable = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        current = self.path.lstat()
        if (len(body) > MAXIMUM or len(body) != after.st_size
                or any(getattr(before, name) != getattr(after, name) for name in stable)
                or _identity(current) != _identity(after)
                or current.st_size != after.st_size or current.st_mtime_ns != after.st_mtime_ns):
            raise ValueError('Godot project settings changed or exceed their bound')
        return body

    def _write(self, body, *, restoring=False):
        with self._open() as stream:
            current = self._read(stream)
            changed = current != self.expected
            if changed and not restoring:
                raise ValueError('Godot project settings changed before import')
            if current != body:
                stream.seek(0)
                stream.write(body)
                stream.truncate()
                stream.flush()
            self.expected = body
        if changed:
            raise ValueError('native import modified Godot project settings')

    def write(self, body):
        self._write(body)

    def restore(self):
        self._write(self.original, restoring=True)


def _string(text, position):
    r"""Read native quoted keys/strings, including Godot's six-digit \U form."""
    result = []
    position += 1
    while position < len(text):
        character = text[position]
        position += 1
        if character == '"':
            value = ''.join(result)
            # Godot accepts UTF-16 escape pairs and refuses unpaired surrogates.
            value = value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
            return value, position
        if character == '\\':
            if position == len(text):
                break
            character = text[position]
            position += 1
            if character in {'u', 'U'}:
                size = 4 if character == 'u' else 6
                digits = text[position:position + size]
                if len(digits) != size or not re.fullmatch('[0-9A-Fa-f]+', digits):
                    raise ValueError('malformed native project string escape')
                character = chr(int(digits, 16))
                position += size
            else:
                character = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r'}.get(character, character)
        result.append(character)
    raise ValueError('unterminated native project string')


def _space(text, position):
    while position < len(text):
        if ord(text[position]) <= 32:
            position += 1
        elif text[position] == ';':
            newline = text.find('\n', position)
            position = len(text) if newline < 0 else newline + 1
        else:
            break
    return position


def _group(text, position):
    endings = {'[': ']', '{': '}', '(': ')'}
    stack = [endings[text[position]]]
    position += 1
    while stack and position < len(text):
        character = text[position]
        if character == '"':
            _, position = _string(text, position)
            continue
        if character == ';':
            position = _space(text, position)
            continue
        if character in endings:
            stack.append(endings[character])
        elif character in ']})':
            if character != stack.pop():
                raise ValueError('unbalanced native project value')
        position += 1
    if stack:
        raise ValueError('unterminated native project value')
    return position


def _value(text, position):
    position = _space(text, position)
    if position == len(text):
        raise ValueError('missing native project value')
    if text[position] in '&^@' and position + 1 < len(text) and text[position + 1] == '"':
        position += 1
    if text[position] == '"':
        return _string(text, position)[1]
    if text[position] in '[{(':
        return _group(text, position)
    end = position
    while end < len(text) and text[end] not in ' \t\r\n;([{':
        end += 1
    if end == position:
        raise ValueError('unsupported native project value')
    token = text[position:end]
    following = _space(text, end)
    if token in {'Array', 'Dictionary'} and following < len(text) and text[following] == '[':
        following = _space(text, _group(text, following))
    if following < len(text) and text[following] == '(':
        return _group(text, following)
    return end


def _properties(raw):
    """Find native assignment names; leave every Variant value unmodified."""
    if b'\0' in raw:
        raise ValueError('native project settings may not terminate before the import policy')
    text = raw.decode('utf-8-sig')
    section, position = '', 0
    while (position := _space(text, position)) < len(text):
        if text[position] == '[':
            start = position = position + 1
            while position < len(text) and (text[position] != ']' or text[position - 1] == '\\'):
                position += 1
            if position == len(text):
                raise ValueError('unterminated native project section')
            # Native simple tags trim only code points <=32. ProjectSettings
            # ignores empty tags instead of clearing the preceding section.
            name = text[start:position].strip(''.join(chr(value) for value in range(33)))
            if name:
                section = name
            position += 1
            continue
        key = ''
        while position < len(text) and text[position] != '=':
            if text[position] == '"':
                key, position = _string(text, position)
            elif text[position] == ';':
                position = _space(text, position)
            else:
                if ord(text[position]) > 32:
                    key += text[position]
                position += 1
        if not key or position == len(text):
            raise ValueError('missing native project assignment')
        yield section + '/' + key if section else key
        position = _value(text, position + 1)


def _serial_settings(raw):
    keys = {PROPERTY}
    keys.update(name for name in _properties(raw) if name.startswith(PROPERTY + '.'))
    newline = re.search(rb'\r\n|\r|\n', raw)
    newline = newline[0] if newline else b'\n'
    suffix = b'' if raw.endswith((b'\r', b'\n')) else newline
    suffix += newline + b'[editor]' + newline
    for key in sorted(keys):
        name = key.removeprefix('editor/')
        encoded = name if key == PROPERTY else json.dumps(name, ensure_ascii=False)
        suffix += encoded.encode('utf-8') + b'=false' + newline
    return raw + suffix


@contextmanager
def serial_import(project):
    """Use serial resource importing for one native invocation, then restore."""
    binary = Path(project) / 'project.binary'
    if binary.exists() or binary.is_symlink():
        raise ValueError('native import settings require project.godot without a binary override')
    settings = _Settings(Path(project) / 'project.godot')
    settings.write(_serial_settings(settings.original))
    try:
        yield
    finally:
        settings.restore()
