"""Admit completed filesystem scans of exact immutable static archive bytes."""
import datetime
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
import uuid
import zlib

MAX_BYTES = 8 << 30
MAX_MEMBERS = 65536
CHUNK = 1 << 20
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


def sibling(name):
    spec = importlib.util.spec_from_file_location('_tools_filesystem_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


image = sibling('image_scan_context')
registry = sibling('oci_registry')


def archive_inventory(path, artifact):
    if (not isinstance(artifact, dict) or set(artifact) != {'kind','reference','digest','size_bytes','media_type'}
            or artifact['kind'] != 'archive' or artifact['media_type'] != registry.STATIC_LAYER
            or type(artifact['size_bytes']) is not int or not 0 < artifact['size_bytes'] <= MAX_BYTES
            or registry.reference(artifact['reference'])[2] != registry.digest(registry.artifact_manifest(artifact['digest'],artifact['size_bytes']))):
        raise ValueError('filesystem scan requires an exact static artifact descriptor')
    path, fingerprint = image.regular(path, MAX_BYTES)
    deadline = time.monotonic() + 600
    def remaining():
        if time.monotonic() >= deadline:raise ValueError('filesystem archive admission exceeded its deadline')
    def identity(info):return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns
    if fingerprint[2] != artifact['size_bytes']:
        raise ValueError('filesystem input archive size differs from its descriptor')
    descriptor = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as source:
        if identity(os.fstat(source.fileno())) != fingerprint:
            raise ValueError('filesystem input archive changed before admission')
        raw_hash = hashlib.sha256(); decoder = zlib.decompressobj(16+zlib.MAX_WBITS); expanded = 0
        while block := source.read(CHUNK):
            remaining();raw_hash.update(block)
            if decoder.eof:raise ValueError('static archive has trailing compressed data')
            while block:
                body=decoder.decompress(block,CHUNK);expanded+=len(body);block=decoder.unconsumed_tail
                if expanded > MAX_BYTES + MAX_MEMBERS*1024 + CHUNK:
                    raise ValueError('static archive exceeds its expanded stream bound')
                if decoder.unused_data:raise ValueError('static archive has trailing compressed data')
                remaining()
        if not decoder.eof or 'sha256:'+raw_hash.hexdigest()!=artifact['digest']:
            raise ValueError('filesystem input archive differs from its admitted bytes')
        source.seek(0)
        import gzip
        stream=gzip.GzipFile(fileobj=source)
        def read(length):
            remaining();value=stream.read(length)
            if len(value)!=length:raise ValueError('static archive has a truncated member')
            return value
        inventory={};names=set();total=0
        while True:
            header=read(512)
            if header==b'\0'*512:
                padding=512
                while body:=stream.read(CHUNK):
                    remaining();padding+=len(body)
                    if padding>CHUNK or any(body):raise ValueError('static archive hides trailing data')
                if padding<1024:raise ValueError('static archive has a truncated end marker')
                break
            member=tarfile.TarInfo.frombuf(header,'utf-8','strict')
            name=member.name.rstrip('/')
            if (not name or len(name)>1024 or str(PurePosixPath(name))!=name
                    or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                    or '\\' in name or any(ord(char)<32 for char in name) or name in names
                    or member.type not in {tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE}
                    or member.size<0 or member.isdir() and member.size):
                raise ValueError('static archive has unsafe, repeated or linked members')
            names.add(name);total+=member.size
            if len(names)>MAX_MEMBERS or total>MAX_BYTES:
                raise ValueError('static archive exceeds its member or expanded payload bound')
            hasher=hashlib.sha256();left=member.size
            while left:
                body=read(min(CHUNK,left));left-=len(body);hasher.update(body)
            if member.size%512:read(512-member.size%512)
            if member.isfile():inventory[name]={'sha256':hasher.hexdigest(),'size':member.size,'mode':0o644}
        if not inventory:raise ValueError('static archive must contain actual files')
        if identity(os.fstat(source.fileno()))!=fingerprint or image.regular(path,MAX_BYTES)[1]!=fingerprint:
            raise ValueError('filesystem input archive changed during admission')
        return inventory


def validate(report, sbom, context, input_archive):
    if (not isinstance(context,dict) or set(context)!={'source','release_digest','artifact','target','scanner','started_at','finished_at','before','after'}
            or not isinstance(context['source'],str) or not image.REVISION.fullmatch(context['source'])
            or not isinstance(context['release_digest'],str) or not DIGEST.fullmatch(context['release_digest'])
            or context['target']!='/inputs/static'):
        raise ValueError('filesystem scan requires a closed exact release completion context')
    scanner=context['scanner']
    if (not isinstance(scanner,dict) or set(scanner)!={'reference','id','version'}
            or not isinstance(scanner['reference'],str) or not image.SCANNER.fullmatch(scanner['reference'])
            or not isinstance(scanner['id'],str) or not DIGEST.fullmatch(scanner['id'])
            or scanner['version']!='0.70.0'):
        raise ValueError('filesystem scan requires the prepared immutable scanner identity')
    start,finish=image.timestamp(context['started_at']),image.timestamp(context['finished_at'])
    if not datetime.timedelta(0)<=finish-start<=datetime.timedelta(minutes=20):
        raise ValueError('filesystem scan completion interval is invalid')
    inventory=archive_inventory(input_archive,context['artifact'])
    for actual in [context['before'],context['after']]:
        if (not isinstance(actual,dict) or any(not isinstance(record,dict)
                or set(record)!={'sha256','size','mode'} or type(record['size']) is not int
                or type(record['mode']) is not int for record in actual.values())):
            raise ValueError('filesystem scan requires exact typed file inventories')
    if context['before']!=inventory or context['after']!=inventory:
        raise ValueError('scanned filesystem differs from the accepted static archive')
    if (not isinstance(report,dict) or type(report.get('SchemaVersion')) is not int or report['SchemaVersion']!=2
            or report.get('ArtifactType')!='filesystem' or report.get('ArtifactName')!=context['target']
            or report.get('ArtifactID','')!='' or report.get('Trivy')!={'Version':scanner['version']}
            or report.get('Metadata',{})!={}):
        raise ValueError('filesystem security report schema, target or scanner differs')
    try:uuid.UUID(report.get('ReportID',''))
    except (ValueError,AttributeError,TypeError):raise ValueError('filesystem report has no completed scan identifier') from None
    if (not isinstance(sbom,dict) or sbom.get('bomFormat')!='CycloneDX' or sbom.get('specVersion')!='1.6'
            or type(sbom.get('version')) is not int or sbom['version']!=1
            or any(not isinstance(sbom.get(name),list) for name in ['components','dependencies','vulnerabilities'])):
        raise ValueError('filesystem SBOM schema or complete inventories are missing')
    metadata=sbom.get('metadata',{});tools=metadata.get('tools',{}).get('components',[]) if isinstance(metadata,dict) else []
    if (not isinstance(tools,list) or len(tools)!=1 or not isinstance(tools[0],dict)
            or tools[0].get('group')!='aquasecurity' or tools[0].get('name')!='trivy' or tools[0].get('version')!=scanner['version']):
        raise ValueError('filesystem SBOM scanner differs from the completed scan')
    component=metadata.get('component',{})
    if (not isinstance(component,dict) or component.get('type')!='application' or component.get('name')!=context['target']
            or not isinstance(component.get('bom-ref'),str) or not component['bom-ref']
            or component.get('properties')!=[{'name':'aquasecurity:trivy:SchemaVersion','value':'2'}]
            or not any(isinstance(item,dict) and item.get('ref')==component['bom-ref'] and isinstance(item.get('dependsOn'),list) for item in sbom['dependencies'])):
        raise ValueError('filesystem SBOM does not bind its actual filesystem target')
    for value in [report.get('CreatedAt'),metadata.get('timestamp')]:
        stamp=image.timestamp(value)
        if not start-datetime.timedelta(seconds=2)<=stamp<=finish+datetime.timedelta(seconds=2):
            raise ValueError('filesystem security evidence is outside its completed scan')
    results=report.get('Results',[])
    if not isinstance(results,list):raise ValueError('filesystem scan findings must be complete lists')
    findings=[]
    for result in results:
        if (not isinstance(result,dict) or not isinstance(result.get('Target'),str) or not result['Target']
                or result.get('Class') not in {'os-pkgs','lang-pkgs','secret'}):
            raise ValueError('filesystem result has an unsupported target or scanner class')
        for name in ['Vulnerabilities','Secrets']:
            if name in result and (not isinstance(result[name],list) or any(not isinstance(item,dict) for item in result[name])):
                raise ValueError('filesystem findings must be complete lists of records')
        for vulnerability in result.get('Vulnerabilities',[]):
            if vulnerability.get('Severity') not in {'UNKNOWN','LOW','MEDIUM','HIGH','CRITICAL'}:
                raise ValueError('filesystem vulnerability has an unsupported severity')
            if vulnerability['Severity'] in {'HIGH','CRITICAL'}:findings.append(vulnerability)
        findings.extend(result.get('Secrets',[]))
    return findings
