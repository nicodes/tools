from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'helpers/caddy-pin.sh'


class CaddyPins(unittest.TestCase):
    def test_every_stage_final_stage_and_literal_image_identity_are_required(self):
        pinned = 'caddy:2.11.7-alpine@sha256:' + 'a' * 64
        base = 'FROM golang:1.27.3-alpine AS compiler\nFROM ' + pinned + '\n'
        cases = {
            'floating final': base + 'FROM caddy:2-alpine\n',
            'floating tag': base.replace(pinned, 'caddy:2-alpine'),
            'no digest': base.replace(pinned, 'caddy:2.11.7-alpine'),
            'floating first': 'FROM caddy:2-alpine AS first\n' + base,
            'wrong final': base + 'FROM alpine:3.21\n',
            'platform bypass': 'FROM --platform=linux/amd64 caddy:2-alpine AS first\n' + base,
            'qualified bypass': 'FROM docker.io/library/caddy:2-alpine AS first\n' + base,
            'multiline bypass': 'FROM --platform=linux/amd64 \\\n caddy:2-alpine AS first\n' + base,
            'argument bypass': 'ARG CADDY_IMAGE=caddy:2-alpine\nFROM ${CADDY_IMAGE} AS first\n' + base,
            'missing variant': base.replace('2.11.7-alpine@', '2.11.7@'),
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'Dockerfile'
            for name, content in cases.items():
                path.write_text(content)
                result = subprocess.run(['bash', str(HELPER), str(path)], capture_output=True, text=True, timeout=10)
                with self.subTest(name=name):
                    self.assertNotEqual(result.returncode, 0)
            for name, content in {
                'ordinary': base, 'alias': base.replace(pinned, pinned + ' AS gate'),
                'platform qualified': base.replace(pinned, '--platform=linux/amd64 docker.io/library/' + pinned),
            }.items():
                path.write_text(content)
                result = subprocess.run(['bash', str(HELPER), str(path)], capture_output=True, text=True, timeout=10)
                with self.subTest(name=name):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
