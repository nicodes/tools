"""Prospective source, removed files and referenced identities use real Git inventory."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('native_resource_uids',Path(__file__).resolve().parents[1]/'helpers/godot-resource-uids.py')
uids=importlib.util.module_from_spec(spec);spec.loader.exec_module(uids)
class ResourceUids(unittest.TestCase):
    def test_untracked_duplicate_fails_and_removed_or_reference_only_uid_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'first.uid').write_text('uid://owned\n');subprocess.run(['git','-C',str(root),'add','.'],check=True)
            (root/'scene.tscn').write_text('[gd_scene format=3 uid="uid://scene"]\n[ext_resource type="Script" uid="uid://owned" path="res://first.gd"]\n')
            self.assertFalse(uids.check(root))
            duplicate=root/'new.tres';duplicate.write_text('[gd_resource type="Resource" format=3 uid="uid://owned"]\n')
            self.assertTrue(uids.check(root))
            (root/'first.uid').unlink()
            self.assertFalse(uids.check(root))
    def test_import_uid_and_script_uid_share_one_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'script.uid').write_text('uid://collision\n')
            (root/'asset.png.import').write_text('[remap]\nuid="uid://collision"\n')
            self.assertTrue(uids.check(root))
