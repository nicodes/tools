"""Retain complete vulnerability, secret and SBOM evidence for built game images."""
import json
from pathlib import Path
import re
import tomllib

from configuration import relative
import images


def declaration(root, config):
    value=config.get('components',{}).get('image_security')
    if not isinstance(value,dict) or set(value)!={'reports'}:
        raise ValueError('image security requires a generated report directory')
    directory=relative(root,value['reports'])
    if not directory.is_relative_to(root/'.artifacts'):
        raise ValueError('image evidence belongs to generated checkout artifacts')
    pin=tomllib.loads((root/'mise.toml').read_text()).get('env',{}).get('TRIVY_IMAGE','')
    if not re.fullmatch(r'aquasec/trivy@sha256:[a-f0-9]{64}',pin):
        raise ValueError('image scanner SDK requires its exact reviewed Mise digest')
    return directory,pin


def evidence_paths(root, config):
    directory,_=declaration(root,config)
    return [(directory/name/filename).relative_to(root).as_posix()
            for name in images.components(images.definition(root,config)) for filename in ['security.json','sbom.json']]


def setup(root, config, environment, run):
    _,scanner=declaration(root,config)
    run(['docker','pull',scanner],root,environment,timeout=300)


def scan(root, config, environment, run):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
    from image_security_report import blocking_findings
    directory,scanner=declaration(root,config)
    definition=images.definition(root,config)
    records=zip(images.components(definition),images.references(definition,environment['SOURCE_COMMIT']),strict=True)
    run(['docker','image','inspect',scanner],root,environment)
    for name,reference in records:
        output=relative(root,(directory/name).relative_to(root).as_posix());output.mkdir(parents=True,exist_ok=True)
        base=['docker','run','--rm','-v','/var/run/docker.sock:/var/run/docker.sock','-v',str(output)+':/reports',scanner,'image','--quiet','--skip-version-check']
        run([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json',reference],root,environment)
        run([*base,'--format','cyclonedx','--output','/reports/sbom.json',reference],root,environment)
        report=json.loads((output/'security.json').read_text())
        if blocking_findings(report):raise ValueError('release image has High/Critical vulnerabilities or detected secrets; full report retained')
        if not (output/'sbom.json').is_file():raise ValueError('release image SBOM is missing')
