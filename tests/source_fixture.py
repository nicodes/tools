"""Use the production source codec for authored test declarations."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('_fixture_source_config', Path(__file__).resolve().parents[1] / 'helpers/source_config.py')
source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source)
write_source = source.write
read_source = source.read
dump_source = source.dumps

read = source.read
dumps = source.dumps
write = source.write
equal = source.equal
