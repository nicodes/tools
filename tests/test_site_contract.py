import importlib.util
from pathlib import Path
import sys
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


if __name__ == '__main__':
    unittest.main()
