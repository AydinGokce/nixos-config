"""Explicit, resumable molecular-dynamics protocols; no cloud side effects.

Planning uses only the standard library. Upstream pmx/BFEE3/PLUMED imports and
GROMACS checks run as visible worker stages in the pinned runtime.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys


SCHEMA = 'bio-md-request.v1'
PROTOCOLS = {'pmx_binding_ddg', 'plumed_metadynamics', 'bfee3_geometric'}
AMINO = dict(zip(('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL').split(), 'ARNDCQEGHILKMFPSTWYV'))
BFEE_STEPS = ['000_eq','001_RMSD_bound','002_euler_theta','003_euler_phi','004_euler_psi',
              '005_polar_theta','006_polar_phi','007_r','008_RMSD_unbound']
SOURCE_URLS = {
    'pmx': 'https://degrootlab.github.io/pmx/tutorials/protein_mut.html',
    'bfee3': 'https://github.com/fhh2626/BFEE3/tree/8b3e33e39b74bf1a7b58167df92af7f5f5795180',
    'plumed': 'https://www.plumed.org/doc-v2.10/user-doc/html/_m_e_t_a_d.html',
    'gromacs': 'https://manual.gromacs.org/current/user-guide/mdp-options.html',
}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def require(test,message):
    if not test: raise ValueError(message)


def number(value,name,low,high,integer=False):
    require(type(value) in ((int,) if integer else (int,float)) and math.isfinite(value) and low<=value<=high,
            f'{name} must be {"an integer" if integer else "finite"} in [{low}, {high}]')
    return value


def label(value,name):
    require(isinstance(value,str) and 0<len(value)<=500 and value.isprintable() and not any(c in value for c in '\n\r\x00;'),f'Invalid {name}')
    return value


def asset(root,relative):
    require(isinstance(relative,str) and relative and not Path(relative).is_absolute() and '\\' not in relative
            and '..' not in Path(relative).parts and '\x00' not in relative,'Asset paths must be relative without traversal')
    path=root/relative
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),f'Missing or unsafe asset: {relative}')
    require(all(not part.is_symlink() for part in [path,*path.parents] if part!=root.parent),'Asset path contains a symlink')
    return path


def topology_includes(path,root,force_field,seen=None):
    seen=set() if seen is None else seen
    path=path.resolve()
    if path in seen: return
    seen.add(path)
    require(path.is_relative_to(root.resolve()),'Topology includes leave the admitted asset directory')
    for line in path.read_text().splitlines():
        if re.match(r'^\s*#\s*include\b',line):
            match=re.match(r'^\s*#\s*include\s+"([^"\x00]+)"\s*(?:;.*)?$',line)
            require(match is not None,'Topology includes must use literal quoted paths')
            name=match[1]; relative=Path(name)
            require(not relative.is_absolute() and '\\' not in name,'Absolute topology includes are not admitted')
            local=path.parent/relative
            if local.exists():
                require(not local.is_symlink() and local.resolve().is_relative_to(root.resolve()),'Topology include escapes admitted assets')
                topology_includes(local,root,force_field,seen)
            else:
                require('..' not in relative.parts and len(relative.parts)>1 and relative.parts[0]==force_field+'.ff',
                        f'Unresolved topology include is not the declared pinned force field: {name}')


def validate_request(request,assets_dir):
    """Return a normalized request with immutable input hashes; never launch MD."""
    require(isinstance(request,dict) and request.get('schema')==SCHEMA,'Unsupported MD request schema')
    value=copy.deepcopy(request); root=Path(assets_dir)
    for field in ('comparison_id','model_id'):
        if field in value: label(value[field],field)
    require(value.get('protocol') in PROTOCOLS,'Unsupported molecular-dynamics protocol')
    conditions=value.get('conditions',{}); sim=value.get('simulation',{})
    require(isinstance(conditions,dict) and isinstance(sim,dict),'Conditions and simulation settings must be objects')
    for key in ('force_field','water_model'):
        require(isinstance(conditions.get(key),str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}',conditions[key]),f'Explicit {key} is required')
    number(conditions.get('temperature_kelvin'),'temperature_kelvin',250,400)
    number(conditions.get('pressure_bar'),'pressure_bar',0.1,100)
    number(conditions.get('ionic_strength_molar'),'ionic_strength_molar',0,3)
    protonation=conditions.get('protonation',{})
    require(isinstance(protonation,dict) and protonation.get('method') in {'explicit','reviewed_preparation'},'Explicit reviewed protonation is required')
    number(protonation.get('pH'),'protonation.pH',0,14); label(protonation.get('description'),'protonation description')
    require(conditions.get('prepared_solvated_ionized') is True,'Prepared coordinates/topologies must already contain the declared solvent, ions and protonation')
    nb=conditions.get('nonbonded',{})
    require(isinstance(nb,dict) and nb.get('coulombtype')=='PME','Explicit PME nonbonded settings are required')
    for key in ('rcoulomb_nm','rvdw_nm'): number(nb.get(key),key,0.5,2)
    require(nb.get('vdw_modifier') in {'Potential-shift','Force-switch'},'Choose the nonbonded modifier specified by the force field')
    require(nb.get('dispersion_correction') in {'no','EnerPres'},'Explicit dispersion correction is required')
    if nb['vdw_modifier']=='Force-switch': number(nb.get('rvdw_switch_nm'),'rvdw_switch_nm',0,nb['rvdw_nm']-0.001)
    number(sim.get('dt_ps'),'dt_ps',0.0001,0.002)
    for key in ('minimization_steps','nvt_steps','npt_steps','production_steps'): number(sim.get(key),key,1,10**9,True)
    number(sim.get('output_stride'),'output_stride',1,100000,True)
    number(sim.get('seed'),'seed',1,2**31-1000000,True)
    number(sim.setdefault('threads',4),'threads',1,256,True)
    systems=value.get('systems'); require(isinstance(systems,dict),'Prepared molecular systems are required')
    expected={'complex'} if value['protocol']=='plumed_metadynamics' else {'bound','unbound'}
    require(set(systems)==expected,f'This protocol requires exactly {sorted(expected)} systems')
    for name,system in systems.items():
        require(isinstance(system,dict),f'Invalid {name} system')
        path=asset(root,system.get('coordinates')); require(path.suffix.lower() in {'.gro','.pdb'},'Prepared coordinates must be GRO or PDB')
        top=asset(root,system.get('topology')); require(top.suffix.lower()=='.top','Prepared GROMACS TOP topology is required')
        topology_includes(top,root,conditions['force_field'])
        manifests=system.get('parameter_manifests',[])
        require(isinstance(manifests,list) and len(manifests)<=100,'parameter_manifests must be a list of at most 100 admitted JSON files')
        if system.get('parameter_manifest'): manifests=[system['parameter_manifest'],*manifests]
        for manifest in manifests: asset(root,manifest)
        system['parameter_manifests']=list(dict.fromkeys(manifests))
        if system.get('reference_pdb'): asset(root,system['reference_pdb'])
    if value['protocol']=='pmx_binding_ddg':
        pmx=value.get('pmx',{}); mutation=pmx.get('mutation',{})
        require(pmx.get('topology_mode') in {'prepared_hybrid','generate'},'pmx topology_mode must be prepared_hybrid or generate')
        require(mutation.get('from') in AMINO and mutation.get('to') in AMINO and mutation['from']!=mutation['to'],
                'Only explicitly specified canonical protein substitutions are admitted by this pmx workflow')
        require(isinstance(mutation.get('chain'),str) and re.fullmatch('[A-Za-z0-9]',mutation['chain']),'Mutation must identify a chain')
        number(mutation.get('resid'),'mutation.resid',1,99999,True)
        require(pmx.get('charge_change_e')==0 and type(pmx.get('charge_change_e')) in (int,float),
                'Charge-changing transformations require a validated correction protocol and are not admitted')
        charges={'ARG':1,'LYS':1,'ASP':-1,'GLU':-1}
        require(charges.get(mutation['from'],0)==charges.get(mutation['to'],0),'This mutation changes formal charge; no correction protocol is implemented')
        schedule=pmx.get('lambda_schedule')
        require(isinstance(schedule,list) and 3<=len(schedule)<=101,'Provide 3–101 strictly increasing lambda states including both endpoints')
        for entry in schedule: number(entry,'lambda',0,1)
        require(schedule[0]==0 and schedule[-1]==1 and all(a<b for a,b in zip(schedule,schedule[1:])),'Lambda schedule must increase from exactly 0 to exactly 1')
        number(pmx.get('replicas'),'replicas',2,100,True)
        if pmx['topology_mode']=='generate':
            require(conditions['force_field'].endswith('-mut'),'pmx generation requires a declared installed mutation force field')
            for system in systems.values():
                require(system.get('reference_pdb'),'pmx generation requires a PDB retaining chain/residue identity')
                require(system.get('pdb2gmx_protonation_confirmed') is True,'Confirm the declared force-field protonation before pmx pdb2gmx generation')
    elif value['protocol']=='plumed_metadynamics':
        enhanced=value.get('enhanced',{}); cvs=enhanced.get('cvs')
        require(isinstance(cvs,list) and 1<=len(cvs)<=2,'Specify one or two supported collective variables')
        names=set()
        for cv in cvs:
            require(isinstance(cv,dict) and re.fullmatch('[a-z][a-z0-9_]{0,24}',str(cv.get('name',''))),'Invalid CV name')
            require(cv['name'] not in names and cv['name'] not in {'metad','weights','hist','fes'},'Duplicate or reserved CV name'); names.add(cv['name'])
            require(cv.get('type') in {'distance','torsion'},'Only explicit distance/torsion CVs are supported; arbitrary PLUMED directives are not admitted')
            atoms=cv.get('atoms'); require(isinstance(atoms,list) and len(atoms)==(2 if cv['type']=='distance' else 4),'Wrong CV atom count')
            for atom in atoms: number(atom,'CV atom index',1,10000000,True)
            require(len(set(atoms))==len(atoms),'CV atom indices must be distinct')
            number(cv.get('sigma'),'CV sigma',0.0001,10)
            number(cv.get('grid_min'),'CV grid minimum',-1000,1000); number(cv.get('grid_max'),'CV grid maximum',-1000,1000)
            require(cv['grid_min']<cv['grid_max'],'CV grid range must increase')
            number(cv.setdefault('grid_bins',200),'CV grid bins',10,1000,True)
            if cv['type']=='torsion': require(abs(cv['grid_min']+math.pi)<1e-5 and abs(cv['grid_max']-math.pi)<1e-5,'Periodic torsions require the full -pi to pi grid')
            else: require(cv['grid_min']>=0,'Distance grids cannot be negative')
        number(enhanced.get('biasfactor'),'biasfactor',1.01,100)
        number(enhanced.get('height_kj_mol'),'height_kj_mol',0.001,20)
        number(enhanced.get('pace_steps'),'pace_steps',1,1000000,True)
        number(enhanced.setdefault('discard_ps',0),'discard_ps',0,sim['production_steps']*sim['dt_ps'])
        require(enhanced.get('whole_molecules'),'Specify whole_molecules atom ranges so periodic coordinates are reconstructed before evaluating CVs')
        for molecule in enhanced['whole_molecules']:
            require(isinstance(molecule,list) and len(molecule)==2,'Each whole molecule needs first/last atom indices')
            number(molecule[0],'first atom',1,10000000,True); number(molecule[1],'last atom',molecule[0],10000000,True)
    else:
        bfee=value.get('bfee3',{})
        for key in ('receptor_selection','ligand_selection','solvent_selection'): label(bfee.get(key),key)
        if 'unbound_ligand_selection' in bfee: label(bfee['unbound_ligand_selection'],'unbound_ligand_selection')
        number(bfee.get('bulk_distance_nm'),'bulk_distance_nm',0.5,100)
        number(bfee.get('periodic_margin_nm'),'periodic_margin_nm',1,100)
        require(bfee.get('prepared_box_supports_bulk_separation') is True,'BFEE3 requires an already solvated/ionized box large enough for the declared bulk separation')
        require(bfee.get('restraint_convention')=='upstream_geometric_v1','Acknowledge the upstream BFEE3 geometric restraint convention')
    files={}
    require(root.is_dir() and not root.is_symlink(),'Invalid admitted asset directory')
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(),'Symlinks are not admitted in molecular assets')
        if path.is_file(): files[path.relative_to(root).as_posix()]={'sha256':sha(path),'bytes':path.stat().st_size}
        else: require(path.is_dir(),'Special files are not admitted in molecular assets')
    require(files and len(files)<=20000,'Invalid asset inventory size')
    if '_asset_files' in value: require(value['_asset_files']==files,'Prepared molecular input bytes changed')
    value['_asset_files']=files
    return value


def scientific_report(request):
    return {'protocol':request['protocol'],'conditions':request['conditions'],'claims':{
        'pmx_binding_ddg':'Relative mutation binding free energy: ΔΔG_bind = ΔG_bound(A→B) − ΔG_unbound(A→B). Positive values weaken binding under the chosen model.',
        'plumed_metadynamics':'A reweighted free-energy surface over selected CVs; not a binding affinity or proof of function.',
        'bfee3_geometric':'BFEE3 geometric standard-state binding free energy, conditional on converged restraint and separation PMFs.'}[request['protocol']],
        'limitations':['Force fields and protonation are hypotheses; agreement between models is not an accuracy guarantee.',
                       'Elapsed simulation time alone does not establish convergence.',
                       'No custom-residue parameters, missing atoms or chemical linkages are invented.',
                       'Charge-changing alchemical mutations are rejected without a validated correction protocol.'],
        'sources':SOURCE_URLS}


def _write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text)


def _relative(cwd,path):
    return os.path.relpath(path,cwd).replace(os.sep,'/')


def _mdp(request,phase,seed,*,state=None):
    c=request['conditions']; s=request['simulation']; nb=c['nonbonded']
    md={'integrator':'steep' if phase=='min' else 'md','nsteps':s[{'min':'minimization_steps','nvt':'nvt_steps','npt':'npt_steps','production':'production_steps'}[phase]],
        'dt':s['dt_ps'],'pbc':'xyz','cutoff-scheme':'Verlet','coulombtype':'PME','rcoulomb':nb['rcoulomb_nm'],
        'vdwtype':'Cut-off','vdw-modifier':nb['vdw_modifier'],'rvdw':nb['rvdw_nm'],'DispCorr':nb['dispersion_correction'],
        'constraints':'h-bonds','constraint-algorithm':'lincs','nstlog':s['output_stride'],'nstenergy':s['output_stride'],
        'nstxout-compressed':s['output_stride'],'nstcalcenergy':1}
    if nb['vdw_modifier']=='Force-switch': md['rvdw-switch']=nb['rvdw_switch_nm']
    if phase=='min': md.update(emtol=100,emstep=0.01)
    else:
        md.update({'tcoupl':'v-rescale','tc-grps':'System','tau-t':1.0,'ref-t':c['temperature_kelvin'],
                   'gen-vel':'yes' if phase=='nvt' else 'no','continuation':'no' if phase=='nvt' else 'yes',
                   'ld-seed':seed})
        if phase=='nvt': md.update({'gen-temp':c['temperature_kelvin'],'gen-seed':seed,'pcoupl':'no'})
        else: md.update({'pcoupl':'C-rescale','pcoupltype':'isotropic','tau-p':5.0,'ref-p':c['pressure_bar'],'compressibility':4.5e-5})
    if state is not None:
        md.update({'free-energy':'yes','init-lambda-state':state,'fep-lambdas':' '.join(map(str,request['pmx']['lambda_schedule'])),
                   'calc-lambda-neighbors':-1,'sc-alpha':0.5,'sc-power':1,'sc-sigma':0.3,'nstdhdl':s['output_stride'],
                   'dhdl-derivatives':'yes','dhdl-print-energy':'potential','separate-dhdl-file':'yes'})
    return ''.join(f'{key} = {item}\n' for key,item in md.items())


def prepare(request,assets_dir,work_dir):
    request=validate_request(request,assets_dir); root=Path(work_dir)
    root.mkdir(parents=True,exist_ok=True)
    require(not any(root.iterdir()),'Protocol work directory must be empty; resume the existing plan instead of regenerating it')
    for relative,metadata in request['_asset_files'].items():
        destination=root/'inputs'/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(Path(assets_dir)/relative,destination)
        require(sha(destination)==metadata['sha256'],'Staged molecular asset digest differs')
    fingerprint=hashlib.sha256(canonical(request)).hexdigest()
    _write(root/'request.json',json.dumps(request,indent=2)+'\n')
    stages=[]; analysis=[]
    def stage(identity,argv,cwd='.',deps=(),inputs=(),outputs=(),checkpoint=None,stdin=None):
        entry={'id':identity,'argv':argv,'cwd':cwd,'dependencies':list(deps),'inputs':list(dict.fromkeys(['request.json',*inputs])),'outputs':list(outputs)}
        if checkpoint: entry['checkpoint']=checkpoint
        if stdin is not None: entry['stdin']=stdin
        stages.append(entry); return identity
    def check(identity,cwd,top,system,deps,charge=False):
        argv=['python','-m','md.protocols','check-topology','--topology',_relative(cwd,top),'--out',identity+'.json',
              '--force-field',request['conditions']['force_field'],'--water-model',request['conditions']['water_model']]
        if charge: argv+=['--neutral-transformation']
        if request['protocol']=='plumed_metadynamics': argv+=['--cv-request',_relative(cwd,'request.json')]
        for manifest in system.get('parameter_manifests',[]):
            argv+=['--manifest',_relative(cwd,'inputs/'+manifest)]
        argv+=['--base-dir',_relative(cwd,'inputs')]
        return stage(identity,argv,cwd,deps,[top],[cwd+'/'+identity+'.json'])
    def dynamics(prefix,system,seed,*,state=None,deps=(),topology=None,coordinates=None,plumed=False,phases=('min','nvt','npt','production')):
        top=topology or 'inputs/'+system['topology']; coord=coordinates or 'inputs/'+system['coordinates']; previous=list(deps); checkpoint=None
        for phase in phases:
            cwd=prefix+'/'+phase; _write(root/cwd/'run.mdp',_mdp(request,phase,seed,state=state))
            argv=['gmx','grompp','-f','run.mdp','-c',_relative(cwd,coord),'-p',_relative(cwd,top),'-o','run.tpr','-pp','processed.top']
            if checkpoint and phase not in {'min','nvt'}: argv+=['-t',_relative(cwd,checkpoint)]
            prep=stage(prefix.replace('/','_')+'_'+phase+'_grompp',argv,cwd,previous,[coord,top,cwd+'/run.mdp'],[cwd+'/run.tpr',cwd+'/processed.top'])
            valid=check(prefix.replace('/','_')+'_'+phase+'_topology',cwd,cwd+'/processed.top',system,[prep],state is not None)
            argv=['gmx','mdrun','-s','run.tpr','-deffnm','run','-ntmpi','1','-ntomp',str(request['simulation']['threads'])]
            outputs=[cwd+'/run.gro',cwd+'/run.log',cwd+'/run.edr']
            if phase!='min': outputs+=[cwd+'/run.cpt',cwd+'/run.xtc']
            if state is not None and phase=='production': argv+=['-dhdl','dhdl.xvg']; outputs+=[cwd+'/dhdl.xvg']
            if plumed and phase=='production':
                _write(root/cwd/'plumed.dat',plumed_input(request)); argv+=['-plumed','plumed.dat']; outputs+=[cwd+'/HILLS',cwd+'/COLVAR']
            done=stage(prefix.replace('/','_')+'_'+phase,argv,cwd,[valid],[cwd+'/run.tpr']+([cwd+'/plumed.dat'] if plumed and phase=='production' else []),outputs,None if phase=='min' else cwd+'/run.cpt')
            if plumed and phase=='production': stages[-1]['restart_files']=[cwd+'/HILLS',cwd+'/COLVAR']
            coord=cwd+'/run.gro'; checkpoint=cwd+'/run.cpt' if phase!='min' else None; previous=[done]
        final=prefix+'/final.pdb'
        stage(prefix.replace('/','_')+'_structure',['gmx','editconf','-f',_relative(prefix,coord),'-o','final.pdb'],prefix,previous,[coord],[final])
        return previous[0],prefix+'/production/dhdl.xvg',final
    if request['protocol']=='pmx_binding_ddg':
        for leg_index,(leg,system) in enumerate(sorted(request['systems'].items())):
            top='inputs/'+system['topology']; coord='inputs/'+system['coordinates']; deps=[]
            if request['pmx']['topology_mode']=='generate':
                cwd=leg+'/hybrid'; mutation=request['pmx']['mutation']; _write(root/cwd/'mutation.txt',f"{mutation['chain']} {mutation['resid']} {AMINO[mutation['to']]}\n")
                first=stage(leg+'_mutate',['pmx','mutate','-f',_relative(cwd,'inputs/'+system['reference_pdb']),'-o','mutant.pdb',
                    '-ff',request['conditions']['force_field'],'--keep_resid','--script','mutation.txt'],cwd,[],['inputs/'+system['reference_pdb']],[cwd+'/mutant.pdb'])
                second=stage(leg+'_pdb2gmx',['gmx','pdb2gmx','-f','mutant.pdb','-o','hybrid.gro','-p','topol.top',
                    '-ff',request['conditions']['force_field'],'-water',request['conditions']['water_model']],cwd,[first],[cwd+'/mutant.pdb'],[cwd+'/hybrid.gro',cwd+'/topol.top'])
                normalized=stage(leg+'_portable_topology',['python','-m','md.protocols','portable-topology','--topology','topol.top','--force-field',request['conditions']['force_field'],'--out','portable.top'],cwd,[second],[cwd+'/topol.top'],[cwd+'/portable.top'])
                third=stage(leg+'_gentop',['pmx','gentop','-p','portable.top','-o','hybrid.top','-ff',request['conditions']['force_field']],cwd,[normalized],[cwd+'/portable.top'],[cwd+'/hybrid.top'])
                top=cwd+'/hybrid.top'; coord=cwd+'/hybrid.gro'; deps=[third]
            for replica in range(request['pmx']['replicas']):
                paths=[]
                for index,_ in enumerate(request['pmx']['lambda_schedule']):
                    prefix=f'{leg}/replica-{replica:03d}/lambda-{index:03d}'
                    _,path,_=dynamics(prefix,system,request['simulation']['seed']+leg_index*100000+replica*101+index,state=index,deps=deps,topology=top,coordinates=coord)
                    paths.append(path)
                analysis.append({'kind':'gromacs_u_nk','paths':paths,'protocol':{'comparison_id':request.get('comparison_id',fingerprint),'model_id':request.get('model_id',request['conditions']['force_field']),
                    'force_field':request['conditions']['force_field'],'water_model':request['conditions']['water_model'],
                    'temperature_kelvin':request['conditions']['temperature_kelvin'],'transformation':request['pmx']['mutation'],
                    'pressure_bar':request['conditions']['pressure_bar'],'ionic_strength_molar':request['conditions']['ionic_strength_molar'],
                    'protonation':request['conditions']['protonation'],'charge_change_e':0,
                    'parameter_manifest_sha256':{name:[request['_asset_files'][manifest]['sha256'] for manifest in entry['parameter_manifests']] for name,entry in request['systems'].items()},
                    'hamiltonian':{'nonbonded':request['conditions']['nonbonded'],'constraints':'h-bonds','lambda_schedule':request['pmx']['lambda_schedule'],
                                   'topology_parameter_sources':{path:metadata['sha256'] for path,metadata in request['_asset_files'].items() if Path(path).suffix.lower() in {'.top','.itp','.rtp','.atp','.hdb','.tdb','.prm'}},
                                   'force_field_distribution':'pmx-0dd5f0a9cdf26109eff98bdfeb4ac4e55353aa76;gromacs-2026.3'},
                    'leg':leg,'replica_id':str(replica),'replicate_id':str(replica),'sampling_method':'independent_windows','ensemble':'NPT',
                    'independent_replica':True,'independence_id':f'{fingerprint}:{leg}:{replica}','legs_independent':True,
                    'provenance':{'request_sha256':fingerprint,'source_files':request['_asset_files']}}})
    elif request['protocol']=='plumed_metadynamics':
        done,_,_=dynamics('enhanced',request['systems']['complex'],request['simulation']['seed'],plumed=True)
        stage('metadynamics_analysis',['python','-m','md.protocols','metadynamics-report','--colvar','COLVAR','--request','../../request.json','--out','sampling-report.json'],
              'enhanced/production',[done],['enhanced/production/COLVAR'],['enhanced/production/sampling-report.json','enhanced/production/reweighted-fes.dat'])
        analysis=[{'kind':'reweighted_cv_surface','path':'enhanced/production/reweighted-fes.dat','logweights':'enhanced/production/COLVAR',
                   'affinity_claim':False,'method':'well_tempered_metadynamics_REWEIGHT_METAD'}]
    else:
        _bfee_plan(request,root,stage,check,dynamics,analysis)
    if request['protocol']=='pmx_binding_ddg':
        stage('analyze_binding_ddg',['python','-m','md.protocols','analyze-plan','--plan','plan.json','--out','analysis/report.json'],
              '.', [entry['id'] for entry in stages], ['plan.json',*[path for item in analysis for path in item['paths']]], ['analysis/report.json'])
    for entry in stages:
        if entry['id']=='bfee_geometry': entry['head_preflight']=True
    plan={'schema':'bio-md-plan.v1','request_sha256':fingerprint,'protocol':request['protocol'],'stages':stages,'analysis_inputs':analysis,
          'scientific_report':scientific_report(request),'claims':scientific_report(request)['claims']}
    _write(root/'plan.json',json.dumps(plan,indent=2)+'\n')
    return plan


def plumed_input(request):
    enhanced=request['enhanced']; cvs=enhanced['cvs']; temperature=request['conditions']['temperature_kelvin']
    lines=['# Restart is coordinated with the GROMACS checkpoint; preserve HILLS and COLVAR.',
           'WHOLEMOLECULES '+' '.join(f'ENTITY{i}={pair[0]}-{pair[1]}' for i,pair in enumerate(enhanced['whole_molecules']))]
    for cv in cvs:
        lines.append(f"{cv['name']}: {'DISTANCE' if cv['type']=='distance' else 'TORSION'} ATOMS="+','.join(map(str,cv['atoms'])))
    fields={'ARG':','.join(cv['name'] for cv in cvs),'SIGMA':','.join(str(cv['sigma']) for cv in cvs),
            'GRID_MIN':','.join(str(cv['grid_min']) for cv in cvs),'GRID_MAX':','.join(str(cv['grid_max']) for cv in cvs),
            'GRID_BIN':','.join(str(cv['grid_bins']) for cv in cvs),'TEMP':temperature,'BIASFACTOR':enhanced['biasfactor'],
            'HEIGHT':enhanced['height_kj_mol'],'PACE':enhanced['pace_steps'],'FILE':'HILLS'}
    lines.append('metad: METAD '+' '.join(f'{key}={value}' for key,value in fields.items())+' CALC_RCT')
    # The canonical FES is computed once from every retained COLVAR sample after
    # production. An unused online DUMPGRID creates backups at every write and
    # eventually aborts long simulations at PLUMED's backup-count limit.
    lines+=['weights: REWEIGHT_METAD TEMP='+str(temperature),
            'PRINT ARG='+fields['ARG']+',metad.bias,metad.rbias,metad.rct,weights FILE=COLVAR STRIDE='+str(request['simulation']['output_stride'])]
    return '\n'.join(lines)+'\n'


def validate_cv_atom_count(request,atom_count):
    require(request.get('protocol')=='plumed_metadynamics','A CV preflight requires an enhanced-sampling request')
    number(atom_count,'prepared topology atom count',1,100000000,True)
    enhanced=request['enhanced']
    for cv in enhanced['cvs']:
        for atom in cv['atoms']:
            require(type(atom) is int and 1<=atom<=atom_count,'Collective-variable atom index exceeds the actual prepared topology atom count')
    for first,last in enhanced['whole_molecules']:
        require(type(first) is int and type(last) is int and 1<=first<=last<=atom_count,
                'Whole-molecule range exceeds the actual prepared topology atom count')
    return {'schema':'bio-md-cv-admission.v1','prepared_atom_count':atom_count,'collective_variables':len(enhanced['cvs']),
            'whole_molecule_ranges':len(enhanced['whole_molecules']),'within_actual_topology':True}


def topology_charge_report(path,neutral=False):
    section=''; molecule=None; types={}; counts={}
    for raw in Path(path).read_text().splitlines():
        line=raw.split(';',1)[0].strip()
        if not line: continue
        require(not line.startswith('#'),'Topology must be preprocessed by grompp before charge validation')
        match=re.fullmatch(r'\[\s*([A-Za-z_]+)\s*\]',line)
        if match: section=match[1].lower(); continue
        fields=line.split()
        if section=='moleculetype': molecule=fields[0]; types[molecule]={'a':0.0,'b':0.0,'atoms':0,'hybrid_atoms':0}; section=''
        elif section=='atoms':
            require(molecule is not None and len(fields)>=7,'Malformed processed topology atom')
            a=float(fields[6]); b=float(fields[9]) if len(fields)>=11 else a
            require(math.isfinite(a) and math.isfinite(b),'Nonfinite topology charge')
            types[molecule]['a']+=a; types[molecule]['b']+=b; types[molecule]['atoms']+=1
            types[molecule]['hybrid_atoms']+=int(len(fields)>=11 and (fields[1]!=fields[8] or abs(a-b)>1e-8 or fields[7]!=fields[10]))
        elif section=='molecules':
            require(len(fields)==2 and fields[0] in types,'Unknown molecule in processed topology')
            counts[fields[0]]=counts.get(fields[0],0)+int(fields[1])
    require(counts and all(n>=0 for n in counts.values()),'Processed topology lacks molecule counts')
    a=sum(types[name]['a']*count for name,count in counts.items()); b=sum(types[name]['b']*count for name,count in counts.items())
    hybrid=sum(types[name]['hybrid_atoms']*count for name,count in counts.items())
    if neutral:
        require(hybrid>0,'Prepared alchemical topology has no hybrid A/B atom types')
        require(abs(b-a)<1e-4,'Actual hybrid topology changes total charge; no charge correction protocol is implemented')
    return {'schema':'bio-md-topology-check.v1','topology_sha256':sha(path),'charge_a_e':a,'charge_b_e':b,'charge_change_e':b-a,
            'hybrid_atoms':hybrid,'atom_count':sum(types[name]['atoms']*n for name,n in counts.items()),'neutral_transformation_required':neutral}


def _bfee_plan(request,root,stage,check,dynamics,analysis):
    admissions=[]
    for leg,system in sorted(request['systems'].items()):
        cwd='bfee/preflight-'+leg; _write(root/cwd/'run.mdp',_mdp(request,'min',request['simulation']['seed']))
        top='inputs/'+system['topology']; coord='inputs/'+system['coordinates']
        grompp=stage('bfee_preflight_'+leg+'_grompp',['gmx','grompp','-f','run.mdp','-c',_relative(cwd,coord),'-p',_relative(cwd,top),'-o','run.tpr','-pp','processed.top'],
            cwd,[],[coord,top,cwd+'/run.mdp'],[cwd+'/run.tpr',cwd+'/processed.top'])
        admissions.append(check('bfee_preflight_'+leg+'_topology',cwd,cwd+'/processed.top',system,[grompp]))
    geometry=stage('bfee_geometry',['python','-m','md.protocols','bfee-geometry','--request','../request.json','--assets','../inputs',
        '--bound-topology','preflight-bound/processed.top','--out','geometry.json'],'bfee',admissions,
        ['bfee/preflight-bound/processed.top'],['bfee/geometry.json'])
    # Selected by the head's fixed native preflight; no dependency performs dynamics.
    dependencies=[]
    for index,(leg,system) in enumerate(sorted(request['systems'].items())):
        done,_,_=dynamics('bfee/prepare-'+leg,system,request['simulation']['seed']+index*100000,
                          deps=[geometry],phases=('min','nvt','npt'))
        dependencies.append(done)
    generation_outputs=['bfee/generation.json','bfee/BFEE/Protein/bound.top','bfee/BFEE/Protein/bound.gro',
                        'bfee/BFEE/Ligand/unbound.top','bfee/BFEE/Ligand/unbound.gro']
    for index,name in enumerate(BFEE_STEPS):
        folder='bfee/BFEE/'+name+'/'
        generation_outputs += [folder+name for name in (['colvars_ligand_only.ndx','reference_ligand_only.xyz'] if index==8 else ['colvars.ndx','reference.xyz'])]
        generation_outputs += [folder+f'{index:03d}_colvars.dat',folder+f'{index:03d}_{"eq" if index==0 else "PMF"}.mdp']
        if index in (7,8): generation_outputs += [folder+f'{index:03d}_eq_colvars.dat',folder+f'{index:03d}_Equilibration.mdp']
    generated=stage('bfee_generate',['python','-m','md.protocols','bfee-generate','--request','../request.json',
                    '--work','..','--out','generation.json'],'bfee',dependencies,
                    [f'bfee/prepare-{leg}/npt/{name}' for leg in ('bound','unbound') for name in ('run.gro','processed.top')],generation_outputs)
    preceding=generated; pmfs=[]; bias_runs=[]
    for index,name in enumerate(BFEE_STEPS):
        cwd='bfee/BFEE/'+name; system=request['systems']['unbound' if index==8 else 'bound']
        top='bfee/BFEE/Ligand/unbound.top' if index==8 else 'bfee/BFEE/Protein/bound.top'
        coord='bfee/BFEE/Ligand/unbound.gro' if index==8 else 'bfee/BFEE/Protein/bound.gro'
        if index>0 and index!=8: coord='bfee/BFEE/000_eq/output/000_eq.out.gro'
        variants=[('eq',f'{index:03d}_eq_colvars.dat'),('pmf',f'{index:03d}_colvars.dat')] if index in (7,8) else [('eq' if index==0 else 'pmf',f'{index:03d}_colvars.dat')]
        center_inputs=[f'bfee/BFEE/{BFEE_STEPS[prior]}/output/{BFEE_STEPS[prior]}.out.abf1.{suffix}'
                       for prior in range(2,min(index,7)) for suffix in ('czar.pmf','zcount')] if index!=8 else []
        for variant,colvars in variants:
            basename=name+('_eq' if variant=='eq' and index else '')
            centers=stage('bfee_'+basename+'_centers',['python','-m','md.protocols','bfee-centers','--config',colvars,
                '--bfee-root','..','--prepared',variant+'-ready.dat','--out',variant+'-centers.json'],cwd,[preceding],[cwd+'/'+colvars,*center_inputs],[cwd+'/'+variant+'-centers.json',cwd+'/'+variant+'-ready.dat'])
            mdp=f'{index:03d}_{"eq" if index==0 else "Equilibration" if variant=="eq" else "PMF"}.mdp'
            prep=stage('bfee_'+basename+'_grompp',['gmx','grompp','-f',mdp,'-c',_relative(cwd,coord),'-p',_relative(cwd,top),
                       '-o',variant+'.tpr','-pp',variant+'-processed.top'],cwd,[centers],[cwd+'/'+mdp,cwd+'/'+variant+'-ready.dat',coord,top],[cwd+'/'+variant+'.tpr',cwd+'/'+variant+'-processed.top'])
            valid=check('bfee_'+basename+'_topology',cwd,cwd+'/'+variant+'-processed.top',system,[prep])
            deffnm='output/'+basename+'.out'
            outputs=[cwd+'/'+deffnm+suffix for suffix in ('.gro','.cpt','.edr','.log','.xtc')]
            if variant=='pmf':
                pmf=cwd+'/'+deffnm+'.abf1.czar.pmf'; outputs += [pmf,str(pmf).removesuffix('.czar.pmf')+'.zcount']; pmfs.append(pmf)
            preceding=stage('bfee_'+basename,['gmx','mdrun','-s',variant+'.tpr','-deffnm',deffnm,'-ntmpi','1','-ntomp',str(request['simulation']['threads'])],
                cwd,[valid],[cwd+'/'+variant+'.tpr'],outputs,cwd+'/'+deffnm+'.cpt')
            bias_runs.append(preceding); coord=cwd+'/'+deffnm+'.gro'
        stage('bfee_'+name+'_structure',['gmx','editconf','-f',_relative(cwd,coord),'-o','final.pdb'],cwd,[preceding],[coord],[cwd+'/final.pdb'])
    stage('bfee_affinity_analysis',['python','-m','md.protocols','bfee-analyze','--request','../request.json','--bfee-root','BFEE','--out','affinity-report.json'],
          'bfee',bias_runs,[*pmfs,*[path.removesuffix('.czar.pmf')+'.zcount' for path in pmfs]],['bfee/affinity-report.json'])
    analysis.append({'kind':'bfee3_geometric','paths':pmfs,'report':'bfee/affinity-report.json','convergence_established':False})


def bfee_geometry(request,assets_dir,bound_topology):
    """Read-only native BFEE selection/periodic-box admission before dynamics."""
    import MDAnalysis as mda
    import numpy as np
    assets=Path(assets_dir); params=request['bfee3']
    bound=mda.Universe(str(assets/request['systems']['bound']['coordinates']))
    unbound=mda.Universe(str(assets/request['systems']['unbound']['coordinates']))
    receptor=bound.select_atoms('('+params['receptor_selection']+') and not (name H*)')
    ligand=bound.select_atoms('('+params['ligand_selection']+') and not (name H*)')
    free=unbound.select_atoms('('+params.get('unbound_ligand_selection',params['ligand_selection'])+') and not (name H*)')
    solvent=bound.select_atoms(params['solvent_selection'])
    require(min(len(receptor),len(ligand),len(free))>=3 and len(solvent)>0,'BFEE3 requires nonempty partner heavy atoms and solvent')
    require(not set(receptor.indices)&set(ligand.indices),'BFEE3 partner selections overlap')
    require(not set(solvent.indices)&(set(receptor.indices)|set(ligand.indices)),'BFEE3 solvent selection overlaps partners')
    require(list(zip(ligand.resnames,ligand.names))==list(zip(free.resnames,free.names)),'BFEE3 bound/unbound ligand names, residue identities and atom order differ')
    membership=topology_molecule_membership(bound_topology)
    require(len(membership)==len(bound.atoms),'BFEE3 topology/coordinates atom counts differ')
    require(not {membership[int(i)] for i in receptor.indices}&{membership[int(i)] for i in ligand.indices},
            'BFEE3 partners must occupy separate topology molecular instances')
    box=bound.dimensions
    require(box is not None and np.all(np.isfinite(box)) and np.allclose(box[3:],90,atol=.01),'BFEE3 requires a prepared orthorhombic box')
    centers=[]; radii=[]
    for group in (receptor,ligand):
        centered=group.positions-group.positions.mean(axis=0)
        require(np.linalg.matrix_rank(centered)>=2,'BFEE3 requires noncollinear orientational anchors')
        centers.append(group.positions.mean(axis=0)/10); radii.append(float(np.max(np.linalg.norm(centered,axis=1)))/10)
    distance=float(np.linalg.norm(centers[1]-centers[0])); bulk=params['bulk_distance_nm']
    require(bulk>distance+.2,'BFEE3 bulk separation must exceed the initial distance by at least 0.2 nm')
    required=bulk+.02+sum(radii)+params['periodic_margin_nm']
    require(float(min(box[:3]))/20>required,'Prepared BFEE3 box is too small for the bulk range, partner sizes and periodic margin')
    return {'schema':'bio-md-bfee-geometry.v1','molecular_dynamics_executed':False,'initial_distance_nm':distance,
            'bulk_distance_nm':bulk,'partner_radii_nm':radii,'required_half_box_nm':required,'prepared_box_nm':(box[:3]/10).tolist(),
            'partner_heavy_atoms':[len(receptor),len(ligand)],'bound_topology_sha256':sha(bound_topology),
            'limitations':['This validates the geometric setup; it does not establish a sampled bulk plateau or converged affinity.']}


def bfee_generate(request,work):
    """Run the pinned upstream generator, preserving the admitted coordinates/box.

    BFEEGromacs.__init__ estimates a new cell even when one exists. Restore the
    supplied physical cell and positions before generating its restraints.
    No generated shell scripts, extra solvation, or topology rebuilding run.
    """
    import numpy as np
    import MDAnalysis as mda
    from BFEE2.templates_gromacs.BFEEGromacs import BFEEGromacs
    work=Path(work).resolve(); directory=work/'bfee'/'BFEE'
    require(not directory.exists(),'BFEE3 generation directory already exists; use the sealed stage/checkpoint rather than overwrite it')
    params=request['bfee3']; prepared=work/'bfee'/'prepared'; prepared.mkdir(exist_ok=True)
    universes={}; selections={}
    for leg in ('bound','unbound'):
        original=work/'inputs'/request['systems'][leg]['coordinates']
        identity=mda.Universe(str(original))
        final=mda.Universe(str(work/'bfee'/('prepare-'+leg)/'npt'/'run.gro'))
        require(len(identity.atoms)==len(final.atoms),'Equilibrated atom count no longer matches the selected input')
        require(list(identity.atoms.names)==list(final.atoms.names),'Atom order/names changed before BFEE3 selections')
        selection=params['ligand_selection'] if leg=='bound' else params.get('unbound_ligand_selection',params['ligand_selection'])
        atoms=identity.select_atoms('('+selection+') and not (name H*)')
        require(len(atoms)>=3,'BFEE3 needs at least three selected ligand heavy atoms')
        selections[leg]=list(map(int,atoms.indices)); universes[leg]=final
        final.atoms.write(str(prepared/(leg+'.gro')))
        shutil.copyfile(work/'bfee'/('prepare-'+leg)/'npt'/'processed.top',prepared/(leg+'.top'))
    bound_identity=mda.Universe(str(work/'inputs'/request['systems']['bound']['coordinates']))
    receptor=bound_identity.select_atoms('('+params['receptor_selection']+') and not (name H*)')
    solvent=bound_identity.select_atoms(params['solvent_selection'])
    require(len(receptor)>=3 and len(solvent)>0,'BFEE3 requires nonempty receptor heavy-atom and solvent selections')
    require(not set(receptor.indices)&set(selections['bound']),'BFEE3 receptor and ligand selections overlap')
    require(not set(solvent.indices)&(set(receptor.indices)|set(selections['bound'])),'Solvent selection overlaps binding partners')
    bound_ligand=universes['bound'].atoms[selections['bound']]; unbound_ligand=universes['unbound'].atoms[selections['unbound']]
    require(list(zip(bound_ligand.resnames,bound_ligand.names))==list(zip(unbound_ligand.resnames,unbound_ligand.names)),
            'Bound/unbound selected ligand atom order and residue identities must match exactly')
    membership=topology_molecule_membership(prepared/'bound.top')
    receptor_molecules={membership[int(i)] for i in receptor.indices}; ligand_molecules={membership[int(i)] for i in selections['bound']}
    require(not receptor_molecules&ligand_molecules,'BFEE3 partners must be separate topology molecular instances; covalently connected or merged partners are not admitted')
    for group in (universes['bound'].atoms[receptor.indices],bound_ligand):
        require(np.linalg.matrix_rank(group.positions-group.positions.mean(axis=0))>=2,'BFEE3 orientational restraints require noncollinear heavy-atom selections')
    box=universes['bound'].dimensions
    require(box is not None and np.all(np.isfinite(box)) and np.allclose(box[3:],90,atol=0.01),'BFEE3 currently requires an explicit orthorhombic prepared box')
    protein=universes['bound'].atoms[receptor.indices]
    centers=[group.positions.mean(axis=0)/10 for group in (protein,bound_ligand)]
    radii=[float(np.max(np.linalg.norm(group.positions/10-center,axis=1))) for group,center in zip((protein,bound_ligand),centers)]
    distance=float(np.linalg.norm(centers[1]-centers[0])); bulk=params['bulk_distance_nm']
    require(bulk>distance+0.2,'Bulk separation must exceed the initial partner distance by at least 0.2 nm')
    require(min(box[:3])/20>bulk+.02+sum(radii)+params['periodic_margin_nm'],
            'Prepared BFEE3 box is too small for the requested bulk distance, partner sizes and periodic-image margin')
    bfee=BFEEGromacs(str(prepared/'bound.gro'),str(prepared/'bound.top'),str(prepared/'unbound.gro'),str(prepared/'unbound.top'),
                     baseDirectory=str(directory),structureFormat='gro',ligandOnlyStructureFileFormat='gro')
    for leg,universe in universes.items():
        target=bfee.system if leg=='bound' else bfee.ligandOnlySystem
        target.atoms.positions=universe.atoms.positions.copy(); target.dimensions=universe.dimensions.copy()
        output=directory/('Protein' if leg=='bound' else 'Ligand')/(leg+'.gro')
        target.atoms.write(str(output))
        if leg=='bound': bfee.structureFile=str(output)
        else: bfee.ligandOnlyStructureFile=str(output)
    # Explicit index mapping also supports different chain/residue numbering in the unbound input.
    bfee.protein=bfee.system.atoms[receptor.indices]; bfee.ligand=bfee.system.atoms[selections['bound']]
    bfee.ligandOnly=bfee.ligandOnlySystem.atoms[selections['unbound']]; bfee.solvent=bfee.system.atoms[solvent.indices]
    bfee.setTemperature(request['conditions']['temperature_kelvin'])
    for index in range(9): getattr(bfee,f'generate{index:03d}')()
    files={}
    for index,name in enumerate(BFEE_STEPS):
        for path in (directory/name).glob('*.mdp'):
            if 'Minimize' in path.name: continue
            match=re.search(r'^colvars-configfile\s*=\s*(\S+)',path.read_text(),re.M)
            require(match is not None,'Pinned BFEE3 MDP has no Colvars configuration')
            mdp=_mdp(request,'production',request['simulation']['seed']+200000+index)
            # Fixed prepared volume during biased PMFs avoids changing the separation box.
            mdp=re.sub(r'^pcoupl\s*=.*$','pcoupl = no',mdp,flags=re.M)
            mdp=re.sub(r'^gen-vel\s*=.*$','gen-vel = yes',mdp,flags=re.M)
            mdp=re.sub(r'^continuation\s*=.*$','continuation = no',mdp,flags=re.M)
            if index==0 or 'Equilibration' in path.name:
                mdp=re.sub(r'^nsteps\s*=.*$',f"nsteps = {request['simulation']['nvt_steps']}",mdp,flags=re.M)
            mdp+=f"gen-temp = {request['conditions']['temperature_kelvin']}\ngen-seed = {request['simulation']['seed']+200000+index}\ncolvars-active = yes\ncolvars-configfile = {'eq' if index==0 or 'Equilibration' in path.name else 'pmf'}-ready.dat\n"
            path.write_text(mdp)
        for path in (directory/name).glob('*colvars.dat'):
            text=path.read_text()
            text=re.sub(r'^(colvarsTrajFrequency|colvarsRestartFrequency)\s+\d+',lambda m:m[1]+' '+str(request['simulation']['output_stride']),text,flags=re.M)
            if path.name=='007_colvars.dat':
                for field,value in [('upperboundary',math.ceil((bulk+.02)*100)/100),('upperWalls',math.ceil((bulk+.02)*100)/100),('lowerboundary',max(.01,math.floor((distance-.2)*100)/100)),('lowerWalls',max(.01,math.floor((distance-.2)*100)/100))]:
                    text=re.sub(r'^(\s*'+field+r'\s+)\S+',lambda m:m[1]+str(value),text,flags=re.M|re.I)
            path.write_text(text)
        for path in (directory/name).glob('*'):
            if path.is_file(): files[path.relative_to(directory).as_posix()]=sha(path)
    return {'schema':'bio-md-bfee-generation.v1','generator':'BFEE2.templates_gromacs.BFEEGromacs',
            'upstream_commit':'8b3e33e39b74bf1a7b58167df92af7f5f5795180','files':files,'prepared_box_preserved':True,
            'solvent_or_ion_additions':0,'sampling_ensemble':'NVT after NPT preparation','bound_box_nm':(box[:3]/10).tolist(),
            'initial_distance_nm':distance,'bulk_distance_nm':bulk,'partner_radii_nm':radii,
            'selected_heavy_atoms':{'receptor':len(receptor),'bound_ligand':len(bound_ligand),'unbound_ligand':len(unbound_ligand)}}


def read_pmf(path):
    points=[]
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        fields=line.split(); require(len(fields)==2,'Expected a one-dimensional BFEE3 PMF')
        point=tuple(map(float,fields)); require(all(math.isfinite(x) for x in point),'BFEE3 PMF contains nonfinite values'); points.append(point)
    require(len(points)>=3 and all(a[0]<b[0] for a,b in zip(points,points[1:])),'BFEE3 PMF must have at least three increasing bins')
    return points


def bfee_count_coverage(pmf):
    """Use actual physical-CV CZAR counts, not zeros in unsampled PMF bins."""
    path=Path(str(pmf).removesuffix('.czar.pmf')+'.zcount')
    if not path.is_file(): return {'status':'missing_physical_cv_counts','count_path':str(path),'sufficient':False},[]
    points=read_pmf(path)
    require(all(y>=0 and float(y).is_integer() for _,y in points),'BFEE3 physical CV counts must be nonnegative integers')
    count=[int(y) for _,y in points]; threshold=10000
    report={'status':'coverage_available','count_sha256':sha(path),'bins':len(count),'sampled_bins':sum(x>0 for x in count),
            'minimum_count':min(count),'total_observations':sum(count),'bins_at_full_samples':sum(x>=threshold for x in count),
            'required_count_per_bin':threshold,'sufficient':all(x>=threshold for x in count),
            'threshold_note':'Conservative full-bin coverage gate; passing does not establish independent samples, convergence or a bulk plateau.'}
    return report,points


def bfee_centers(config,bfee_root,prepared):
    path=Path(config); text=path.read_text(); references={}
    for prefix in sorted(set(re.findall(r'(00[2-6])_pmf_argmin',text))):
        name=BFEE_STEPS[int(prefix)]; pmf=Path(bfee_root)/name/'output'/(name+'.out.abf1.czar.pmf')
        points=read_pmf(pmf); coverage,counts=bfee_count_coverage(pmf)
        require(coverage['sufficient'],'Insufficient BFEE3 physical-CV sampling to locate a restraint minimum: '+name)
        minimum=min(points,key=lambda point:point[1])[0]
        require(points[0][0]<minimum<points[-1][0],'BFEE3 PMF minimum lies at the sampling boundary; extend the sampled range')
        text=text.replace(prefix+'_pmf_argmin',format(minimum,'.12g')); references[prefix]={'center':minimum,'pmf_sha256':sha(pmf),'sampling':coverage}
    require('pmf_argmin' not in text and '$' not in text,'Unresolved BFEE3 template placeholder')
    # GROMACS embeds this configuration in the TPR; substitution must precede grompp.
    destination=Path(prepared)
    require(destination.resolve()!=path.resolve(),'Resolved Colvars configuration must not overwrite the sealed template')
    destination.write_text(text)
    return {'schema':'bio-md-restraint-centers.v1','config_sha256':sha(destination),'measured_centers':references}


def bfee_analyze(request,bfee_root):
    from BFEE2.postTreatment import postTreatment
    paths=[Path(bfee_root)/name/'output'/(name+'.out.abf1.czar.pmf') for name in BFEE_STEPS[1:]]
    points=[read_pmf(path) for path in paths]; bulk=request['bfee3']['bulk_distance_nm']
    # Last sampled bin can be half a bin below the hard upper boundary.
    require(points[6][-1][0]>=bulk,'Distance PMF does not reach the requested bulk separation')
    parameters=[4184,.4184,.4184,.4184,.4184,.4184,bulk,4184]
    values=list(map(float,postTreatment(request['conditions']['temperature_kelvin'],'gromacs').geometricBindingFreeEnergy(list(map(str,paths)),parameters)))
    require(len(values)==10 and all(math.isfinite(value) for value in values),'Upstream BFEE3 returned invalid contributions')
    coverage=[bfee_count_coverage(path)[0] for path in paths]
    supported=all(item['sufficient'] for item in coverage)
    return {'schema':'bio-md-bfee-affinity.v1','method':'BFEE3 geometric standard-state','upstream_commit':'8b3e33e39b74bf1a7b58167df92af7f5f5795180',
            'status':'conditional_estimate' if supported else 'insufficient_sampling',
            'estimate_kcal_mol':values[-1] if supported else None,'estimate_kj_mol':values[-1]*4.184 if supported else None,
            'raw_upstream_contributions_kcal_mol':values,'raw_upstream_total_is_affinity_estimate':supported,
            'input_restraint_parameters_gromacs':parameters,'pmf_sha256':[sha(path) for path in paths],'sampling_coverage':coverage,
            'temperature_kelvin':request['conditions']['temperature_kelvin'],'convergence_established':False,'standard_state':'1 mol/L (upstream correction)',
            'bulk_plateau_verified':False,'uncertainty_kj_mol':None,
            'limitations':['Raw upstream contributions can be numerical artifacts when bins are unsampled; they are retained for diagnosis, not reported as affinity.',
            'Even full count coverage does not establish a bulk plateau or converged independent sampling; inspect PMF histories and repeat independently.']}


def analyze_plan(plan_path):
    from .analysis import analyze_gromacs,binding_ddg,summarize_models
    path=Path(plan_path).resolve(); plan=json.loads(path.read_text()); legs=[]; by_replica={}
    for item in plan['analysis_inputs']:
        require(item['kind']=='gromacs_u_nk','Unsupported analysis input kind for binding cycle')
        report=analyze_gromacs([str(path.parent/name) for name in item['paths']],item['protocol'])
        legs.append(report); by_replica.setdefault(item['protocol']['replicate_id'],{})[item['protocol']['leg']]=report
    paired=[]; unavailable=[]
    for replica,values in by_replica.items():
        require(set(values)=={'bound','unbound'},f'Missing paired binding leg for replica {replica}')
        if any(value['mbar'].get('delta_g_kj_mol') is None for value in values.values()):
            unavailable.append({'replicate_id':replica,'reason':'insufficient_overlap_or_numerical_support','estimate_kj_mol':None})
        else: paired.append(binding_ddg(values['bound'],values['unbound']))
    return {'schema':'bio-md-cycle-report.v1','request_sha256':plan['request_sha256'],'legs':legs,'binding_ddg':paired,'unavailable_binding_ddg':unavailable,'models':summarize_models(paired) if paired else []}


def metadynamics_report(request,colvar,out):
    """Recompute the weighted FES from the complete retained trajectory on resume."""
    import numpy as np
    fields=None; values=[]; repeated_header=False; discarded_boundaries=0
    cv_names=[cv['name'] for cv in request['enhanced']['cvs']]
    for line in Path(colvar).read_text().splitlines():
        if line.startswith('#! FIELDS '):
            header=line.split()[2:]
            require(fields is None or fields==header,'COLVAR field layout changed during restart')
            repeated_header=fields is not None; fields=header
        elif line.strip() and not line.lstrip().startswith('#'):
            row=list(map(float,line.split())); require(fields and len(row)==len(fields) and all(math.isfinite(x) for x in row),'Malformed or nonfinite COLVAR sample')
            if repeated_header and values and row[fields.index('time')]==values[-1][fields.index('time')]:
                require(all(row[fields.index(name)]==values[-1][fields.index(name)] for name in cv_names),
                        'Repeated checkpoint time has different molecular CV coordinates')
                # Native PLUMED reprints the checkpoint configuration after loading its last hill.
                # Keep the original physical sample with its original bias/log weight, exactly once.
                discarded_boundaries+=1; repeated_header=False; continue
            repeated_header=False; values.append(row)
    require(len(values)>=2,'At least two retained CV samples are required')
    data=np.asarray(values); times=data[:,fields.index('time')]
    require(np.all(np.diff(times)>0),'CV sampling times overlap or decrease across restart; no automatic biased-data duplication')
    enhanced=request['enhanced']; cvs=enhanced['cvs']; discard=enhanced.get('discard_ps',0)
    keep=times>=discard; data=data[keep]; require(len(data)>=2,'Insufficient CV samples after requested equilibration discard')
    samples=np.column_stack([data[:,fields.index(cv['name'])] for cv in cvs]); logweights=data[:,fields.index('weights')]
    weights=np.exp(logweights-float(np.max(logweights)))
    ranges=[(cv['grid_min'],cv['grid_max']) for cv in cvs]; bins=[cv['grid_bins'] for cv in cvs]
    inside=np.ones(len(samples),dtype=bool)
    for index,(low,high) in enumerate(ranges): inside&=(samples[:,index]>=low)&(samples[:,index]<=high)
    require(inside.all(),'CV trajectory left the admitted FES grid; enlarge the grid rather than silently dropping samples')
    histogram,edges=np.histogramdd(samples,bins=bins,range=ranges,weights=weights)
    probability=histogram/histogram.sum(); observed=probability>0; rt=.00831446261815324*request['conditions']['temperature_kelvin']
    fes=np.full(histogram.shape,np.inf); fes[observed]=-rt*np.log(probability[observed]); fes[observed]-=float(np.min(fes[observed]))
    centers=[(edge[1:]+edge[:-1])/2 for edge in edges]; destination=Path(out).parent/'reweighted-fes.dat'; destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('w') as stream:
        stream.write('# complete retained COLVAR; binned probability; unsampled bins inf\n# '+' '.join(cv['name'] for cv in cvs)+' free_energy_kj_mol probability\n')
        for index in np.ndindex(histogram.shape):
            stream.write(' '.join([*(format(centers[axis][position],'.12g') for axis,position in enumerate(index)),format(fes[index],'.12g'),format(probability[index],'.12g')])+'\n')
    return {'schema':'bio-md-sampling-report.v1','samples':len(data),'discard_ps':discard,'colvar_sha256':sha(colvar),
            'restart_boundary_rows_discarded':discarded_boundaries,
            'restart_boundary_rule':'Retain original sample; discard only the first same-time, same-CV row after a repeated native FIELDS header',
            'fes_path':destination.name,'fes_sha256':sha(destination),'method':'REWEIGHT_METAD log weights; full-trajectory binned probability',
            'importance_weight_ess_uncorrected_for_time_correlation':float(weights.sum()**2/(weights@weights)),
            'observed_bins':int(observed.sum()),'total_bins':int(histogram.size),'converged':False,'affinity_claim':False,
            'note':'Importance-weight ESS excludes time correlation. Inspect FES stability, transitions, correlated ESS and independent repeats before interpretation.'}


def portable_topology(topology,force_field,out):
    """Relocate only absolute includes emitted inside the pinned GMXLIB tree."""
    source=Path(topology); library=Path(os.environ.get('GMXLIB','/nonexistent')).resolve()
    require(library.is_dir(),'Pinned GROMACS force-field library is unavailable')
    text=source.read_text()
    def replace(match):
        name=match[1]; path=Path(name)
        if not path.is_absolute(): return match[0]
        path=path.resolve(); allowed=library/(force_field+'.ff')
        require(path.is_relative_to(allowed) and path.is_file(),'Generated topology references a file outside the declared pinned force field')
        return '#include "'+(Path(force_field+'.ff')/path.relative_to(allowed)).as_posix()+'"'
    text=re.sub(r'^\s*#\s*include\s+"([^"\n]+)"',replace,text,flags=re.M)
    Path(out).write_text(text)
    return {'schema':'bio-md-portable-topology.v1','source_sha256':sha(source),'topology_sha256':sha(out),'force_field':force_field}


def topology_molecule_membership(path):
    section=''; molecule=None; counts={}; result=[]; instance=0
    for raw in Path(path).read_text().splitlines():
        line=raw.split(';',1)[0].strip()
        if not line: continue
        match=re.fullmatch(r'\[\s*([A-Za-z_]+)\s*\]',line)
        if match: section=match[1].lower(); continue
        fields=line.split()
        if section=='moleculetype': molecule=fields[0]; counts[molecule]=0; section=''
        elif section=='atoms': counts[molecule]+=1
        elif section=='molecules':
            for _ in range(int(fields[1])):
                result.extend([instance]*counts[fields[0]]); instance+=1
    require(result,'Prepared topology has no molecular instances')
    return result


def topology_residue_names(path):
    section=''; molecule=None; residues={}; used=set()
    for raw in Path(path).read_text().splitlines():
        line=raw.split(';',1)[0].strip()
        if not line: continue
        match=re.fullmatch(r'\[\s*([A-Za-z_]+)\s*\]',line)
        if match: section=match[1].lower(); continue
        fields=line.split()
        if section=='moleculetype': molecule=fields[0]; residues[molecule]=set(); section=''
        elif section=='atoms': residues[molecule].add(fields[3])
        elif section=='molecules' and int(fields[1])>0: used.add(fields[0])
    return set.union(*(residues[name] for name in used))


def force_field_residues(force_field):
    candidates=[Path(os.environ.get('GMXLIB','/nonexistent'))/(force_field+'.ff'),Path(sys.prefix)/'share/gromacs/top'/(force_field+'.ff')]
    directories=[path.resolve() for path in candidates if path.is_dir()]
    require(directories,'Declared pinned force-field residue templates are unavailable')
    require(len(set(directories))==1,'Declared force field is ambiguous between installed libraries')
    directory=directories[0]; names=set(); sources={}
    subsections={'bondedtypes','atoms','bonds','angles','dihedrals','impropers','exclusions','cmap','pairs'}
    for path in sorted(directory.rglob('*.rtp')):
        sources[path.relative_to(directory).as_posix()]=sha(path)
        for raw in path.read_text().splitlines():
            match=re.fullmatch(r'\s*\[\s*([^\]]+)\s*\]\s*',raw.split(';',1)[0])
            if match and match[1].strip().lower() not in subsections: names.add(match[1].strip())
    for path in sorted(directory.rglob('*.itp')):
        sources[path.relative_to(directory).as_posix()]=sha(path); section=''
        for raw in path.read_text().splitlines():
            line=raw.split(';',1)[0].strip(); match=re.fullmatch(r'\[\s*([A-Za-z_]+)\s*\]',line)
            if match: section=match[1].lower(); continue
            if section=='atoms' and line and not line.startswith('#'):
                fields=line.split()
                if len(fields)>=7: names.add(fields[3])
    require(names,'Declared force field has no residue templates')
    return names,{'force_field':force_field,'template_sha256':sources}


def validate_residue_coverage(topology,force_field,water_model,manifests,base_dir):
    require(force_field and water_model,'Residue admission requires the declared force field and water model')
    known,evidence=force_field_residues(force_field); present=topology_residue_names(topology); receipts=[]; covered=set()
    for path in manifests:
        from .chemistry import validate_topology
        manifest=json.loads(Path(path).read_text())
        receipt=validate_topology(manifest,topology,base_dir,expected_model={'force_field':force_field,'water_model':water_model})
        residue=manifest.get('residue_name')
        require(isinstance(residue,str) and residue not in covered,'Custom residue parameter manifests must identify distinct residues')
        covered.add(residue); receipts.append(receipt)
    unknown=present-known-covered
    require(not unknown,'Prepared topology contains residues without installed force-field templates or validated chemical parameter manifests: '+', '.join(sorted(unknown)))
    return {'schema':'bio-md-residue-admission.v1','installed_templates':evidence,'present_residue_names':sorted(present),
            'custom_parameter_manifests':receipts,'uncovered_residues':[]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); commands=parser.add_subparsers(dest='command',required=True)
    topology=commands.add_parser('check-topology'); topology.add_argument('--topology',required=True); topology.add_argument('--out',required=True)
    topology.add_argument('--neutral-transformation',action='store_true'); topology.add_argument('--manifest',action='append',default=[]); topology.add_argument('--base-dir',default='.')
    topology.add_argument('--force-field'); topology.add_argument('--water-model'); topology.add_argument('--cv-request')
    report=commands.add_parser('metadynamics-report'); report.add_argument('--colvar',required=True); report.add_argument('--out',required=True); report.add_argument('--request',required=True)
    for command in ('bfee-generate','bfee-analyze'):
        child=commands.add_parser(command); child.add_argument('--request',required=True); child.add_argument('--out',required=True)
        child.add_argument('--work' if command=='bfee-generate' else '--bfee-root',required=True)
    centers=commands.add_parser('bfee-centers'); centers.add_argument('--config',required=True); centers.add_argument('--bfee-root',required=True); centers.add_argument('--out',required=True); centers.add_argument('--prepared',required=True)
    analyze=commands.add_parser('analyze-plan'); analyze.add_argument('--plan',required=True); analyze.add_argument('--out',required=True)
    portable=commands.add_parser('portable-topology'); portable.add_argument('--topology',required=True); portable.add_argument('--force-field',required=True); portable.add_argument('--out',required=True)
    geometry=commands.add_parser('bfee-geometry'); geometry.add_argument('--request',required=True); geometry.add_argument('--assets',required=True); geometry.add_argument('--bound-topology',required=True); geometry.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    if args.command=='portable-topology':
        portable_topology(args.topology,args.force_field,args.out); return
    if args.command=='check-topology':
        result=topology_charge_report(args.topology,args.neutral_transformation)
        if args.cv_request: result['collective_variables']=validate_cv_atom_count(json.loads(Path(args.cv_request).read_text()),result['atom_count'])
        result['residue_parameters']=validate_residue_coverage(args.topology,args.force_field,args.water_model,args.manifest,args.base_dir)
    elif args.command=='bfee-geometry': result=bfee_geometry(json.loads(Path(args.request).read_text()),args.assets,args.bound_topology)
    elif args.command=='bfee-generate': result=bfee_generate(json.loads(Path(args.request).read_text()),args.work)
    elif args.command=='bfee-analyze': result=bfee_analyze(json.loads(Path(args.request).read_text()),args.bfee_root)
    elif args.command=='bfee-centers': result=bfee_centers(args.config,args.bfee_root,args.prepared)
    elif args.command=='analyze-plan': result=analyze_plan(args.plan)
    else: result=metadynamics_report(json.loads(Path(args.request).read_text()),args.colvar,args.out)
    _write(Path(args.out),json.dumps(result,indent=2)+'\n')


if __name__=='__main__': main()
