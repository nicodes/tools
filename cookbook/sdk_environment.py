"""Preserve SDK installation directories before isolating application data homes."""
import os
from pathlib import Path, PureWindowsPath
import platform


def preserve_mise(environment=None, *, system=None):
    values = dict(os.environ if environment is None else environment)
    if 'MISE_DATA_DIR' in values:
        return values
    windows = (system or platform.system()) == 'Windows'
    path = PureWindowsPath if windows else Path
    if values.get('XDG_DATA_HOME'):
        directory = path(values['XDG_DATA_HOME']) / 'mise'
    elif windows:
        directory = (path(values['LOCALAPPDATA']) if values.get('LOCALAPPDATA') else
                     path(values.get('USERPROFILE') or values.get('HOME') or str(Path.home())) / 'AppData/Local') / 'mise'
    else:
        directory = path(values.get('HOME') or str(Path.home())) / '.local/share/mise'
    values['MISE_DATA_DIR'] = str(directory)
    return values
