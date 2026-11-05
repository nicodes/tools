"""The actual manifest encoder refuses syntax it cannot preserve."""
from pathlib import Path
import subprocess
import tempfile
import unittest
TOOLS=Path(__file__).resolve().parents[1]
class Hostnames(unittest.TestCase):
    def encode(self,text):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'deploy').mkdir();(root/'deploy/komizo.yml').write_text(text)
            result=subprocess.run(['bun',str(TOOLS/'helpers/hostnames.mjs')],cwd=root,text=True,capture_output=True,timeout=30)
            output=root/'.artifacts/config/hostnames'
            return result.returncode,output.read_text() if output.exists() else None
    def test_actual_encoder_preserves_routes_and_refuses_ignored_or_unsafe_data(self):
        text='compose: compose.yml\nhostnames:\n  - name: app.example.test\n    container: gateway\n'
        self.assertEqual(self.encode(text),(0,'app.example.test -> gateway\n'))
        for invalid in [text.replace('compose.yml','../other.yml'),text+'unknown: true\n',
                text.replace('app.example.test','app.example.test;bad'),text+text.split('hostnames:\n')[1],
                text+'    unknown: true\n',text.replace('container: gateway','container: [gateway]')]:
            with self.subTest(invalid=invalid):
                code,output=self.encode(invalid);self.assertNotEqual(code,0);self.assertIsNone(output)
