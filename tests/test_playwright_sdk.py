import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cookbook'))
import playwright_sdk as browsers


class PlainBrowserSDK(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.package=self.root/'tests/browser';self.package.mkdir(parents=True)
        (self.package/'package.json').write_text(json.dumps({'devDependencies':{'playwright':'1.63.0'}}))
        installed=self.package/'node_modules/playwright';installed.mkdir(parents=True)
        (installed/'package.json').write_text('{"version":"1.63.0"}\n')
        self.config={'node':{'packages':[{'directory':'tests/browser','manager':'bun','ignore_scripts':True}]},
                     'components':{'playwright':{'packages':['tests/browser'],'browsers':['chromium']}}}

    def test_setup_uses_the_frozen_native_package_without_resolving_an_external_cli(self):
        calls=[]
        browsers.setup(self.root,self.config,{'GITHUB_ACTIONS':'true'},lambda *args,**kwargs:calls.append((args,kwargs)))
        self.assertEqual(calls[0][0][0],['bun','x','--no-install','playwright','install','--with-deps','chromium'])
        self.assertEqual(calls[0][0][1],self.package)
        self.assertEqual(calls[0][1]['timeout'],600)
        calls.clear()
        browsers.setup(self.root,self.config,{},lambda *args,**kwargs:calls.append(args[0]))
        self.assertNotIn('--with-deps',calls[0])

    def test_undeclared_packages_duplicate_engines_and_arbitrary_install_arguments_refuse(self):
        for packages,engines in [(['other'],['chromium']),(['tests/browser'],['chromium','chromium']),
                                 (['tests/browser'],['--all']),(['tests/browser'],'chromium')]:
            with self.subTest(packages=packages,engines=engines),self.assertRaises(ValueError):
                browsers.selection(self.root,self.config,packages,engines)
        self.config['components']['playwright']['command']='install anything'
        with self.assertRaises(ValueError):browsers.declaration(self.root,self.config)

    def test_missing_or_external_installed_package_refuses_before_any_sdk_effect(self):
        manifest=self.package/'node_modules/playwright/package.json'
        manifest.unlink()
        calls=[]
        with self.assertRaises(ValueError):browsers.setup(self.root,self.config,{},lambda *args,**kwargs:calls.append(args))
        external=self.root/'external.json';external.write_text('{}')
        manifest.symlink_to(external)
        with self.assertRaises(ValueError):browsers.setup(self.root,self.config,{},lambda *args,**kwargs:calls.append(args))
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
