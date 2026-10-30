import json
from pathlib import Path
import subprocess
import unittest
ROOT=Path(__file__).parents[1]

def load(name):
 result=subprocess.check_output(['bun','--eval',"const fs=require('node:fs'); process.stdout.write(JSON.stringify(Bun.YAML.parse(fs.readFileSync(process.argv[1],'utf8'))));",str(ROOT/'.github/workflows'/name)],text=True)
 return json.loads(result)

class OptionalWorkflows(unittest.TestCase):
 def test_source_command_is_caller_owned_and_images_are_optional(self):
  doc=load('reusable-security-scan.yml')
  self.assertEqual(set(doc['on']),{'workflow_call'})
  inputs=doc['on']['workflow_call']['inputs']
  self.assertTrue(inputs['source-command']['required'])
  self.assertFalse(inputs['scan-images']['default'])
  job=doc['jobs']['scan'];self.assertNotIn('environment',job)
  source=next(s for s in job['steps'] if s.get('name')=='Scan every source module')
  self.assertEqual(source['env']['SOURCE_COMMAND'],'${{ inputs.source-command }}')
  self.assertNotIn('${',source['run'])
  for name in ('Log in to container registry','Scan deployed runtime images'):
   self.assertEqual(next(s for s in job['steps'] if s.get('name')==name)['if'],'inputs.scan-images')
 def test_reusable_helpers_do_not_grant_host_or_oidc_authority(self):
  for name in ('reusable-security-scan.yml','reusable-dependency-maintenance.yml'):
   doc=load(name)
   self.assertEqual(set(doc['on']),{'workflow_call'})
   for job in doc['jobs'].values():
    self.assertNotIn('concurrency',job)
    self.assertNotIn('environment',job)
    self.assertNotEqual(job['permissions'].get('id-token'),'write')
    self.assertFalse(any('komizo' in s.get('uses','') or 'komizo exec' in s.get('run','') for s in job['steps']))
