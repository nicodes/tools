"""Custom template setup never leaks into tests or imported product validation."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cookbook'))
import godot_game as game


class TemplateHookControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        app=self.root/'app';app.mkdir();(app/'project.godot').write_text('config_version=5\n')
        self.config={'components':{'godot':{'project':'app','exports':[{'preset':'Web','output':'app/build/web/index.html'}],
            'templates':{'profile':'app/engine/profile.json','directory':'app/.export-templates'}}},'artifacts':{'paths':['app/build/web']}}

    def test_bad_or_absent_custom_sdk_stops_the_real_export_without_network_repair(self):
        template=SimpleNamespace(verify=MagicMock(side_effect=ValueError('offline SDK mismatch')),setup=MagicMock())
        exporter=MagicMock()
        with patch.dict(sys.modules,{'godot_template':template}),patch.object(game.godot_sdk,'activate',return_value={}),\
             patch.object(game.godot_package,'author'),patch.object(game.godot_package,'extensions'),\
             patch.object(game,'helper',return_value=exporter):
            with self.assertRaisesRegex(ValueError,'offline SDK mismatch'):
                game.compile_outputs(self.root,self.config,{})
        template.verify.assert_called_once();template.setup.assert_not_called();exporter.export.assert_not_called()

    def test_imported_host_product_validation_does_not_require_custom_sdk_cache(self):
        output=self.root/'app/build/web';output.mkdir(parents=True)
        for suffix in ['.html','.wasm','.pck','.js']:(output/('index'+suffix)).write_bytes(b'accepted product fixture')
        verification=SimpleNamespace(scope=lambda **values:values,artifacts=lambda scope:{'fixture':'accepted'})
        template=SimpleNamespace(verify=MagicMock(side_effect=AssertionError('must not inspect export SDK')),setup=MagicMock())
        with patch.dict(sys.modules,{'godot_template':template}),patch.object(game,'helper',return_value=verification):
            game.validate_outputs(self.root,self.config,{},compiler=False)
        template.verify.assert_not_called();template.setup.assert_not_called()


if __name__=='__main__':unittest.main()
