#!/usr/bin/env python3
"""Keep authored import bytes while Godot writes equivalent new default keys."""
import difflib
from pathlib import Path
import stat
import subprocess
import sys

DEFAULT_INSERTIONS = {
    'compress/uastc_level=0', 'compress/rdo_quality_loss=0.0',
    'process/channel_remap/red=0', 'process/channel_remap/green=1',
    'process/channel_remap/blue=2', 'process/channel_remap/alpha=3',
    'modulate_color_glyphs=false',
}
MAX_FILESYSTEM_CACHE_BYTES = 16 << 20


def editor_filesystem_cache(project):
    """Admit only the generated cache whose import hashes would become stale."""
    project = project.absolute()
    parents = []
    for directory in [*reversed(project.parents), project, project / '.godot', project / '.godot/editor']:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return None
        reparse = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        if not stat.S_ISDIR(info.st_mode) or reparse:
            raise ValueError('generated editor cache requires unlinked project directories')
        parents.append((info.st_dev, info.st_ino))
    path = project / '.godot/editor/filesystem_cache10'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    reparse = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    if not stat.S_ISREG(info.st_mode) or reparse or info.st_nlink != 1 or info.st_size > MAX_FILESYSTEM_CACHE_BYTES:
        raise ValueError('generated editor cache must be a bounded unlinked regular file')
    identity = (*parents, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return path, identity


def default_insertions_only(before, after):
    old=before.decode().splitlines(keepends=True);new=after.decode().splitlines(keepends=True)
    for tag, _, _, start, end in difflib.SequenceMatcher(a=old,b=new,autojunk=False).get_opcodes():
        if tag=='equal':continue
        if tag!='insert':return False
        section=next((line.strip() for line in reversed(new[:start]) if line.startswith('[')),None)
        if section!='[params]' or any(line.rstrip('\n') not in DEFAULT_INSERTIONS for line in new[start:end]):return False
    return True


def preserve_imports(project, execute):
    project=Path(project)
    originals={}
    for path in project.rglob('*.import'):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file() or path.stat().st_size>1<<20:
            raise ValueError('authored import must be a bounded regular file')
        originals[path]=path.read_bytes()
    result=execute()
    changed=[]
    for path,before in originals.items():
        if path.is_symlink() or not path.is_file():raise ValueError('editor replaced an authored import')
        after=path.read_bytes()
        if after!=before:
            if not default_insertions_only(before,after):raise ValueError('editor changed authored import settings: '+str(path))
            changed.append(path)
    cache = editor_filesystem_cache(project) if changed else None
    for path in changed:path.write_bytes(originals[path])
    if cache is not None:
        if editor_filesystem_cache(project) != cache:
            raise ValueError('generated editor cache changed during import restoration')
        cache[0].unlink()
    return result


def main(command):
    if not command:raise ValueError('a Godot editor command is required')
    return preserve_imports(Path('app'),lambda: subprocess.run(command).returncode)


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
