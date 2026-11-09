"""An empty image scan must prove its exact completed data-only target."""
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helpers'))
from image_security_report import blocking_findings, image_scan_findings
from image_scan_context import data_image
import image_scan_fixture as fixture


class ImageScanContextControls(unittest.TestCase):
    def test_foreign_cached_archive_core_cannot_replace_this_Tools_source(self):
        foreign = types.ModuleType('_tools_oci_archive')
        foreign.__file__ = '/foreign/oci_archive.py'
        with patch.dict(sys.modules, {'_tools_oci_archive': foreign}):
            with self.assertRaisesRegex(ValueError, 'core origin differs'):
                self.admit()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.path = self.root / 'image.tar'
        self.inspected, self.config, self.size = fixture.archive(self.path)
        self.report, self.sbom, self.context = fixture.evidence(self.inspected, self.config, self.size)

    def admit(self, **changes):
        return image_scan_findings(changes.get('report', self.report), changes.get('sbom', self.sbom),
            context=changes.get('context', self.context), input_archive=changes.get('input_archive'), data_archive=changes.get('archive', self.path))

    def test_default_parser_stays_strict_and_complete_data_image_is_admitted(self):
        before = copy.deepcopy(self.report)
        with self.assertRaises(ValueError):
            blocking_findings(self.report)
        self.assertEqual(self.admit(), [])
        self.assertEqual(self.report, before)
        self.assertNotIn('Results', self.report)
        self.report['Results'] = []
        self.assertEqual(self.admit(), [])
        with self.assertRaises(ValueError):
            self.admit(archive=None)

    def test_oci_input_target_hash_and_full_identity_are_bound_without_report_rewrite(self):
        report,sbom,context=copy.deepcopy((self.report,self.sbom,self.context))
        context['archive']={'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest(),'target':'/inputs/image.oci'}
        report['ArtifactName']='/inputs/image.oci';report['Metadata'].pop('Reference')
        sbom['metadata']['component']['name']='/inputs/image.oci'
        sbom['metadata']['component']['properties']=[row for row in sbom['metadata']['component']['properties'] if not row['name'].endswith(':Reference')]
        original=copy.deepcopy((report,sbom,context))
        with self.assertRaisesRegex(ValueError,'authenticated input archive'):
            self.admit(report=report,sbom=sbom,context=context)
        self.assertEqual(self.admit(report=report,sbom=sbom,context=context,input_archive=self.path),[])
        self.assertEqual((report,sbom,context),original)
        report['Results']=[{'Target':'fixture','Class':'os-pkgs','Vulnerabilities':[]}]
        self.assertEqual(self.admit(report=report,sbom=sbom,context=context,input_archive=self.path,archive=None),[])
        context['archive']['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'input differs'):
            self.admit(report=report,sbom=sbom,context=context,input_archive=self.path,archive=None)

    def test_original_nonempty_high_critical_and_any_secret_behavior_is_unchanged(self):
        row = {'Target': 'fixture', 'Class': 'os-pkgs', 'Vulnerabilities': [{'Severity': value} for value in ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']],
               'Secrets': [{'Severity': 'LOW', 'RuleID': 'synthetic'}]}
        self.report['Results'] = [row]
        self.assertEqual(self.admit(archive=None), blocking_findings(self.report))
        self.assertEqual(len(self.admit()), 3)

    def test_malformed_nonempty_targets_or_finding_records_cannot_claim_a_complete_scan(self):
        rows = [{}, None, {'Target': 'fixture'}, {'Target': '', 'Class': 'os-pkgs'},
                {'Target': 'fixture', 'Class': 'unknown'},
                {'Target': 'fixture', 'Class': 'os-pkgs', 'Vulnerabilities': None},
                {'Target': 'fixture', 'Class': 'secret', 'Secrets': [{} , None]},
                {'Target': 'fixture', 'Class': 'os-pkgs', 'Vulnerabilities': [{}]},
                {'Target': 'fixture', 'Class': 'os-pkgs', 'Vulnerabilities': [{'Severity': 'invalid'}]}]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.admit(report={**self.report, 'Results': [row]}, archive=None)

    def test_ordinary_unlabeled_binary_reports_keep_their_exact_source_tag_binding(self):
        self.report['Results'] = [{'Target': 'fixture', 'Class': 'os-pkgs', 'Vulnerabilities': []}]
        for value in [self.context['before'], self.context['after']]: value['Config'].pop('Labels')
        self.report['Metadata']['ImageConfig']['config'].pop('Labels')
        properties = self.sbom['metadata']['component']['properties']
        self.sbom['metadata']['component']['properties'] = [item for item in properties if not item['name'].endswith('Labels:org.opencontainers.image.revision')]
        self.assertEqual(self.admit(archive=None), [])
        for value in [self.context['before'], self.context['after']]:
            value['Config']['Labels'] = {'org.opencontainers.image.revision': 'vendor-revision'}
        self.report['Metadata']['ImageConfig']['config']['Labels'] = {'org.opencontainers.image.revision': 'vendor-revision'}
        self.sbom['metadata']['component']['properties'].append(
            {'name': 'aquasecurity:trivy:Labels:org.opencontainers.image.revision', 'value': 'vendor-revision'})
        self.assertEqual(self.admit(archive=None), [])
        self.report.pop('Results')
        with self.assertRaisesRegex(ValueError, 'explicit matching source revision label'): self.admit()

    def test_null_or_malformed_results_are_never_empty_admission(self):
        for value in [None, {}, '', 0, False]:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.admit(report={**self.report, 'Results': value})

    def test_exact_schema_scanner_reference_source_image_and_layers_are_required(self):
        changes = [(['SchemaVersion'], 3), (['ArtifactType'], 'filesystem'),
            (['ArtifactName'], 'example.invalid/other:' + fixture.SOURCE),
            (['Trivy', 'Version'], '0.74.0'), (['Metadata', 'ImageID'], 'sha256:' + 'f' * 64),
            (['Metadata', 'Reference'], 'example.invalid/other:' + fixture.SOURCE),
            (['Metadata', 'DiffIDs'], ['sha256:' + 'e' * 64]),
            (['Metadata', 'Layers'], []), (['Metadata', 'ImageConfig', 'config', 'Labels'], {}),
            (['Metadata', 'ImageConfig', 'rootfs', 'diff_ids'], [])]
        for path, value in changes:
            report = copy.deepcopy(self.report); target = report
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError): self.admit(report=report)
        for path, value in [(['source'], 'f' * 40), (['scanner', 'reference'], 'aquasec/trivy:latest'),
                            (['after', 'Id'], 'sha256:' + 'f' * 64), (['before', 'Config', 'Labels'], {})]:
            context = copy.deepcopy(self.context); target = context
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError): self.admit(context=context)

    def test_paired_sbom_identity_scanner_layers_and_closed_empty_inventory_are_required(self):
        for field, value in [('bomFormat', 'SPDX'), ('specVersion', '1.5'), ('components', [{}]),
                             ('vulnerabilities', [{}]), ('dependencies', []), ('components', None)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.admit(sbom={**self.sbom, field: value})
        for key in ['ImageID', 'DiffID', 'Reference', 'SchemaVersion', 'Labels:org.opencontainers.image.revision']:
            sbom = copy.deepcopy(self.sbom)
            for item in sbom['metadata']['component']['properties']:
                if item['name'] == 'aquasecurity:trivy:' + key: item['value'] = 'wrong'
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit(sbom=sbom)
        sbom = copy.deepcopy(self.sbom)
        sbom['metadata']['component']['properties'].append(copy.deepcopy(next(item for item in sbom['metadata']['component']['properties'] if item['name'].endswith(':ImageID'))))
        with self.assertRaises(ValueError): self.admit(sbom=sbom)

    def test_stale_future_or_unbounded_completion_evidence_refuses(self):
        for delta in [-60, 60]:
            report = copy.deepcopy(self.report)
            report['CreatedAt'] = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta)).isoformat()
            with self.assertRaises(ValueError): self.admit(report=report)
        context = copy.deepcopy(self.context)
        context['started_at'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(ValueError): self.admit(context=context)

    def test_archive_missing_wrong_tag_source_config_or_layer_bytes_refuse(self):
        for options in [{'reference': 'example.invalid/other:' + fixture.SOURCE}, {'source': 'c' * 40},
                        {'body': b'value: changed\n'}, {'extra_outer': True}]:
            fixture.archive(self.path, **options)
            with self.subTest(options=options), self.assertRaises(ValueError): self.admit()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError): self.admit()

    def test_unreachable_metadata_is_refused_even_if_content_addressed(self):
        with tarfile.open(self.path) as packet:
            rows = [(item.name, packet.extractfile(item).read()) for item in packet if item.isfile()]
        original = {'architecture': 'amd64', 'os': 'linux', 'created': self.config['created'],
            'id': 'd' * 64, 'config': copy.deepcopy(self.config['config']), 'container_config': {}}
        for mutation in [None, 'source', 'architecture', 'container-command', 'unknown', 'wrong-digest']:
            value = copy.deepcopy(original)
            if mutation == 'source': value['config']['Labels'] = {'org.opencontainers.image.revision': 'e' * 40}
            if mutation == 'architecture': value['architecture'] = 'arm64'
            if mutation == 'container-command': value['container_config']['Cmd'] = ['/runner']
            if mutation == 'unknown': value['unknown'] = True
            body = json.dumps(value, sort_keys=True).encode()
            name = 'blobs/sha256/' + (('0' * 64) if mutation == 'wrong-digest' else hashlib.sha256(body).hexdigest())
            with tarfile.open(self.path, 'w') as packet:
                for path, data in [*rows, (name, body)]:
                    item = tarfile.TarInfo(path); item.size = len(data); item.mode = 0o644
                    packet.addfile(item, io.BytesIO(data))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.admit()

    def test_recursive_plaintext_layers_are_authenticated_in_their_original_order(self):
        image, config, size = fixture.archive(self.path, second_body=b'node.example.invalid\n')
        report, sbom, context = fixture.evidence(image, config, size)
        self.assertEqual(self.admit(report=report, sbom=sbom, context=context), [])
        properties = sbom['metadata']['component']['properties']
        diffids = [item for item in properties if item['name'].endswith(':DiffID')]
        diffids[0]['value'], diffids[1]['value'] = diffids[1]['value'], diffids[0]['value']
        with self.assertRaises(ValueError): self.admit(report=report, sbom=sbom, context=context)

    def test_foreign_layer_chains_cannot_add_commands_change_epoch_or_escape_source(self):
        image, config, size = fixture.archive(self.path, second_body=b'node.example.invalid\n')
        report, sbom, context = fixture.evidence(image, config, size)
        with tarfile.open(self.path) as packet:
            rows = [(item.name, packet.extractfile(item).read()) for item in packet if item.isfile()]
        for mutation in [None, 'epoch', 'command', 'parent', 'extra-chain']:
            placeholder = {'container_config': {}, 'created': '1970-01-01T01:00:00+01:00', 'id': 'c' * 64, 'os': 'linux'}
            terminal = {'architecture': 'amd64', 'os': 'linux', 'created': config['created'],
                'id': 'd' * 64, 'parent': placeholder['id'], 'config': config['config'], 'container_config': {}}
            if mutation == 'epoch': placeholder['created'] = '2026-01-01T00:00:00Z'
            if mutation == 'command': placeholder['container_config'] = {'Cmd': ['/runner']}
            if mutation == 'parent': terminal['parent'] = 'e' * 64
            metadata = [placeholder, terminal]
            if mutation == 'extra-chain': metadata.append({**placeholder, 'id': 'e' * 64})
            with tarfile.open(self.path, 'w') as packet:
                extras = []
                for value in metadata:
                    body = json.dumps(value, sort_keys=True).encode()
                    extras.append(('blobs/sha256/' + hashlib.sha256(body).hexdigest(), body))
                for name, body in [*rows, *extras]:
                    item = tarfile.TarInfo(name); item.size = len(body); item.mode = 0o644
                    packet.addfile(item, io.BytesIO(body))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.admit(report=report, sbom=sbom, context=context)

    def test_compressed_layer_cannot_bypass_small_expanded_data_bound(self):
        import oci_fixture
        body = bytes((32 << 20) + 1)
        config = copy.deepcopy(self.config)
        config['rootfs']['diff_ids'] = ['sha256:' + hashlib.sha256(body).hexdigest()]
        oci_fixture.write(self.path, oci_fixture.contents(fixture.REFERENCE, config, [body], compressed=True))
        self.assertLess(self.path.stat().st_size, 65536)
        inspected = copy.deepcopy(self.inspected)
        inspected['Id'] = 'sha256:' + hashlib.sha256(oci_fixture.encoded(config)).hexdigest()
        inspected['RootFS']['Layers'] = config['rootfs']['diff_ids']
        report, sbom, context = fixture.evidence(inspected, config, len(body))
        with self.assertRaisesRegex(ValueError, 'decompressed bound'):
            self.admit(report=report, sbom=sbom, context=context)

    def test_binary_scripts_executable_links_devices_whiteouts_and_traversal_refuse(self):
        cases = [{'body': b'\x7fELF\x00\x02binary'}, {'body': b'#!/bin/sh\necho harmless\n'},
                 {'filename': 'data/source.py'}, {'mode': 0o755}, {'member_type': tarfile.SYMTYPE},
                 {'member_type': tarfile.CHRTYPE}, {'filename': 'data/.wh.hidden'},
                 {'filename': '../outside'}, {'entrypoint': ['/runner']}]
        for options in cases:
            inspected, config, size = fixture.archive(self.path, **options)
            report, sbom, context = fixture.evidence(inspected, config, size)
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.admit(report=report, sbom=sbom, context=context)

    def test_linked_hardlinked_oversized_or_ancestor_linked_archive_refuses(self):
        linked = self.root / 'linked.tar'; linked.symlink_to(self.path)
        with self.assertRaises(ValueError): self.admit(archive=linked)
        hard = self.root / 'hard.tar'; os.link(self.path, hard)
        with self.assertRaises(ValueError): self.admit()
        hard.unlink()
        directory = self.root / 'alias'; directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): self.admit(archive=directory / self.path.name)
        with self.path.open('wb') as stream: stream.truncate((32 << 20) + 1)
        with self.assertRaises(ValueError): self.admit()

    def test_admission_returns_content_hashes_and_never_extracts_files(self):
        result = data_image(self.path, fixture.REFERENCE, self.inspected, self.report)
        self.assertEqual(result['files'], {'data/settings.yaml': {'sha256': hashlib.sha256(b'value: harmless\n').hexdigest(), 'bytes': 16}})
        self.assertEqual(list(self.root.iterdir()), [self.path])


class NativeImageScanContextControls(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('TOOLS_NATIVE_IMAGE_SECURITY_ROOT'), 'prepare pinned native scanner before native controls')
    def test_pinned_scanner_data_secret_and_unsupported_binary_controls(self):
        import native_image_security_control as native
        directory = Path(os.environ['TOOLS_NATIVE_IMAGE_SECURITY_ROOT'])
        native.run(directory)


if __name__ == '__main__': unittest.main()
