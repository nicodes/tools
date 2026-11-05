"""Retain scanner evidence and prevent missing SDK or findings from passing."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cookbook'))
import godot_security as security


class ImageSecurityControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="aquasec/trivy@sha256:'+'a'*64+'"\n')
        self.config={'components':{'image_security':{'reports':'.artifacts/reports/security'}}}
        self.definition={'image_base':'ghcr.io/fixture/game','inputs':{'images':['ghcr.io/fixture/game-gate:{revision}']}}
        self.commands=[];self.report={'SchemaVersion':2,'Results':[{}]}

    def execute(self, argv, root, env, **kwargs):
        self.commands.append((argv,kwargs))
        if '--output' in argv:
            filename=Path(argv[argv.index('--output')+1]).name
            output=self.root/'.artifacts/reports/security/gate'/filename
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(self.report if filename=='security.json' else {'bomFormat':'CycloneDX'}))

    def test_full_security_and_sbom_evidence_is_retained_without_product_build(self):
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
            self.assertEqual(len(security.evidence_paths(self.root,self.config)),2)
        self.assertEqual(self.commands[0][0][:3],['docker','image','inspect'])
        self.assertEqual(len(self.commands),3)
        self.assertIn('vuln,secret',self.commands[1][0])
        self.assertIn('cyclonedx',self.commands[2][0])
        self.assertFalse(any('build' in argv or 'pull' in argv for argv,_ in self.commands))

    def test_any_secret_high_or_critical_blocks_after_retaining_complete_report(self):
        for results in [{'Secrets':[{'Severity':'LOW'}]}, {'Vulnerabilities':[{'Severity':'HIGH'}]}, {'Vulnerabilities':[{'Severity':'CRITICAL'}]}]:
            self.report={'SchemaVersion':2,'Results':[results]}
            with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(ValueError):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
            path=self.root/'.artifacts/reports/security/gate/security.json'
            self.assertEqual(json.loads(path.read_text()),self.report)

    def test_setup_pulls_only_pinned_scanner_and_tests_refuse_missing_sdk(self):
        security.setup(self.root,self.config,{},self.execute)
        self.assertEqual(len(self.commands),1)
        self.assertEqual(self.commands[0][0][:2],['docker','pull'])
        self.assertEqual(self.commands[0][1],{'timeout':300})
        def missing(argv,*args,**kwargs):
            self.assertEqual(argv[:3],['docker','image','inspect'])
            raise subprocess.CalledProcessError(1,argv)
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(subprocess.CalledProcessError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},missing)

    def test_historical_ref_is_scanned_without_retagging_or_current_source_proof(self):
        reference='ghcr.io/fixture/game-gate@sha256:'+'c'*64
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute,[('gate',reference)])
        self.assertEqual([argv[-1] for argv,_ in self.commands[1:]],[reference,reference])
        self.assertFalse(any(any(value in argv for value in ['tag','build','push','pull']) for argv,_ in self.commands))
        self.assertFalse((self.root/'.artifacts/verification').exists())

    def test_historical_scan_inventory_cannot_escape_declared_image_family(self):
        invalid=[[],[('gate','ghcr.io/other/game-gate@sha256:'+'c'*64)],
            [('gate','ghcr.io/fixture/game-gate:historical')],[('config','ghcr.io/fixture/game-config@sha256:'+'c'*64)],
            [('gate','ghcr.io/fixture/game-gate@sha256:'+'C'*64)],
            [('gate','ghcr.io/fixture/game-gate@sha256:'+'c'*64)]*2]
        with patch.object(security.images,'definition',return_value=self.definition):
            for records in invalid:
                with self.subTest(records=records),self.assertRaises(ValueError):
                    security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute,records)
        self.assertEqual(self.commands,[])

    def test_report_and_sdk_pins_cannot_escape_native_authority(self):
        self.config['components']['image_security']['reports']='app'
        with self.assertRaises(ValueError):security.declaration(self.root,self.config)
        self.config['components']['image_security']['reports']='.artifacts/reports/security'
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="aquasec/trivy:latest"\n')
        with self.assertRaises(ValueError):security.declaration(self.root,self.config)


if __name__=='__main__':unittest.main()
