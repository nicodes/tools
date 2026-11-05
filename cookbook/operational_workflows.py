"""Generate bounded native backup, recovery and operational audit callers."""
import json
from pathlib import Path
import re

from configuration import relative,load
import delivery_workflows

NAME=re.compile('[a-z][a-z0-9_-]{0,62}')
ENV=re.compile('[A-Z][A-Z0-9_]{0,79}')
ROLES={'backup','recovery','security','production'}


def bounded(value,maximum,label):
    if type(value) is not int or not 1<=value<=maximum:raise ValueError(label+' requires its bounded native deadline')


def source(root,name,prefixes=('deploy/','tests/','api/','internal/')):
    if not isinstance(name,str) or not name.startswith(prefixes):raise ValueError('operational programs belong to their native owning component')
    return relative(root,name,exists=True)


def entry(root,value,paid=False):
    if not isinstance(value,dict) or set(value)-{'runner','path','args','environment','timeout','target','filter','binary'}:
        raise ValueError('operational checks require native program data')
    runner=value.get('runner')
    if runner not in ({'python','node','bun','bash','cargo-test','cargo-run'} if paid else {'python','node','bun','bash'}):
        raise ValueError('unsupported native operational runner')
    path=source(root,value.get('path'))
    if runner.startswith('cargo-') and path.name!='Cargo.toml':raise ValueError('native Cargo checks select their owning manifest')
    arguments=value.get('args',[])
    if not isinstance(arguments,list) or len(arguments)>32 or any(not isinstance(v,str) or len(v)>1024 or '\0' in v for v in arguments):
        raise ValueError('native arguments must be bounded literal argv')
    environment=value.get('environment',{})
    if not isinstance(environment,dict) or len(environment)>16:raise ValueError('native environment must be bounded literal data')
    for key,item in environment.items():
        if (not ENV.fullmatch(key) or key.startswith(('GITHUB_','MISE_','COOKBOOK_','REQUEST_'))
                or key in {'GH_TOKEN','TOOLS_ROOT','KOMIZO_ACTIONS_ROOT','OPENROUTER_API_KEY'}
                or not isinstance(item,str) or len(item)>2048 or any(c in item for c in '\0\r\n')):
            raise ValueError('native environment cannot replace workflow, SDK or secret authority')
    bounded(value.get('timeout',180),900,'native check')
    for key in ['target','filter','binary']:
        if key in value and (not isinstance(value[key],str) or not re.fullmatch('[A-Za-z0-9_:.-]{1,160}',value[key])):
            raise ValueError('native Cargo selectors must be literal identifiers')
    if runner=='cargo-test' and not {'target','filter'}<=set(value):raise ValueError('paid Cargo test must select its exact native test')
    if runner=='cargo-run' and 'binary' not in value:raise ValueError('paid Cargo run must select its exact native binary')
    return value


def declaration(root,config):
    selected=config.get('operations')
    if not isinstance(selected,dict) or set(selected)!={'definition'}:raise ValueError('operations require one native declaration')
    path=relative(root,selected['definition'],exists=True)
    if not path.is_file() or path.stat().st_size>65536:raise ValueError('operations declaration must be bounded regular source')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or set(value)-{'version','project','backup','recovery','security','production','paid','restore_profiles'} or value.get('version')!=1:
        raise ValueError('unsupported native operational declaration')
    delivery=delivery_workflows.declaration(root,config)
    if value.get('project')!=delivery['project']:raise ValueError('operations and delivery project authority disagree')
    for role in ROLES:
        if role not in value:continue
        record=value[role]
        if not isinstance(record,dict) or set(record)-{'runner','timeout_minutes','environment','schedule','config','gate_variable','concurrency','queue','report_timeout','checks','prepare','evidence','deployed_policy'}:
            raise ValueError('unsupported native '+role+' data')
        if not isinstance(record.get('runner'),str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',record['runner']):raise ValueError('operation requires its original runner')
        bounded(record.get('timeout_minutes'),110 if role=='security' else 45,role)
        if 'report_timeout' in record:bounded(record['report_timeout'],11,'failure report')
        if record.get('environment') not in {None,'production','Production'}:raise ValueError('preserve the original protected production environment')
        schedule=record.get('schedule',[])
        if not isinstance(schedule,list) or len(schedule)>4 or any(not isinstance(v,str) or not re.fullmatch('[0-9*/,-]+(?: [0-9*/,-]+){4}',v) for v in schedule):raise ValueError('operational schedule must be bounded literal cron')
        if role in {'backup','recovery'} and 'checks' in record:raise ValueError('credentialed backup and transport recovery execute only pinned shared code')
        if role=='backup':
            source(root,record.get('config'),('deploy/',))
            if record.get('gate_variable') and not ENV.fullmatch(record['gate_variable']):raise ValueError('backup gates select a named public variable')
            if record.get('concurrency') not in {None,'deploy-production','backup'} or record.get('queue') not in {None,'max'}:raise ValueError('preserve bounded native host serialization')
        for check in record.get('checks',[]):entry(root,check)
        if role=='production' and not record.get('checks'):raise ValueError('production verification requires actual native assertions')
        if role=='security' and record.get('deployed_policy'):source(root,record['deployed_policy'],('deploy/',))
        prepare=record.get('prepare',{})
        if not isinstance(prepare,dict) or set(prepare)-{'packages','browser','rust','native'}:raise ValueError('SDK preparation accepts native package capabilities only')
        for package in prepare.get('packages',[]):relative(root,package,exists=True)
        if prepare.get('browser'):relative(root,prepare['browser'],exists=True)
        if type(prepare.get('rust',False)) is not bool:raise ValueError('Rust SDK preparation must be explicit')
        for check in prepare.get('native',[]):entry(root,check)
        for name in record.get('evidence',[]):
            if not isinstance(name,str) or not name.startswith('.artifacts/'):raise ValueError('operational evidence belongs in generated artifacts')
            relative(root,name)
    paid=value.get('paid',[])
    if not isinstance(paid,list) or len(paid)>4:raise ValueError('paid checks require bounded explicit opt-in declarations')
    seen=set()
    for check in paid:
        if not isinstance(check,dict) or set(check)-{'id','description','runner','timeout_minutes','environment','prepare','check','deployment_required','skip_default'}:
            raise ValueError('unsupported paid native check')
        name=check.get('id','')
        if not isinstance(name,str) or not NAME.fullmatch(name) or name in seen:raise ValueError('paid checks require distinct native dispatch inputs')
        seen.add(name);bounded(check.get('timeout_minutes'),45,'paid check')
        if check.get('environment') not in {'production','Production'}:raise ValueError('paid checks retain their protected approval environment')
        if not re.fullmatch('[A-Za-z0-9_.-]{1,80}',check.get('runner','')):raise ValueError('paid check requires its existing runner')
        if not isinstance(check.get('description'),str) or not 1<=len(check['description'])<=256:raise ValueError('paid checks require a useful bounded dispatch description')
        for key in ['deployment_required','skip_default']:
            if type(check.get(key,False)) is not bool:raise ValueError('paid check gates must be literal booleans')
        entry(root,check.get('check'),paid=True)
        for native in check.get('prepare',[]):entry(root,native)
    profiles=value.get('restore_profiles',[])
    if not isinstance(profiles,list) or len(profiles)>4:raise ValueError('restore profiles require bounded native data')
    for profile in profiles:
        if (not isinstance(profile,dict) or set(profile)!={'name','project','config'}
                or not NAME.fullmatch(profile.get('name','')) or not NAME.fullmatch(profile.get('project',''))):
            raise ValueError('invalid native restore profile')
        source(root,profile['config'],('deploy/',))
    return {**value,'komizo_ref':delivery['komizo_ref'],'definition':selected['definition']}


def expression(value):return chr(36)+'{{ '+value+' }}'


def workflow_files(root,revision,config=None):
    config=load(root) if config is None else config
    if not config.get('operations'):return {}
    if not re.fullmatch('[a-f0-9]{40}',revision):raise ValueError('operation callers require an immutable cookbook pin')
    data=declaration(root,config);pin=data['komizo_ref'];result={}
    permissions={'backup':{'contents':'read','issues':'write','id-token':'write','attestations':'read'},
        'recovery':{'contents':'read','actions':'read','attestations':'read'},
        'security':{'contents':'read','actions':'read','packages':'read','deployments':'read','issues':'write','attestations':'read'},
        'production':{'contents':'read','actions':'read','attestations':'read'}}
    for role in ['backup','recovery','security','production']:
        if role not in data:continue
        record=data[role];paid=data.get('paid',[]) if role=='production' else []
        name={'backup':'Backup','recovery':'Recovery','security':'Security Scan','production':'Production Verification'}[role]
        lines=['# Generated cookbook caller. Change native operation data and regenerate.','name: '+name,"'on':"]
        if record.get('schedule'):
            lines+=['  schedule:']
            for cron in record['schedule']:lines+=['    - cron: '+json.dumps(cron)]
        lines+=['  workflow_dispatch:']
        if role=='recovery':
            lines+=['    inputs:']
            for key,description in [('source-run-id','Successful exact main CD/Backup run containing the encrypted export'),('artifact-id','Immutable Actions encrypted backup artifact ID')]:
                lines+=['      '+key+':','        description: '+json.dumps(description),'        required: true','        type: string']
        elif paid:
            lines+=['    inputs:']
            for check in paid:lines+=['      '+check['id']+':','        description: '+json.dumps(check['description']),'        type: boolean','        default: false']
        lines+=['permissions: {}']
        concurrency=record.get('concurrency',{'production':'production-verification','recovery':'recovery'}.get(role))
        if concurrency:
            lines+=['concurrency:','  group: '+concurrency,'  cancel-in-progress: false']
            if record.get('queue'):lines+=['  queue: '+record['queue']]
        lines+=['jobs:']
        jobs=[(role,record,None)]
        jobs += [(check['id'],check,check['id']) for check in paid]
        for job,settings,paid_id in jobs:
            operation='paid' if paid_id else role
            condition="github.ref == 'refs/heads/main'"
            if paid_id:condition+=' && inputs.'+paid_id
            elif role=='production':
                for check in paid:
                    if check.get('skip_default'):condition+=' && !inputs.'+check['id']
            if record.get('gate_variable') and role=='backup':condition+=" && vars."+record['gate_variable']+" == 'true'"
            if operation=='security':callee='cookbook-security-audit'
            elif operation in {'production','paid'}:callee='cookbook-production-check' if settings.get('environment') else 'cookbook-public-production-check'
            else:callee='cookbook-'+operation
            lines+=['  '+job+':','    if: '+expression(condition),'    uses: nicodes/komizo-actions/.github/workflows/'+callee+'.yml@'+pin,'    permissions:']
            for key,value in permissions[role].items():lines+=['      '+key+': '+value]
            lines+=['    with:','      tools-ref: '+revision,'      komizo-ref: '+pin,'      declaration: '+json.dumps(data['definition']),
                    '      runner: '+settings['runner'],'      timeout-minutes: '+str(settings['timeout_minutes'])]
            if operation in {'security','production','paid'}:lines+=['      operation: '+operation,'      check: '+json.dumps(paid_id or '')]
            if role=='security':lines+=['      report-timeout: '+str(record.get('report_timeout',5))]
            if settings.get('environment'):lines+=['      environment: '+settings['environment']]
            if role=='recovery':
                for key in ['source-run-id','artifact-id']:lines+=['      '+key+': '+expression('inputs.'+key)]
            if role=='backup':
                lines+=['    secrets:']
                for key in ['KOMIZO_DEPLOY_KEY','BACKUP_S3_ACCESS_KEY','BACKUP_S3_SECRET_KEY']:lines+=['      '+key+': '+expression('secrets.'+key)]
            if paid_id:lines+=['    secrets:','      OPENROUTER_API_KEY: '+expression('secrets.OPENROUTER_API_KEY')]
        filename={'production':'production-verification','security':'security-scan'}.get(role,role)
        result['.github/workflows/'+filename+'.yml']='\n'.join(lines)+'\n'
    return result
