import copy
import datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'helpers'))
import filesystem_scan_context as filesystem
from image_security_report import filesystem_scan_findings


class StaticFilesystemEvidence(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.archive=self.root/'static.tar.gz'
        self.write([('index.html',b'<html>accepted immutable static bytes</html>')])
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.context={'source':'a'*40,'release_digest':'sha256:'+'b'*64,'artifact':self.artifact,'target':'/inputs/static',
            'scanner':{'reference':'docker.io/aquasec/trivy@sha256:'+'c'*64,'id':'sha256:'+'d'*64,'version':'0.70.0'},
            'started_at':now,'finished_at':now,'before':self.inventory,'after':self.inventory}
        self.report={'SchemaVersion':2,'Trivy':{'Version':'0.70.0'},'ReportID':str(uuid.uuid4()),
            'CreatedAt':now,'ArtifactType':'filesystem','ArtifactName':'/inputs/static'}
        self.sbom={'bomFormat':'CycloneDX','specVersion':'1.6','version':1,'components':[],'vulnerabilities':[],
            'dependencies':[{'ref':'root-filesystem','dependsOn':[]}],
            'metadata':{'timestamp':now,'tools':{'components':[{'group':'aquasecurity','name':'trivy','version':'0.70.0'}]},
                'component':{'type':'application','name':'/inputs/static','bom-ref':'root-filesystem',
                    'properties':[{'name':'aquasecurity:trivy:SchemaVersion','value':'2'}]}}}

    def write(self,entries):
        raw=io.BytesIO();self.inventory={}
        with tarfile.open(fileobj=raw,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name,body in entries:
                member=tarfile.TarInfo(name);member.mode=0o755;member.size=len(body);archive.addfile(member,io.BytesIO(body))
                self.inventory[name]={'sha256':hashlib.sha256(body).hexdigest(),'size':len(body),'mode':0o644}
        self.archive.write_bytes(gzip.compress(raw.getvalue(),mtime=0));self.bind()

    def bind(self):
        self.artifact={'kind':'archive','digest':'sha256:'+hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            'size_bytes':self.archive.stat().st_size,'media_type':filesystem.registry.STATIC_LAYER}
        manifest=filesystem.registry.artifact_manifest(self.artifact['digest'],self.artifact['size_bytes'])
        self.artifact['reference']='ghcr.io/example/static@'+filesystem.registry.digest(manifest)

    def admit(self):return filesystem_scan_findings(self.report,self.sbom,self.context,self.archive)

    def test_actual_empty_results_admit_only_complete_exact_static_evidence_without_rewriting_reports(self):
        original=copy.deepcopy((self.report,self.sbom,self.context))
        self.assertEqual(self.admit(),[]);self.assertEqual((self.report,self.sbom,self.context),original)
        self.assertNotIn('Results',self.report)
        self.report['Results']=[];self.assertEqual(self.admit(),[])
        self.report['Metadata']={};self.assertEqual(self.admit(),[])

    def test_all_secrets_and_high_critical_vulnerabilities_block_with_original_records(self):
        findings=[{'Severity':'HIGH','VulnerabilityID':'high'},{'Severity':'CRITICAL','VulnerabilityID':'critical'}]
        secret={'Severity':'LOW','RuleID':'synthetic'}
        self.report['Results']=[{'Target':'index.html','Class':'secret','Secrets':[secret]},
            {'Target':'package-lock.json','Class':'lang-pkgs','Vulnerabilities':[{'Severity':'LOW'},*findings]}]
        self.assertEqual(self.admit(),[secret,*findings])

    def test_source_release_scanner_target_and_time_context_cannot_be_substituted(self):
        mutations=[lambda c:c.update(source='latest'),lambda c:c.update(release_digest='invalid'),
            lambda c:c.update(target='/different'),lambda c:c['scanner'].update(version='0.69.0'),
            lambda c:c.update(finished_at='2020-01-01T00:00:00Z')]
        original=copy.deepcopy(self.context)
        for mutate in mutations:
            self.context=copy.deepcopy(original);mutate(self.context)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):self.admit()

    def test_report_and_sbom_need_actual_filesystem_identity_scanner_complete_inventory_and_freshness(self):
        mutations=[lambda r,s:r.update(ArtifactType='container_image'),lambda r,s:r.update(ArtifactID='sha256:'+'a'*64),
            lambda r,s:r.update(Metadata={'ImageID':'sha256:'+'a'*64}),lambda r,s:r.update(ArtifactName='/different'),
            lambda r,s:r.update(ReportID=''),lambda r,s:r.update(CreatedAt='2020-01-01T00:00:00Z'),
            lambda r,s:s.pop('components'),lambda r,s:s.update(dependencies=[]),
            lambda r,s:s['metadata']['component'].update(type='container'),
            lambda r,s:s['metadata']['component']['properties'].append({'name':'aquasecurity:trivy:ImageID','value':'fabricated'})]
        original=copy.deepcopy((self.report,self.sbom))
        for mutate in mutations:
            self.report,self.sbom=copy.deepcopy(original);mutate(self.report,self.sbom)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):self.admit()

    def test_actual_archive_and_each_scanned_file_byte_membership_mode_and_type_are_bound(self):
        original=copy.deepcopy(self.context)
        for change in [{'index.html':{'sha256':'0'*64,'size':1,'mode':420}}, {},
                       {**self.inventory,'extra':{'sha256':'0'*64,'size':0,'mode':420}},
                       {'index.html':{**self.inventory['index.html'],'mode':493}}]:
            self.context=copy.deepcopy(original);self.context['after']=change
            with self.subTest(change=change),self.assertRaises(ValueError):self.admit()
        self.context=original;self.archive.write_bytes(self.archive.read_bytes()+b'changed')
        with self.assertRaises(ValueError):self.admit()

    def test_duplicate_traversal_links_compressed_trailers_and_bounds_are_refused(self):
        for entries in [[('../escape',b'x')],[('/absolute',b'x')],[('index.html',b'x'),('index.html',b'y')]]:
            self.write(entries)
            with self.subTest(entries=entries),self.assertRaises(ValueError):filesystem.archive_inventory(self.archive,self.artifact)
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as archive:
            entry=tarfile.TarInfo('linked');entry.type=tarfile.SYMTYPE;entry.linkname='/outside';archive.addfile(entry)
        self.archive.write_bytes(gzip.compress(raw.getvalue()));self.bind()
        with self.assertRaises(ValueError):filesystem.archive_inventory(self.archive,self.artifact)
        self.write([('index.html',b'accepted')]);self.archive.write_bytes(self.archive.read_bytes()+gzip.compress(b'hidden'));self.bind()
        with self.assertRaisesRegex(ValueError,'trailing compressed'):filesystem.archive_inventory(self.archive,self.artifact)
        self.write([('index.html',b'accepted')])
        with patch.object(filesystem,'MAX_BYTES',3),self.assertRaises(ValueError):filesystem.archive_inventory(self.archive,self.artifact)

    def test_malformed_results_never_claim_a_complete_filesystem_scan(self):
        for result in [None,{}, {'Target':'index.html','Class':'unknown'},
                       {'Target':'index.html','Class':'secret','Secrets':None},
                       {'Target':'index.html','Class':'lang-pkgs','Vulnerabilities':[{}]}]:
            self.report['Results']=[result]
            with self.subTest(result=result),self.assertRaises(ValueError):self.admit()
