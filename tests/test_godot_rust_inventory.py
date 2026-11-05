"""Workspace members select their actual owning dependency inventories explicitly."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cookbook'))
import images
import godot_game
class GodotRustInventory(unittest.TestCase):
    def test_workspace_and_standalone_owning_locks_are_used_without_member_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['Cargo.toml','Cargo.lock','api/Cargo.toml','internal/tool/Cargo.toml','internal/tool/Cargo.lock']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('version=4\n')
            config={'components':{'rust':[{'manifest':'Cargo.toml'},{'manifest':'api/Cargo.toml'},{'manifest':'internal/tool/Cargo.toml'}]},
                'security':{'rust_inventory_manifests':['Cargo.toml','internal/tool/Cargo.toml'],'rust_inventory_sbom':True}}
            self.assertEqual(images.rust_inventory_manifests(root,config),['Cargo.toml','internal/tool/Cargo.toml'])
            self.assertEqual(images.rust_inventory_manifests(root,{}),[])
            commands=[]
            with patch.object(godot_game,'run',side_effect=lambda argv,*args,**kwargs:commands.append(argv)) as native_run,patch.object(godot_game.images,'rust_inventory') as inventory:
                godot_game.security(root,config,{})
            inventory.assert_called_once_with(root,['Cargo.toml','internal/tool/Cargo.toml'],{},native_run,sbom=True)
            audits=[argv for argv in commands if argv[:2]==['cargo','audit']]
            self.assertEqual([argv[3] for argv in audits],['Cargo.lock','internal/tool/Cargo.lock'])
            self.assertFalse((root/'api/Cargo.lock').exists())
            for values in [[],['Cargo.toml','Cargo.toml'],['api/Cargo.toml'],['../Cargo.toml']]:
                with self.subTest(values=values),self.assertRaises(ValueError):
                    images.rust_inventory_manifests(root,dict(config,security={'rust_inventory_manifests':values}))
