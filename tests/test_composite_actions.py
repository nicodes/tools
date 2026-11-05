import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('composite_actions',Path(__file__).parents[1]/'helpers/composite-actions.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class CompositeSchema(unittest.TestCase):
    def test_native_input_names_and_bounded_shell_programs_remain_valid(self):
        module.validate({'inputs':{'timeout-minutes':{}},'runs':{'using':'composite','steps':[
            {'run':'timeout --foreground 120s command','shell':'bash'},
            {'uses':'actions/cache@'+'a'*40,'with':{'path':'cache'},'if':'false'}]}},'fixture')

    def test_workflow_only_fields_fail_even_in_inactive_composite_steps(self):
        for field in ['timeout-minutes','permissions','runs-on','needs']:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'unsupported fields'):
                module.validate({'runs':{'using':'composite','steps':[{'uses':'owner/action@'+'a'*40,'if':'false',field:2}]}},'fixture')

    def test_step_cannot_omit_shell_or_mix_action_and_program(self):
        for step in [{'run':'program'},{'uses':'action','run':'program','shell':'bash'},{}]:
            with self.subTest(step=step),self.assertRaises(ValueError):
                module.validate({'runs':{'using':'composite','steps':[step]}},'fixture')


if __name__=='__main__':unittest.main()
