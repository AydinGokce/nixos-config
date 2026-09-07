"""Protocol admission and execution graph checks; toy text is not a physical MD fixture."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from md.protocols import prepare,validate_request,topology_charge_report,bfee_centers,plumed_input,read_pmf,validate_residue_coverage,metadynamics_report,topology_molecule_membership,portable_topology,validate_cv_atom_count


TOP='''[ defaults ]
1 2 yes 0.5 0.833333
[ atomtypes ]
C 6 12 0 A 0.3 0.2
D 6 12 0 A 0.3 0.0
[ moleculetype ]
Protein 3
[ atoms ]
1 C 1 ALA CA 1 0.1 12 D 0.0 12
2 C 1 ALA CB 1 -0.1 12 C 0.0 12
[ system ]
Toy topology parser fixture
[ molecules ]
Protein 2
'''


def request():
    return {'schema':'bio-md-request.v1','protocol':'pmx_binding_ddg','comparison_id':'target-v-to-a',
            'systems':{name:{'coordinates':'prepared.gro','topology':'prepared.top'} for name in ('bound','unbound')},
            'conditions':{'force_field':'amber99sb-star-ildn-mut','water_model':'tip3p','temperature_kelvin':300,
                          'pressure_bar':1,'ionic_strength_molar':0.15,'prepared_solvated_ionized':True,
                          'protonation':{'method':'explicit','pH':7.4,'description':'Reviewed explicit hydrogens in admitted topology'},
                          'nonbonded':{'coulombtype':'PME','rcoulomb_nm':1.2,'rvdw_nm':1.2,'vdw_modifier':'Potential-shift','dispersion_correction':'EnerPres'}},
            'simulation':{'dt_ps':.002,'minimization_steps':50,'nvt_steps':100,'npt_steps':100,'production_steps':200,'output_stride':10,'seed':42},
            'pmx':{'topology_mode':'prepared_hybrid','mutation':{'chain':'A','resid':1,'from':'VAL','to':'ALA'},
                   'lambda_schedule':[0,.5,1],'replicas':2,'charge_change_e':0}}


def plumed_request():
    value=request(); value['protocol']='plumed_metadynamics'; value.pop('pmx')
    value['systems']={'complex':value['systems']['bound']}
    value['enhanced']={'cvs':[{'name':'contact','type':'distance','atoms':[1,2],'sigma':.1,'grid_min':0,'grid_max':3}],
                       'whole_molecules':[[1,2]],'biasfactor':10,'height_kj_mol':1.2,'pace_steps':10}
    return value


def bfee_request():
    value=request(); value['protocol']='bfee3_geometric'; value.pop('pmx')
    value['bfee3']={'receptor_selection':'chainID A','ligand_selection':'chainID B','solvent_selection':'resname SOL',
                    'bulk_distance_nm':3,'periodic_margin_nm':1,'prepared_box_supports_bulk_separation':True,
                    'restraint_convention':'upstream_geometric_v1'}
    return value


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.assets=self.root/'assets'; self.assets.mkdir()
        (self.assets/'prepared.top').write_text(TOP)
        (self.assets/'prepared.gro').write_text('Parser-only fixture; not a physical structure\n0\n 10 10 10\n')

    def plan(self,value): return prepare(value,self.assets,self.root/'work')

    def test_pmx_cycle_has_all_states_legs_replicas_and_actual_analysis(self):
        plan=self.plan(request()); inputs=plan['analysis_inputs']
        self.assertEqual(len(inputs),4); self.assertTrue(all(len(item['paths'])==3 for item in inputs))
        self.assertEqual(plan['stages'][-1]['argv'][3],'analyze-plan')
        for item in inputs:
            self.assertEqual(item['protocol']['comparison_id'],'target-v-to-a')
            self.assertEqual(item['protocol']['pressure_bar'],1)
            self.assertIn('hamiltonian',item['protocol'])
        bound=(self.root/'work/bound/replica-000/lambda-000/nvt/run.mdp').read_text()
        unbound=(self.root/'work/unbound/replica-000/lambda-000/nvt/run.mdp').read_text()
        self.assertIn('gen-seed = 42',bound); self.assertIn('gen-seed = 100042',unbound)
        self.assertIn('calc-lambda-neighbors = -1',bound)
        self.assertEqual(sum(x['argv'][1:2]==['editconf'] for x in plan['stages']),12)
        seen=set(); outputs=set()
        for stage in plan['stages']:
            self.assertTrue(set(stage['dependencies'])<=seen); seen.add(stage['id'])
            self.assertFalse(outputs&set(stage['outputs'])); outputs.update(stage['outputs'])
            self.assertNotIn('-maxwarn',stage['argv'])
            self.assertNotIn('shell',stage)

    def test_input_fingerprints_prevent_changed_source_bytes(self):
        value=validate_request(request(),self.assets)
        (self.assets/'prepared.gro').write_text('changed')
        with self.assertRaisesRegex(ValueError,'bytes changed'): self.plan(value)

    def test_include_escape_and_unresolved_forcefield_rejected(self):
        for include in ('/etc/passwd','../outside.itp','unrelated.ff/forcefield.itp','$(echo leak)'):
            with self.subTest(include=include):
                (self.assets/'prepared.top').write_text('#include "'+include+'"\n')
                with self.assertRaises(ValueError): validate_request(request(),self.assets)

    def test_declared_forcefield_and_nested_admitted_includes_accepted(self):
        (self.assets/'nested').mkdir(); (self.assets/'nested'/'protein.itp').write_text(TOP)
        (self.assets/'prepared.top').write_text('#include "amber99sb-star-ildn-mut.ff/forcefield.itp"\n#include "nested/protein.itp"\n')
        self.assertEqual(len(validate_request(request(),self.assets)['_asset_files']),3)

    def test_charge_and_fake_hybrid_fail_closed(self):
        path=self.assets/'prepared.top'; report=topology_charge_report(path,True)
        self.assertAlmostEqual(report['charge_change_e'],0); self.assertEqual(report['atom_count'],4)
        path.write_text(TOP.replace('D 0.0 12','D 0.5 12'))
        with self.assertRaisesRegex(ValueError,'total charge'): topology_charge_report(path,True)
        path.write_text(TOP.replace(' D 0.0 12','').replace(' C 0.0 12',''))
        with self.assertRaisesRegex(ValueError,'no hybrid'): topology_charge_report(path,True)

    def test_charge_changing_mutation_and_missing_preparation_rejected(self):
        value=request(); value['pmx']['mutation']['to']='LYS'
        with self.assertRaisesRegex(ValueError,'formal charge'): self.plan(value)
        value=request(); value['conditions']['prepared_solvated_ionized']=False
        with self.assertRaisesRegex(ValueError,'solvent'): self.plan(value)

    def test_metadynamics_is_structured_reweighted_and_not_affinity(self):
        plan=self.plan(plumed_request()); text=(self.root/'work/enhanced/production/plumed.dat').read_text()
        self.assertIn('REWEIGHT_METAD',text); self.assertIn('metad.bias,metad.rbias,metad.rct,weights FILE=COLVAR',text)
        self.assertNotIn('ENERGY',text); self.assertFalse(plan['analysis_inputs'][0]['affinity_claim'])
        production=next(x for x in plan['stages'] if x['id']=='enhanced_production')
        self.assertIn('-plumed',production['argv']); self.assertIn('-ntmpi',production['argv'])
        self.assertEqual(production['checkpoint'],'enhanced/production/run.cpt')

    def test_metadynamics_fes_is_generated_after_sampling_without_periodic_backups(self):
        value=plumed_request(); value['simulation']['production_steps']=2000
        plan=self.plan(value); text=(self.root/'work/enhanced/production/plumed.dat').read_text()
        # More than 101 output strides used to exhaust PLUMED's DUMPGRID backups.
        for action in ('HISTOGRAM','CONVERT_TO_FES','DUMPGRID'):
            self.assertNotIn(action,text)
        production=next(x for x in plan['stages'] if x['id']=='enhanced_production')
        analysis=next(x for x in plan['stages'] if x['id']=='metadynamics_analysis')
        retained=['enhanced/production/HILLS','enhanced/production/COLVAR']
        self.assertTrue(set(retained)<=set(production['outputs']))
        self.assertEqual(production['restart_files'],retained)
        self.assertNotIn('enhanced/production/fes.dat',production['outputs'])
        self.assertNotIn('enhanced/production/reweighted-fes.dat',production['outputs'])
        self.assertEqual(analysis['dependencies'],[production['id']])
        self.assertIn('enhanced/production/COLVAR',analysis['inputs'])
        self.assertIn('enhanced/production/reweighted-fes.dat',analysis['outputs'])

    def test_cv_injection_invalid_indices_and_unsupported_cv_reject(self):
        for key,value in [('name','x\nINCLUDE FILE=/etc/passwd'),('atoms',[{},2]),('type','energy')]:
            attempt=plumed_request(); attempt['enhanced']['cvs'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): validate_request(attempt,self.assets)

    def test_bfee_graph_has_upstream_generation_eight_pmfs_and_no_resolvation(self):
        plan=self.plan(bfee_request()); stages=plan['stages']; seen=set(); outputs=set()
        for stage in stages:
            self.assertTrue(set(stage['dependencies'])<=seen); seen.add(stage['id'])
            self.assertFalse(outputs&set(stage['outputs'])); outputs.update(stage['outputs'])
            self.assertNotIn('solvate',stage['argv']); self.assertNotIn('sh',stage['argv'])
        self.assertEqual(len(plan['analysis_inputs'][0]['paths']),8)
        self.assertEqual(stages[-1]['argv'][3],'bfee-analyze')
        centers=next(x for x in stages if x['id']=='bfee_007_r_centers')
        grompp=next(x for x in stages if x['id']=='bfee_007_r_grompp')
        self.assertIn(centers['id'],grompp['dependencies'])
        self.assertIn('--prepared',centers['argv'])

    def test_restraint_minimum_uses_real_pmf_before_grompp_without_mutating_template(self):
        root=self.root/'BFEE'; pmf=root/'002_euler_theta/output/002_euler_theta.out.abf1.czar.pmf'; pmf.parent.mkdir(parents=True)
        pmf.write_text('# native PMF\n-1 2\n0.5 -2\n1 1\n')
        pmf.with_name('002_euler_theta.out.abf1.zcount').write_text('-1 10000\n0.5 10000\n1 10000\n')
        original=self.root/'template.dat'; original.write_text('centers 002_pmf_argmin\n')
        resolved=self.root/'ready.dat'; result=bfee_centers(original,root,resolved)
        self.assertEqual(original.read_text(),'centers 002_pmf_argmin\n'); self.assertEqual(resolved.read_text(),'centers 0.5\n')
        self.assertEqual(result['measured_centers']['002']['center'],.5)
        pmf.with_name('002_euler_theta.out.abf1.zcount').write_text('-1 0\n0.5 20\n1 0\n')
        with self.assertRaisesRegex(ValueError,'Insufficient BFEE3'): bfee_centers(original,root,resolved)
        pmf.with_name('002_euler_theta.out.abf1.zcount').write_text('-1 10000\n0.5 10000\n1 10000\n')
        with self.assertRaisesRegex(ValueError,'overwrite'): bfee_centers(original,root,original)

    def test_invalid_pmf_not_published_as_affinity(self):
        path=self.root/'pmf'; path.write_text('0 1\n1 nan\n2 0\n')
        with self.assertRaisesRegex(ValueError,'nonfinite'): read_pmf(path)
        path.write_text('0 1\n0 2\n1 3\n')
        with self.assertRaisesRegex(ValueError,'increasing'): read_pmf(path)


    def test_unknown_modified_residue_cannot_bypass_manifest_admission(self):
        path=self.assets/'prepared.top'; path.write_text(TOP.replace('ALA','XNA'))
        with patch('md.protocols.force_field_residues',return_value=({'ALA'},{})):
            with self.assertRaisesRegex(ValueError,'without installed force-field'):
                validate_residue_coverage(path,'test','tip3p',[],self.assets)

    def test_plural_manifests_are_admitted_and_routed_without_dropping_alias(self):
        for name in ('one','two'): (self.assets/(name+'.json')).write_text('{}')
        value=request(); value['systems']['bound'].update(parameter_manifest='one.json',parameter_manifests=['one.json','two.json'])
        plan=self.plan(value)
        check=next(stage for stage in plan['stages'] if stage['id']=='bound_replica-000_lambda-000_min_topology')
        self.assertEqual(check['argv'].count('--manifest'),2)
        self.assertIn('--force-field',check['argv'])
        metadata=plan['analysis_inputs'][0]['protocol']['parameter_manifest_sha256']
        self.assertEqual(len(metadata['bound']),2)

    def test_topology_instances_and_mass_omission_are_valid(self):
        path=self.assets/'prepared.top'; path.write_text(TOP.replace('0.1 12 D 0.0 12','0.1').replace('-0.1 12 C 0.0 12','-0.1'))
        self.assertEqual(topology_molecule_membership(path),[0,0,1,1])
        self.assertEqual(topology_charge_report(path)['atom_count'],4)

    def test_bfee_geometry_head_gate_has_no_dynamics_ancestry(self):
        plan=self.plan(bfee_request()); lookup={s['id']:s for s in plan['stages']}; gate=lookup['bfee_geometry']
        self.assertTrue(gate['head_preflight'])
        def visit(identity):
            stage=lookup[identity]; self.assertNotEqual(stage['argv'][:2],['gmx','mdrun'])
            for parent in stage['dependencies']: visit(parent)
        visit(gate['id'])

    def test_weighted_fes_uses_full_logweights_and_rejects_duplicate_restart_times(self):
        try: import numpy
        except ImportError: self.skipTest('Native weighted-FES test requires NumPy')
        value=plumed_request(); value['enhanced']['cvs'][0].update(grid_bins=10,grid_min=0,grid_max=1)
        path=self.root/'COLVAR'; path.write_text('#! FIELDS time contact weights\n0 0.15 0\n1 0.85 1.0986122886681098\n')
        report=metadynamics_report(value,path,self.root/'report.json')
        rows=[row.split() for row in (self.root/'reweighted-fes.dat').read_text().splitlines() if not row.startswith('#')]
        self.assertAlmostEqual(float(rows[1][-1]),.25); self.assertAlmostEqual(float(rows[8][-1]),.75)
        self.assertFalse(report['affinity_claim']); self.assertAlmostEqual(report['importance_weight_ess_uncorrected_for_time_correlation'],1.6)
        path.write_text('#! FIELDS time contact weights\n0 0.15 0\n0 0.85 1\n')
        with self.assertRaisesRegex(ValueError,'overlap or decrease'): metadynamics_report(value,path,self.root/'report.json')
        path.write_text('#! FIELDS time contact weights\n0 0.15 0\n1 0.85 1.0986122886681098\n#! FIELDS time contact weights\n1 0.85 9.0\n')
        resumed=metadynamics_report(value,path,self.root/'resumed.json')
        self.assertEqual(resumed['restart_boundary_rows_discarded'],1)
        self.assertEqual(resumed['samples'],2)
        self.assertAlmostEqual(resumed['importance_weight_ess_uncorrected_for_time_correlation'],1.6)
        path.write_text('#! FIELDS time contact weights\n0 0.15 0\n1 0.85 1\n#! FIELDS time contact weights\n1 0.86 9\n')
        with self.assertRaisesRegex(ValueError,'different molecular CV'): metadynamics_report(value,path,self.root/'bad-resume.json')


    def test_portable_topology_rewrites_only_pinned_forcefield_files(self):
        library=self.root/'library'; field=library/'test.ff'; field.mkdir(parents=True); (field/'forcefield.itp').write_text('; pinned fixture')
        source=self.assets/'prepared.top'; source.write_text('#include "'+str(field/'forcefield.itp')+'"\n')
        with patch.dict('os.environ',{'GMXLIB':str(library)}):
            portable_topology(source,'test',self.root/'portable.top')
            self.assertEqual((self.root/'portable.top').read_text(),'#include "test.ff/forcefield.itp"\n')
            source.write_text('#include "/etc/passwd"\n')
            with self.assertRaisesRegex(ValueError,'outside'): portable_topology(source,'test',self.root/'portable.top')



    def test_enhanced_native_atom_count_gate_precedes_dynamics(self):
        value=plumed_request(); plan=self.plan(value)
        first=next(stage for stage in plan['stages'] if stage['id']=='enhanced_min_topology')
        self.assertIn('--cv-request',first['argv'])
        self.assertTrue(validate_cv_atom_count(value,2)['within_actual_topology'])
        value['enhanced']['cvs'][0]['atoms']=[1,3]
        with self.assertRaisesRegex(ValueError,'Collective-variable atom index'): validate_cv_atom_count(value,2)
        value['enhanced']['cvs'][0]['atoms']=[1,2]; value['enhanced']['whole_molecules']=[[1,3]]
        with self.assertRaisesRegex(ValueError,'Whole-molecule range'): validate_cv_atom_count(value,2)

if __name__=='__main__': unittest.main()
