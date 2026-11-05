"""Accepted source setup and strictly offline SDK admission with real Git bytes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS/'cookbook'))
import komizo_sdk as sdk


class KomizoSdk(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='komizo sdk ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.source=self.root/sdk.SDK/'source';self.source.mkdir(parents=True)
        self.command('init',str(self.source))
        self.command('-C',str(self.source),'remote','add','origin',sdk.REPOSITORY)
        (self.source/'module.py').write_text('VALUE = "reviewed"\n')
        self.command('-C',str(self.source),'add','module.py')
        self.command('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false',
            '-C',str(self.source),'commit','-m','Fixture source')
        self.revision=self.command('-C',str(self.source),'rev-parse','HEAD').strip()
        (self.root/'deploy').mkdir()
        self.config={'delivery':{'definition':'deploy/delivery.json'},'preview':{'definition':'deploy/preview.json'}}
        for name in ['delivery','preview']:(self.root/'deploy'/f'{name}.json').write_text(json.dumps({'komizo_ref':self.revision}))
        self.receipt=self.root/sdk.SDK/'source.json'
        self.receipt.write_text(json.dumps(dict(version=1,repository=sdk.REPOSITORY,source=self.revision,accepted_main=True)))

    def command(self,*arguments):
        return subprocess.check_output(['git',*arguments],text=True,stderr=subprocess.DEVNULL,timeout=10)

    def test_exact_clean_sdk_is_checked_offline_and_absent_declarations_do_nothing(self):
        real=sdk.capture;commands=[]
        def capture(arguments,*values):
            commands.append(arguments)
            return real(arguments,*values)
        with patch.object(sdk,'capture',side_effect=capture):
            self.assertEqual(sdk.check(self.root,self.config,dict(os.environ)),self.source)
        self.assertTrue(commands)
        self.assertTrue(all(command[0]=='git' for command in commands))
        self.assertFalse(any('fetch' in command or 'checkout' in command for command in commands))
        self.assertIsNone(sdk.check(self.root,{},dict(os.environ)))

    def test_disagreeing_invalid_or_arbitrary_source_pins_are_refused(self):
        path=self.root/'deploy/preview.json'
        for value in ['b'*40,'main','https://elsewhere.invalid/project.git','$(touch bad)']:
            path.write_text(json.dumps({'komizo_ref':value}))
            with self.subTest(value=value),self.assertRaises(ValueError):sdk.check(self.root,self.config,dict(os.environ))

    def test_dirty_untracked_ignored_and_wrong_remote_cache_cannot_supply_programs(self):
        for name in ['module.py','unexpected.py','ignored.pyc']:
            path=self.source/name
            if name=='ignored.pyc':
                (self.source/'.git/info/exclude').write_text('*.pyc\n')
            original=path.read_bytes() if path.exists() else None
            path.write_text('raise SystemExit("unreviewed")\n')
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'unexpected files'):
                sdk.check(self.root,self.config,dict(os.environ))
            if original is None:path.unlink()
            else:path.write_bytes(original)
        self.command('-C',str(self.source),'remote','set-url','origin','https://foreign.invalid/repo.git')
        with self.assertRaisesRegex(ValueError,'source authority'):sdk.check(self.root,self.config,dict(os.environ))

    def test_setup_is_the_only_network_path_and_requires_accepted_main_before_fetch(self):
        real=sdk.capture;executed=[];relation='behind'
        def capture(arguments,*values):
            return relation if arguments[0]=='gh' else real(arguments,*values)
        def execute(*values):executed.append(values)
        with patch.object(sdk,'capture',side_effect=capture):
            with self.assertRaisesRegex(ValueError,'accepted on main'):
                sdk.setup(self.root,self.config,dict(os.environ),execute)
            self.assertFalse(executed)
            relation='ahead'
            self.assertEqual(sdk.setup(self.root,self.config,dict(os.environ),execute),self.source)
        self.assertEqual(len(executed),2)
        fetch=executed[0][0]
        self.assertIn('--no-recurse-submodules',fetch)
        self.assertEqual(fetch[-2:],['origin',self.revision])
        self.assertEqual(json.loads(self.receipt.read_text())['source'],self.revision)

    def test_source_receipt_and_linked_checkout_cannot_replace_the_declared_commit(self):
        original=self.receipt.read_text()
        self.receipt.write_text(original.replace(self.revision,'b'*40))
        with self.assertRaisesRegex(ValueError,'exact accepted'):sdk.check(self.root,self.config,dict(os.environ))
        self.receipt.write_text(original)
        saved=self.source.with_name('saved');self.source.rename(saved);self.source.symlink_to(saved,target_is_directory=True)
        with self.assertRaises(ValueError):sdk.check(self.root,self.config,dict(os.environ))


if __name__=='__main__':unittest.main()
