import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('site_contract', Path(__file__).parents[1] / 'helpers/site-contract.py')
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
PAGE = '<!doctype html><html lang="en"><head><title>Product</title><meta name="description" content="Description"><meta name="viewport" content="width=device-width"></head><body><main>{}</main></body></html>'


class StaticSiteContract(unittest.TestCase):
    def test_visible_labels_image_links_and_decorative_images(self):
        html = PAGE.format('<a href="/"><img alt="Home" src="logo.svg"></a><img alt="" src="decoration.svg"><button>Continue</button><label for="email">Email</label><input id="email"><label>Search<input></label>')
        self.assertEqual(helper.findings(html), [])

    def test_missing_metadata_landmark_and_control_names_fail(self):
        for old in [' lang="en"', '<title>Product</title>', '<meta name="description" content="Description">', '<meta name="viewport" content="width=device-width">']:
            self.assertTrue(helper.findings(PAGE.format('').replace(old, '')))
        self.assertTrue(helper.findings(PAGE.format('').replace('<main>', '<div>').replace('</main>', '</div>')))
        errors = helper.findings(PAGE.format('<a href="/"><svg></svg></a><button></button><input placeholder="Email"><img src="photo.jpg">'))
        self.assertEqual(len(errors), 4)

    def test_labelledby_must_resolve_and_script_content_is_not_a_name(self):
        self.assertEqual(helper.findings(PAGE.format('<span id="name">Open</span><button aria-labelledby="name"></button>')), [])
        self.assertTrue(helper.findings(PAGE.format('<button aria-labelledby="missing"><script>text</script></button>')))

    def test_field_values_and_options_are_not_accessible_labels(self):
        errors = helper.findings(PAGE.format('<select><option>One</option></select><textarea>Text</textarea>'))
        self.assertEqual(errors, ['select requires a label', 'textarea requires a label'])

    def test_empty_and_linked_exports_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                helper.check(root)
            outside = root / 'outside'
            outside.write_text(PAGE.format(''))
            (root / 'index.html').symlink_to(outside)
            with self.assertRaises(ValueError):
                helper.check(root)

    def test_checks_nested_pages_in_actual_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'nested').mkdir()
            (root / 'index.html').write_text(PAGE.format('Home'))
            (root / 'nested/index.html').write_text(PAGE.format('<button></button>'))
            files, errors = helper.check(root)
            self.assertEqual(len(files), 2)
            self.assertEqual(errors, ['nested/index.html: button requires an accessible name'])


class WebsitePerformanceContract(unittest.TestCase):
    def budget(self, **overrides):
        return dict(max_total_bytes=4096, max_file_bytes=2048, max_html_gzip_bytes=1024, max_javascript_gzip_bytes=1024, **overrides)

    def test_actual_bytes_and_all_budget_categories_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'index.html').write_text(PAGE.format('Home'))
            (root/'client.js').write_text('console.log("loaded")')
            metrics, errors=helper.performance(root,self.budget())
            self.assertEqual(errors,[])
            self.assertEqual(metrics['total_bytes'],sum(path.stat().st_size for path in root.iterdir()))
            for key in ['max_total_bytes','max_file_bytes','max_html_gzip_bytes','max_javascript_gzip_bytes']:
                budget=self.budget();budget[key]=1
                self.assertTrue(helper.performance(root,budget)[1],key)

    def test_duplicate_references_do_not_hide_oversized_unreferenced_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'index.html').write_text(PAGE.format('<img alt="" src="art.png"><img alt="" src="art.png">'))
            (root/'art.png').write_bytes(b'x'*2049)
            self.assertTrue(any('art.png' in error for error in helper.performance(root,self.budget())[1]))

    def test_bad_declarations_and_linked_outputs_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for budget in [{},self.budget()|{'max_file_bytes':True},self.budget()|{'unknown':4},self.budget()|{'max_file_bytes':0}]:
                with self.assertRaises(ValueError):helper.performance(root,budget)
            (root/'linked').symlink_to(root/'missing')
            with self.assertRaises(ValueError):helper.performance(root,self.budget())

    def test_toml_budget_cli_enforces_the_same_byte_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'export'
            output.mkdir()
            (output / 'index.html').write_text(PAGE.format('Home'))
            budget_path = root / 'budget.toml'
            helper._source.write(budget_path, self.budget())
            command = [sys.executable, '-I', '-B', str(Path(helper.__file__)), str(output), '--budget', str(budget_path)]
            good = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            limited = self.budget(); limited['max_total_bytes'] = 1
            helper._source.write(budget_path, limited)
            bad = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('exceeds total budget 1', bad.stderr)
            budget_path.write_text('max_total_bytes = 2026-11-07\n')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
