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
    definition=images.definition(root,config)
    records=list(zip(images.components(definition),images.references(definition,environment['SOURCE_COMMIT']),strict=True))
    return scan_references(root,config,environment,run,records)


def scan_references(root, config, environment, run, records):
    """Scan only literal source-family refs; historical authority is the caller's verified CD record.

    This creates scanner evidence only. It never issues a current-source build,
    product-test or aggregate acceptance receipt for historical images.
    """
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
    from image_security_report import blocking_findings
    directory,scanner=declaration(root,config)
    definition=images.definition(root,config)
    components=images.components(definition)
    current=dict(zip(components,images.references(definition,environment['SOURCE_COMMIT']),strict=True))
    if (not isinstance(records,(list,tuple)) or not 1<=len(records)<=len(components)
            or any(not isinstance(record,(list,tuple)) or len(record)!=2 for record in records)):
        raise ValueError('image scans require a bounded nonempty component/reference inventory')
    seen=set()
    for name,reference in records:
        if not isinstance(name,str) or name not in components or name in seen or not isinstance(reference,str):
            raise ValueError('image scans require distinct declared shipping components')
        seen.add(name)
        immutable=definition['image_base']+'-'+name+'@sha256:'
        if reference!=current[name] and not (reference.startswith(immutable) and re.fullmatch('[a-f0-9]{64}',reference[len(immutable):])):
            raise ValueError('image scans require this source tag or the exact immutable component family')
    run(['docker','image','inspect',scanner],root,environment)
    for name,reference in records:
        output=relative(root,(directory/name).relative_to(root).as_posix());output.mkdir(parents=True,exist_ok=True)
        base=['docker','run','--rm','-v','/var/run/docker.sock:/var/run/docker.sock','-v',str(output)+':/reports',scanner,'image','--quiet','--skip-version-check']
        run([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json',reference],root,environment)
        run([*base,'--format','cyclonedx','--output','/reports/sbom.json',reference],root,environment)
        report=json.loads((output/'security.json').read_text())
        if blocking_findings(report):raise ValueError('release image has High/Critical vulnerabilities or detected secrets; full report retained')
        if not (output/'sbom.json').is_file():raise ValueError('release image SBOM is missing')
