"""Metadata-only native proof for recorded-external versus explicit Rust homes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_sdk_metadata', ROOT / 'helpers/pinned-tools.py')
tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tools)


def identity(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]


def main():
    original = dict(os.environ)
    selected = tools.activation(ROOT, [('rust', 'cargo'), ('rust', 'rustc'), ('rust', 'rustup')], original)
    manager = Path(shutil.which('rustup', path=selected['PATH']))
    home = Path(selected['RUSTUP_HOME']).resolve()
    native = {}
    for name in ['cargo', 'rustc']:
        path = Path(tools.rustup_query([str(manager), 'which', '--toolchain', '1.94.0', name], ROOT, selected).strip())
        native[path] = identity(path)
    with tempfile.TemporaryDirectory(prefix='recorded Rust runtime ') as directory:
        root = Path(directory).resolve()
        (root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\npython="0.0.0"\n')
        external = root / 'external-proxies'
        external.mkdir()
        suffix = '.exe' if os.name == 'nt' else ''
        for name in ['rustc', 'cargo', 'rustup']:
            (external / (name + suffix)).symlink_to(manager.resolve())
        data = root / 'data'
        tracking = data / 'installs/rust/1.94.0'
        tracking.parent.mkdir(parents=True)
        tracking.symlink_to(external, target_is_directory=True)
        env = dict(selected, MISE_CONFIG_DIR=str(root / 'config'), MISE_DATA_DIR=str(data),
            MISE_CACHE_DIR=str(root / 'cache'), MISE_STATE_DIR=str(root / 'state'),
            MISE_GLOBAL_CONFIG_FILE=str(root / 'global.toml'), MISE_SYSTEM_CONFIG_FILE=str(root / 'system.toml'),
            MISE_CEILING_PATHS=str(root.parent), MISE_TRUSTED_CONFIG_PATHS=str(root / 'mise.toml'),
            MISE_OFFLINE='true', RUSTUP_AUTO_INSTALL='0', CARGO_NET_OFFLINE='true')
        first = tools.activation(root, [('rust', 'cargo'), ('rust', 'rustc'), ('rust', 'rustup')], env)
        nested = tools.activation(root, [('rust', 'cargo')], first)
        runtime = Path(shutil.which('cargo', path=nested['PATH'])).parent.resolve()
        assert runtime != external, 'control must exercise the unchanged external recording'
        assert tracking.resolve() == external
        assert Path(nested['RUSTUP_HOME']).resolve() == home
        assert not (data / 'installs/python').exists(), 'unrequested configured SDK must stay absent'
        missing_home = root / 'missing-cargo-home'
        try:
            tools.activation(root, [('rust', 'cargo')], dict(env, CARGO_HOME=str(missing_home)))
        except ValueError:
            pass
        else:
            raise AssertionError('unprepared explicit runtime must not fall back to recorded external proxies')
        assert not missing_home.exists(), 'activation must not create an unprepared Rust context'
        (root / 'mise.toml').write_text('[tools]\nrust="0.0.0"\npython="0.0.0"\n')
        try:
            tools.activation(root, [('rust', 'cargo')], env)
        except ValueError:
            pass
        else:
            raise AssertionError('missing selected version must refuse')
        assert not (data / 'installs/rust/0.0.0').exists()
        assert original.get('HOME') == os.environ.get('HOME')
        for path, before in native.items():
            assert identity(path) == before, 'native SDK bytes and metadata must remain unchanged'
        print(json.dumps({'recorded_proxy': str(external), 'runtime_proxy': str(runtime),
            'nested_activation': 'passed', 'unrequested_SDK_absent': True,
            'missing_selected_refused': True, 'missing_managed_proxy_refused': True,
            'Rust_SDK_unchanged': True, 'HOME_unchanged': True,
            'product_compile_or_SDK_install': False}), flush=True)


if __name__ == '__main__':
    main()
