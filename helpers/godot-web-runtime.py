#!/usr/bin/env python3
"""Apply reviewed Godot/Emscripten presentation and object-lifetime compatibility."""
import argparse
import hashlib
from pathlib import Path
import re

REVIEWED_SHA256 = frozenset({
    'f8740a96b208882324070e7d247505e10ab88058ed60b3eb9d650742b4dc2137',  # release
    '46004c61dfcf373a9b5a424e9c50ee860608270f13da90d586fe4a05b5bb206a',  # debug
    'cf923ee91288f2967cf4aa4532e3daae9e7bac9e9518fd7a764047ff89ad8a6c',  # dlink release
    '06e3f0663b7a6c449e7faa08d1647b6e5ab2fce26afe0931971b9b8183044f4b',  # dlink debug
})
DLINK_SHA256 = frozenset({
    'cf923ee91288f2967cf4aa4532e3daae9e7bac9e9518fd7a764047ff89ad8a6c',
    '06e3f0663b7a6c449e7faa08d1647b6e5ab2fce26afe0931971b9b8183044f4b',
})
ALLOCATION = 'getNewId:table=>{var ret=GL.counter++;for(var i=table.length;i<ret;i++){table[i]=null}return ret}'
SPARSE_ALLOCATION = 'getNewId:table=>GL.counter++'
DELETED_SLOTS = ('contexts[contextHandle]', 'buffers[id]', 'framebuffers[id]',
                 'programs[id]', 'queries[id]', 'renderbuffers[id]', 'shaders[id]',
                 'syncs[id]', 'textures[id]', 'vaos[id]')


def release_gl_table_slots(source):
    digest = hashlib.sha256(source.encode()).hexdigest()
    if digest not in REVIEWED_SHA256:
        raise ValueError('Unreviewed WebGL runtime; review handle table consumers before changing the template')
    replacements = [(ALLOCATION, SPARSE_ALLOCATION)]
    replacements.extend((f'GL.{slot}=null', f'delete GL.{slot}') for slot in DELETED_SLOTS)
    if digest in DLINK_SHA256:
        replacements.extend((f'GL.{slot}=null', f'delete GL.{slot}')
                            for slot in ('samplers[id]', 'transformFeedbacks[id]'))
    for old, new in replacements:
        # The extension template exports both EXT and WebGL2 query deletion.
        # Its 143 table accesses match across debug/release; the only
        # array search is shaders.indexOf(live_object), which skips holes. No
        # consumer depends on dense slots, table length or null tombstones.
        expected = 2 if digest in DLINK_SHA256 and old == 'GL.queries[id]=null' else 1
        if source.count(old) != expected:
            raise ValueError('Missing or duplicated reviewed WebGL handle operation')
        source = source.replace(old, new)
    return source


START = 'createOffscreenFramebuffer:context=>{'
END = 'resizeOffscreenFramebuffer:context=>{'
FRAMEBUFFER_SHA256 = '35d886ede339b1f3aa51d8516e3e45dd6c41e701b3fe05968f9c279c4ddf8f16'
ORIGINAL = 'context.defaultFboForbidBlitFramebuffer=false'
WEBKIT_TEST = '/AppleWebKit/.test(navigator.userAgent)&&!/Chrome|Chromium|Edg\\//.test(navigator.userAgent)'
REPLACEMENT = 'context.defaultFboForbidBlitFramebuffer=' + WEBKIT_TEST


def select_shader_presentation(source):
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError('Unreviewed Emscripten framebuffer initializer')
    start, end = source.index(START), source.index(END)
    initializer = source[start:end]
    if hashlib.sha256(initializer.encode()).hexdigest() != FRAMEBUFFER_SHA256:
        raise ValueError('Emscripten framebuffer initializer changed; review the template')
    if initializer.count(ORIGINAL) != 1:
        raise ValueError('Missing reviewed framebuffer presentation switch')
    return source[:start] + initializer.replace(ORIGINAL, REPLACEMENT) + source[end:]



def version_extension_fetch(source, release_root):
    if not isinstance(release_root, str) or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]{0,79}', release_root):
        raise ValueError('release root must be a literal JavaScript identifier')
    if "'dynamicLibraries':" not in source:
        return source
    anchor = "'locateFile': function (path) {"
    if source.count(anchor) != 1 or source.count('const gdext = this.gdextensionLibs;') != 1:
        raise ValueError('Unreviewed Godot extension loader')
    # Emscripten must cache the bare library name for Godot's later dlopen.
    # locateFile changes only its fetch URL, after the release shell supplies
    # the content-addressed root. Raw debug exports have no release root.
    return source.replace(anchor, anchor + f"\n\t\t\t\tif (gdext.includes(path) && globalThis.{release_root}) return globalThis.{release_root} + path;")


def prepare(script, release_root=None):
    path = Path(script)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Web runtime must be a regular native export')
    source = release_gl_table_slots(select_shader_presentation(path.read_text()))
    if release_root is not None:
        source = version_extension_fetch(source, release_root)
    path.write_text(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('script', type=Path)
    parser.add_argument('--release-root')
    args = parser.parse_args()
    try:
        prepare(args.script, args.release_root)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
