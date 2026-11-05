"""Preserve source-bound rollback, manifest and authored-import failure controls."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[1] / 'helpers'
sys.path.insert(0, str(HELPERS))
with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'fixture/game'}):
    import godot_release_record as record
    import godot_rollback_config as rollback
import godot_authoring as authoring
import image_security_report as security


class ReleaseAuthorityControls(unittest.TestCase):
    def test_only_exact_successful_main_cd_can_authorize_immutable_rollback(self):
        run = {'repository': {'full_name': 'fixture/game'}, 'head_repository': {'full_name': 'fixture/game'},
            'path': '.github/workflows/cd.yml', 'head_branch': 'main', 'event': 'push', 'status': 'completed',
            'conclusion': 'success', 'head_sha': 'a'*40, 'id': 123}
        value = {'repository': 'fixture/game', 'sha': 'a'*40, 'run_id': '123', 'release_id': 'b'*64,
            'gate_ref': 'ghcr.io/fixture/game-gate@sha256:'+'c'*64, 'config_ref': 'ghcr.io/fixture/game-config@sha256:'+'d'*64}
        self.assertEqual(record.validate_record(value, run), value)
        for key, changed in [('path','.github/workflows/ci.yml'),('event','pull_request'),('head_branch','feature'),('conclusion','failure'),('head_sha','main')]:
            with self.assertRaises(ValueError):record.validate_record(value,{**run,key:changed})
        for key, changed in [('sha','0'*40),('run_id','124'),('gate_ref','ghcr.io/fixture/game-gate:main'),('config_ref','ghcr.io/foreign/game-config@sha256:'+'d'*64),('release_id','')]:
            with self.assertRaises(ValueError):record.validate_record({**value,key:changed},run)

    def test_historical_gate_constraints_reject_weakened_or_additional_services(self):
        ref='ghcr.io/fixture/game-gate@sha256:'+'a'*64
        gate={'image':ref,'user':'1000:1000','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true']}
        rollback.validate_stack({'services':{'game-gate':gate}},ref)
        for key,value in [('user','0'),('read_only',False),('volumes',['/:/host']),('ports',['80:80']),('build','.')]:
            with self.assertRaises(ValueError):rollback.validate_stack({'services':{'game-gate':{**gate,key:value}}},ref)
        with self.assertRaises(ValueError):rollback.validate_stack({'services':{'game-gate':gate,'extra':{}}},ref)

    def test_only_exact_equivalent_import_defaults_are_restored(self):
        before=b'[remap]\nuid="original"\n[params]\ncompress/mode=0\n'
        after=before+b'compress/uastc_level=0\nprocess/channel_remap/red=0\n'
        self.assertTrue(authoring.default_insertions_only(before,after))
        for changed in [after.replace(b'red=0',b'red=1'),after+b'unknown=true\n',after.replace(b'mode=0',b'mode=1'),before.replace(b'original',b'changed')]:
            self.assertFalse(authoring.default_insertions_only(before,changed))
        with tempfile.TemporaryDirectory(prefix='owned imports ') as directory:
            path=Path(directory)/'texture.png.import';path.write_bytes(before)
            self.assertEqual(authoring.preserve_imports(directory,lambda:path.write_bytes(after)),len(after))
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaises(ValueError):authoring.preserve_imports(directory,lambda:path.write_bytes(before+b'unknown=true\n'))

    def test_security_policy_blocks_all_secrets_and_high_critical_with_full_report(self):
        report={'SchemaVersion':2,'Results':[{'Vulnerabilities':[{'Severity':'LOW'},{'Severity':'HIGH'},{'Severity':'CRITICAL'}],'Secrets':[{'Title':'fixture'}]}]}
        self.assertEqual(len(security.blocking_findings(report)),3)
        self.assertEqual(len(report['Results'][0]['Vulnerabilities']),3)
        with self.assertRaises(ValueError):security.blocking_findings({'SchemaVersion':1,'Results':[]})
        with self.assertRaises(ValueError):security.blocking_findings({'SchemaVersion':2})


if __name__=='__main__':unittest.main()
