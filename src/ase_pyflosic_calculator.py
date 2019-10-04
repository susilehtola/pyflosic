#2019 PyFLOSIC developers
           
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# pyflosic-ase-calculator 
#
# author:	S. Schwalbe 
# task:  	ase calculator for pyflosic 

import os, sys
import numpy as np
from ase.calculators.calculator import FileIOCalculator, Parameters, kpts2mp, \
    ReadError
from ase.atom import Atom
from ase import Atoms 
from ase.units import Ha, Bohr, Debye 
try:
    from ase.atoms import atomic_numbers 
except:
    # moved in 3.17 to
    from ase.data import atomic_numbers
import copy
from flosic_os import xyz_to_nuclei_fod,ase2pyscf,flosic 
from flosic_scf import FLOSIC
from ase.calculators.calculator import Calculator, all_changes, compare_atoms

def force_max_lij(lambda_ij):
    # 
    # calculate the RMS of the l_ij matrix 
    # 
    nspin = 2
    lijrms = 0
    for s in range(nspin):
        M = lambda_ij[s,:,:]
        e = (M-M.T)[np.triu_indices((M-M.T).shape[0])]
        e_tmp = 0.0
        for f in range(len(e)):
            e_tmp = e_tmp + e[f]**2.
        e_tmp = np.sqrt(e_tmp/(M.shape[0]*(M.shape[0]-1)))
        lijrms =  lijrms + e_tmp
    lijrms = lijrms/2.
    return  lijrms

def apply_field(mol,mf,E):
    # 
    # add efield to hamiltonian 
    # 
    # The gauge origin for dipole integral
    mol.set_common_orig([0., 0., 0.])
    # recalculate h1e with extra efield 
    h =(mol.intor('cint1e_kin_sph') + mol.intor('cint1e_nuc_sph') + np.einsum('x,xij->ij', E, mol.intor('cint1e_r_sph', comp=3)))
    # update h1e with efield 
    mf.get_hcore = lambda *args: h

class PYFLOSIC(FileIOCalculator):
    """ PYFLOSIC calculator for atoms and molecules.
        by Sebastian Schwalbe
        Notes: ase.calculators -> units [eV,Angstroem,eV/Angstroem]
    	   pyflosic	   -> units [Ha,Bohr,Ha/Bohr] 		                
    """
    implemented_properties = ['energy', 'forces','fodforces','dipole','evalues','homo']
    PYFLOSIC_CMD = os.environ.get('ASE_PYFLOSIC_COMMAND')
    command =  PYFLOSIC_CMD

    # Note: If you need to add keywords, please also add them in valid_args 
    default_parameters = dict(
        fod1 = None,            # ase atoms object FODs spin channel 1 
        fod2 = None,            # ase atoms objects FODs spin channnel 2 
        mol= None,              # PySCF mole object 
        charge = None,          # charge of the system 
        spin = None,            # spin of the system, equal to 2S 
        basis = None,           # basis set
        ecp = None,             # only needed if ecp basis set is used 
        xc = 'LDA,PW',          # exchange-correlation potential - must be available in libxc 
        mode = 'flosic-os',     # calculation method 
        efield=None,            # perturbative electric field
        max_cycle = 300,        # maximum number of SCF cycles 
        conv_tol = 1e-5,        # energy convergence threshold 
        grid = 3,               # numerical mesh (lowest: 0, highest: 9)
        ghost= False,           # ghost atoms at FOD positions 
        mf = None,              # PySCF calculation object
        use_newton=False,       # use the Newton SCF cycle 
        use_chk=False,          # restart from checkpoint file 
        verbose=0,              # output verbosity 
        calc_forces=False,      # calculate FOD forces 
        debug=False,            # extra ouput for debugging purpose 
        l_ij=None,              # developer option: alternative optimization target  
        ods=None,               # developer option: orbital damping sic 
        fopt='force',           # developer option: in use with l_ij, alternative optimization target 
        fixed_vsic=None,        # fixed SIC one body values Veff, Exc, Ecoul
        num_iter=0,             # SCF iteration number 
        vsic_every=1,           # calculate vsic after this number on num_iter cycles 
        ham_sic ='HOO',         # unified SIC Hamiltonian - HOO or HOOOV 
        dm = None               # density matrix
        ) 

    def __init__(self, restart=None, ignore_bad_restart_file=False,
        label=os.curdir, atoms=None, **kwargs):
        """ Constructor """
        FileIOCalculator.__init__(self, restart, ignore_bad_restart_file,
                                  label, atoms, **kwargs)
        valid_args = ('fod1','fod2','mol','charge','spin','basis','ecp','xc','mode','efield','max_cycle','conv_tol','grid','ghost','mf','use_newton','use_chk','verbose','calc_forces','debug','l_ij','ods','fopt','fixed_vsic','num_iter','vsic_every','ham_sic','dm')
        # set any additional keyword arguments
        for arg, val in self.parameters.items():
            if arg in valid_args:
                setattr(self, arg, val)
            else:
                raise RuntimeError('unknown keyword arg "%s" : not in %s'% (arg, valid_args))
        
        self.set_atoms(atoms)
        

    def initialize(self,atoms=None,properties=['energy'],system_changes=all_changes):
        self.atoms = atoms.copy()

    def set_atoms(self, atoms):
        if self.atoms != atoms:
            self.atoms = atoms.copy()
            self.results = {}       

    def get_atoms(self):
        if self.atoms is None:
            raise ValueError('Calculator has no atoms')
        atoms = self.atoms.copy()
        atoms.calc = self
        return atoms

    def set_label(self, label):
        self.label = label
        self.directory = label
        self.prefix = ''
        self.out = os.path.join(label, 'pyflosic.out')

    def check_state(self, atoms,tol=1e-15):
        if atoms is  None:
            system_changes = []
        else:
            system_changes = compare_atoms(self.atoms, atoms)
        
        # Ignore boundary conditions until now 
        if 'pbc' in system_changes:
            system_changes.remove('pbc')
        
        return system_changes

    def set(self, **kwargs):
        changed_parameters = FileIOCalculator.set(self, **kwargs)
        if changed_parameters:
            self.reset()
               
    def write_input(self, atoms, properties=None, system_changes=None, **kwargs):
        FileIOCalculator.write_input(self, atoms, properties, system_changes)
        self.initialize(atoms)
        
    def get_energy(self,atoms=None):
        return self.get_potential_energy(atoms) 

    def calculation_required(self, atoms, properties):
        # checks of some properties need to be calculated or not 
        assert not isinstance(properties, str)
        system_changes = self.check_state(atoms)
        if system_changes:
            return True
        for name in properties:
            if name not in self.results:
                return True
        return False

    def get_potential_energy(self, atoms=None, force_consistent = False):
        # calculate total energy if required 
        if self.calculation_required(atoms,['energy']):
            self.calculate(atoms)
        if self.fopt == 'lij':
            # res = force_max_lij(self.lambda_ij)
            res = self.results['energy'].copy()
        if self.fopt == 'force':
            res = self.results['energy'].copy()
        if self.fopt == 'esic-force':
            res = self.results['esic'].copy()
        return res 

    def get_forces(self, atoms=None):
        if self.calculation_required(atoms,['forces']):
            self.calculate(atoms)
        self.forces = self.results['forces'].copy()
        return self.forces

    def get_fodforces(self, atoms=None):
        if self.calculation_required(atoms,['fodforces']):
            self.calculate(atoms)
        self.fodforces = self.results['fodforces'].copy() 
        return self.fodforces 
	
    def get_dipole_moment(self,atoms=None):
        if self.calculation_required(atoms,['dipole']):
            self.calculate(atoms)
        self.dipole_moment = self.results['dipole'].copy()
        return self.dipole_moment

    def get_evalues(self,atoms=None):
        if self.calculation_required(atoms,['evalues']):
            self.calculate(atoms)
        self.evalues = self.results['evalues'].copy()
        return self.evalues

    def get_homo(self,atoms=None):
        if self.calculation_required(atoms,['homo']):
            self.calculate(atoms)
        self.homo = self.results['homo'].copy()
        return self.homo

    def calculate(self, atoms = None, properties = ['energy','dipole','evalues','fodforces','forces','homo'], system_changes = all_changes):
        self.num_iter += 1 
        if atoms is None:
            atoms = self.get_atoms()
        else:
            self.set_atoms(atoms)
        if self.mode == 'dft':
            # DFT only mode 
            from pyscf import gto, scf
            from pyscf.grad import uks
            [geo,nuclei,fod1,fod2,included] =  xyz_to_nuclei_fod(atoms)
            mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge)
            mf = scf.UKS(mol)
            mf.xc = self.xc 
            # Verbosity of the mol object (o lowest output, 4 might enough output for debugging) 
            mf.verbose = self.verbose
            mf.max_cycle = self.max_cycle
            mf.conv_tol = self.conv_tol
            mf.grids.level = self.grid
            if self.use_chk == True and self.use_newton == False:
                mf.chkfile = 'pyflosic.chk'
            if self.use_chk == True and self.use_newton == False and os.path.isfile('pyflosic.chk'):
                mf.init_guess = 'chk'
                mf.update('pyflosic.chk')
                self.dm = mf.make_rdm1()
            if self.use_newton == True and self.xc != 'SCAN,SCAN':
                mf = mf.as_scanner()
                mf = mf.newton()
            self.mf = mf
            if self.dm is None:
                e = self.mf.kernel()
            else:
                e = self.mf.kernel(self.dm)
            self.results['energy'] = e*Ha
            self.results['dipole'] = self.mf.dip_moment(verbose=0)*Debye # conversion to e*A to match the ase calculator object
            self.results['evalues'] = np.array(self.mf.mo_energy)*Ha
            n_up, n_dn = self.mf.mol.nelec
            if n_up != 0 and n_dn != 0:
                e_up = np.sort(self.results['evalues'][0])
                e_dn = np.sort(self.results['evalues'][1])
                homo_up = e_up[n_up-1]
                homo_dn = e_dn[n_dn-1]
                self.results['homo'] = max(homo_up,homo_dn)
            elif n_up != 0:
                e_up = np.sort(self.results['evalues'][0])
                self.results['homo'] = e_up[n_up-1]
            elif n_dn != 0:
                e_dn = np.sort(self.results['evalues'][1])
                self.results['homo'] = e_dn[n_dn-1]
            else:
                self.results['homo'] = None

            self.results['fodforces'] = None
            if self.xc != 'SCAN,SCAN': # no gradients for meta-GGAs!
                gf = uks.Gradients(self.mf)
                gf.verbose = self.verbose
                gf.grid_response = True
                forces = gf.kernel()*(Ha/Bohr)
                forces = -1*forces
                forces = forces.tolist()
                totalforces = []
                totalforces.extend(forces)
                fod1forces = np.zeros_like(fod1.get_positions())
                fod2forces = np.zeros_like(fod2.get_positions())
                fod1forces = fod1forces.tolist()
                fod2forces = fod2forces.tolist()
                totalforces.extend(fod1forces)
                totalforces.extend(fod2forces)
                totalforces = np.array(totalforces)
                self.results['forces'] = totalforces
            else:
                self.results['forces'] = None
            
        if self.mode == 'flosic-os':
            from pyscf import gto,scf
            [geo,nuclei,fod1,fod2,included] =  xyz_to_nuclei_fod(atoms)
            if self.ecp == None:
                if self.ghost == False:
                    mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge)
                
                if self.ghost == True:
                    mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge)
                    mol.basis ={'default':self.basis,'GHOST1':gto.basis.load('sto3g', 'H'),'GHOST2':gto.basis.load('sto3g', 'H')}
            if self.ecp != None:
                mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge,ecp=self.ecp)
            mf = scf.UKS(mol)
            mf.xc = self.xc 
            mf.verbose = self.verbose
            if self.use_chk == True and self.use_newton == False:
                mf.chkfile = 'pyflosic.chk'
            if self.use_chk == True and self.use_newton == False and os.path.isfile('pyflosic.chk'):
                mf.init_guess = 'chk'
                mf.update('pyflosic.chk')
                self.dm = mf.make_rdm1()
            if self.use_newton == True and self.xc != 'SCAN,SCAN':
                mf = mf.as_scanner()
                mf = mf.newton()
            mf.max_cycle = self.max_cycle
            mf.conv_tol = self.conv_tol
            mf.grids.level = self.grid
            self.mf = mf
            if self.dm is None :
                e = self.mf.kernel()
            else:
                e = self.mf.kernel(self.dm)
            mf = flosic(mol,self.mf,fod1,fod2,sysname=None,datatype=np.float64, print_dm_one = False, print_dm_all = False,debug=self.debug,l_ij = self.l_ij, ods = self.ods, fixed_vsic = self.fixed_vsic, calc_forces=True,ham_sic = self.ham_sic)
            self.results['energy']= mf['etot_sic']*Ha
            self.results['fodforces'] = -1*mf['fforces']*(Ha/Bohr) 
            print('Analytical FOD force [Ha/Bohr]')
            print(-1*mf['fforces'])
            print('fmax = %0.6f [Ha/Bohr]' % np.sqrt((mf['fforces']**2).sum(axis=1).max()))
            self.results['dipole'] = mf['dipole']*Debye
            self.results['evalues'] = mf['evalues']*Ha
            n_up, n_dn = self.mf.mol.nelec
            if n_up != 0 and n_dn != 0:
                e_up = np.sort(self.results['evalues'][0])
                e_dn = np.sort(self.results['evalues'][1])
                homo_up = e_up[n_up-1]
                homo_dn = e_dn[n_dn-1]
                self.results['homo'] = max(homo_up,homo_dn)
            elif n_up != 0:
                e_up = np.sort(self.results['evalues'][0])
                self.results['homo'] = e_up[n_up-1]
            elif n_dn != 0:
                e_dn = np.sort(self.results['evalues'][1])
                self.results['homo'] = e_dn[n_dn-1]
            else:
                self.results['homo'] = None

        if self.mode == 'flosic-scf' or self.mode == 'both':
            from pyscf import gto
            [geo,nuclei,fod1,fod2,included] =  xyz_to_nuclei_fod(atoms)
            # Effective core potentials need so special treatment. 
            if self.ecp is None:
                if self.ghost == False: 
                    mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge)	
                if self.ghost == True: 
                    mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge)
                    mol.basis ={'default':self.basis,'GHOST1':gto.basis.load('sto3g', 'H'),'GHOST2':gto.basis.load('sto3g', 'H')}
            if self.ecp != None:
                mol = gto.M(atom=ase2pyscf(nuclei), basis=self.basis,spin=self.spin,charge=self.charge,ecp=self.ecp)
            if self.efield != None:
                m0 = FLOSIC(mol=mol,xc=self.xc,fod1=fod1,fod2=fod2,grid_level=self.grid,debug=self.debug,l_ij=self.l_ij,ods=self.ods,fixed_vsic=self.fixed_vsic,num_iter=self.num_iter,vsic_every=self.vsic_every,ham_sic=self.ham_sic)
                # test efield to enforce some pseudo chemical environment 
                # and break symmetry of density 
                m0.grids.level = self.grid
                m0.conv_tol = self.conv_tol
                # small efield 
                m0.max_cycle = 1
                h= -0.0001 #-0.1
                apply_field(mol,m0,E=(0,0,0+h))
                m0.kernel()
            mf = FLOSIC(mol=mol,xc=self.xc,fod1=fod1,fod2=fod2,grid_level=self.grid,calc_forces=self.calc_forces,debug=self.debug,l_ij=self.l_ij,ods=self.ods,fixed_vsic=self.fixed_vsic,num_iter=self.num_iter,vsic_every=self.vsic_every,ham_sic=self.ham_sic)
            mf.verbose = self.verbose 
            if self.use_chk == True and self.use_newton == False:
                mf.chkfile = 'pyflosic.chk'
            if self.use_chk == True and self.use_newton == False and os.path.isfile('pyflosic.chk'):
                mf.init_guess = 'chk'
                mf.update('pyflosic.chk')
                self.dm = mf.make_rdm1()
            if self.use_newton == True and self.xc != 'SCAN,SCAN':
                mf = mf.as_scanner()
                mf = mf.newton() 
            mf.max_cycle = self.max_cycle
            mf.conv_tol = self.conv_tol
            self.mf = mf
            if self.dm is None:
                e = self.mf.kernel()
            else:
                e = self.mf.kernel(self.dm)
            self.results['esic'] = self.mf.esic*Ha
            self.results['energy'] = e*Ha
            self.results['fixed_vsic'] = self.mf.fixed_vsic  
            
            if self.fopt == 'force' or self.fopt == 'esic-force':
                # 
                # The standard optimization uses 
                # the analytical FOD forces 
                #
                fforces = self.mf.get_fforces()
                self.results['fodforces'] = fforces*(Ha/Bohr)
                print('Analytical FOD force [Ha/Bohr]')
                print(fforces)
                print('fmax = %0.6f [Ha/Bohr]' % np.sqrt((fforces**2).sum(axis=1).max()))

            if self.fopt == 'lij':
                #
                # This is under development. 
                # Trying to replace the FOD forces. 
                #
                self.lambda_ij = self.mf.lambda_ij
                self.results['lambda_ij'] = self.mf.lambda_ij
                #fforces = []
                #nspin = 2  
                #for s in range(nspin):
                #	# printing the lampda_ij matrix for both spin channels 
                #	print 'lambda_ij'
                #	print lambda_ij[s,:,:]
                #	print 'RMS lambda_ij'
                #	M = lambda_ij[s,:,:]
                #	fforces_tmp =  (M-M.T)[np.triu_indices((M-M.T).shape[0])]
                #	fforces.append(fforces_tmp.tolist()) 
                #print np.array(fforces).shape
                try:	
                    # 
                    # Try to calculate the FOD forces from the differences 
                    # of SIC eigenvalues 
                    #
                    evalues_old = self.results['evalues']/Ha
                    print(evalues_old)
                    evalues_new = self.mf.evalues
                    print(evalues_new)
                    delta_evalues_up = (evalues_old[0][0:len(fod1)] - evalues_new[0][0:len(fod1)]).tolist()
                    delta_evalues_dn = (evalues_old[1][0:len(fod2)] - evalues_new[1][0:len(fod2)]).tolist()
                    print(delta_evalues_up)
                    print(delta_evalues_dn)
                    lij_force = delta_evalues_up
                    lij_force.append(delta_evalues_dn)
                    lij_force = np.array(lij_force) 
                    lij_force = np.array(lij_force,(np.shape(lij_force)[0],3))
                    print('FOD force evalued from evalues')
                    print(lij_force)
                    self.results['fodforces'] = lij_force 
                except:
                    # 
                    # If we are in the first iteration 
                    # we can still use the analystical FOD forces 
                    # as starting values 
                    # 
                    fforces = self.mf.get_fforces()
                    print(fforces)  
                    #self.results['fodforces'] = -1*fforces*(Ha/Bohr)
                    self.results['fodforces'] = fforces*(Ha/Bohr)
                    print('Analytical FOD force [Ha/Bohr]')
                    print(fforces) 
                    print('fmax = %0.6f [Ha/Bohr]' % np.sqrt((fforces**2).sum(axis=1).max()))

            self.results['dipole'] =  self.mf.dip_moment()*Debye
            self.results['evalues'] = np.array(self.mf.evalues)*Ha
            n_up, n_dn = self.mf.mol.nelec
            if n_up != 0 and n_dn != 0:
                e_up = np.sort(self.results['evalues'][0])
                e_dn = np.sort(self.results['evalues'][1])
                homo_up = e_up[n_up-1]
                homo_dn = e_dn[n_dn-1]
                self.results['homo'] = max(homo_up,homo_dn)
            elif n_up != 0:
                e_up = np.sort(self.results['evalues'][0])
                self.results['homo'] = e_up[n_up-1]
            elif n_dn != 0:
                e_dn = np.sort(self.results['evalues'][1])
                self.results['homo'] = e_dn[n_dn-1]
            else:
                self.results['homo'] = None


        if self.mode == 'flosic-scf' or self.mode == 'flosic-os' or self.mode =='both':
            totalforces = []
            forces = np.zeros_like(nuclei.get_positions()) 
            fodforces = self.results['fodforces'].copy()
            forces = forces.tolist()
            fodforces = fodforces.tolist()
            totalforces.extend(forces)
            totalforces.extend(fodforces)
            totalforces = np.array(totalforces)
            self.results['forces'] = totalforces
        	

class BasicFLOSICC(Calculator):
    '''Interface to use ase.optimize methods in an easy way within the
    pyflosic framework.
    
    This is an easy-to-use class to make the usage of pyflosic identical to other
    ase optimizer classes.
    
    For details refer to https://wiki.fysik.dtu.dk/ase/ase/optimize.html
    
    
    Kwargs
        mf : a FLOSIC class object
            to be used to optimize the FOD's on (mandatory)
        
        atoms: an ase.Atoms object
            contains both, the spin up and down FODs
               
            fod1 and fod2 input to the FLOSIC class *must* be references
            pointing to this atoms object:
            fods = ase.Atoms('X2He2', positions=[[0,0,0],[-1,0,0],[0,0,0],[-1,0,0]])
            fodup = fod[:2]
            foddn = fod[2:]
            mf = FLOSIC(...,fod1=fodup, fod2=foddn, ...)
    
    author:
        Torsten Hahn (torstenhahn@fastmail.fm)
    '''
    
    implemented_properties = ['energy', 'forces']
    
    
    def __init__(self, restart=None, ignore_bad_restart_file=False, \
                label=os.curdir, atoms=None, **kwargs):
        
        Calculator.__init__(self, restart, ignore_bad_restart_file, \
                        label, atoms, **kwargs)
        valid_args = ('mf')
        
        
        # set any additional keyword arguments
        for arg, val in self.parameters.items():
            if arg in valid_args:
                setattr(self, arg, val)
            else:
                raise RuntimeError('unknown keyword arg "%s" : not in %s'
                                   % (arg, valid_args))
        #print("ESICC: {0}".format(atoms))
        self.atoms = atoms
        
        # find out machine precision
        # (this is pretty usefull for reliable numerics)
        #self.meps = np.finfo(np.float64).eps
        self.meps = 1e-12
        self.is_init = False
        
    def print_atoms(self):
        print('print_atoms', self.atoms)
        print(self.atoms.positions)
        return

    def get_forces(self, atoms=None):
        if atoms is not None:
            lpos = atoms.positions
        else:
            lpos = self.atoms.positions
        
        nspin = self.mf.nspin
        fposu = self.mf.fod1.positions
        nup = self.mf.fod1.positions.shape[0]
        fposd = self.mf.fod2.positions
        ndn = self.mf.fod2.positions.shape[0]
        fpos = np.zeros((nup+ndn,3), dtype=np.float64)
        fpos[:nup,:] = self.mf.fod1.positions[:,:]
        if self.mf.nspin == 2: fpos[nup:,:] = self.mf.fod2.positions[:,:]
        
        #print(fpos)
        pdiff = np.linalg.norm(lpos - fpos)
        #print('get_potential_energy, pdiff {}', pdiff, self.meps)
        if (pdiff > self.meps):
            self.mf.update_fpos(lpos)
            # update sic potential etupdate_fpos()
            self.mf.kernel(dm0=self.mf.make_rdm1())
        if not self.is_init:
            self.mf.kernel()
            self.is_init = True

        _ff = Ha/Bohr*self.mf.get_fforces()
        ##self.results['forces'][:,:] = self.FLO.
        return _ff



    def get_potential_energy(self, atoms=None, force_consistent=False):
        if atoms is not None:
            lpos = atoms.positions
        else:
            lpos = self.atoms.positions
        
        nspin = self.mf.nspin
        fposu = self.mf.fod1.positions
        nup = self.mf.fod1.positions.shape[0]
        fposd = self.mf.fod2.positions
        ndn = self.mf.fod2.positions.shape[0]
        fpos = np.zeros((nup+ndn,3), dtype=np.float64)
        fpos[:nup,:] = self.mf.fod1.positions[:,:]
        if self.mf.nspin == 2: fpos[nup:,:] = self.mf.fod2.positions[:,:]
        
        #print('>> lpos, fpos:', fpos.sum(), lpos.sum())
        pdiff = np.linalg.norm(lpos - fpos)
        #print('get_potential_energy, pdiff {}', pdiff, self.meps)
        if (pdiff > self.meps):
            self.mf.update_fpos(lpos)
            # update sic potential etupdate_fpos()
            self.mf.kernel(dm0=self.mf.make_rdm1())
        if not self.is_init:
            self.mf.kernel()
            self.is_init = True
        return self.mf.get_esic()*Ha


    def calculate(self, atoms=None, properties=['energy'], system_changes=all_changes):
        for p in properties:
            if p == 'energy':
                self.results['energy'] = self.get_potential_energy()
            elif p == 'forces':
                _ff = self.mf.get_fforces()
                self.results['forces'] = -units.Ha/units.Bohr*_ff.copy()
            else:
                raise PropertyNotImplementedError(\
                    'calculation of {} is not implemented'.format(p))



if __name__ == "__main__":
    from ase.io import read
    import os

    # Path to the xyz file 
    #f_xyz = os.path.dirname(os.path.realpath(__file__))+'/../examples/ase_pyflosic_optimizer/LiH.xyz' 
    #atoms = read(f_xyz)
    #calc = PYFLOSIC(atoms=atoms,charge=0,spin=0,xc='LDA,PW',basis='cc-pvqz')
    #print('Pyflosic total energy: ',calc.get_energy())
    #print('Pyflosic total forces: ',calc.get_forces())

    print('Testing basic optimizer')
    CH3SH = '''
    C -0.04795000 +1.14952000 +0.00000000
    S -0.04795000 -0.66486000 +0.00000000
    H +1.28308000 -0.82325000 +0.00000000
    H -1.09260000 +1.46143000 +0.00000000
    H +0.43225000 +1.55121000 +0.89226000
    H +0.43225000 +1.55121000 -0.89226000
    '''
    # this are the spin-up descriptors
    fod = Atoms('X13He13', positions=[
    (-0.04795000, -0.66486000, +0.00000000),
    (-0.04795000, +1.14952000, +0.00000000),
    (-1.01954312, +1.27662578, +0.00065565),
    (+1.01316012, -0.72796570, -0.00302478),
    (+0.41874165, +1.34380502, +0.80870475),
    (+0.42024357, +1.34411742, -0.81146545),
    (-0.46764078, -0.98842277, -0.72314717),
    (-0.46848962, -0.97040067, +0.72108036),
    (+0.01320210, +0.30892333, +0.00444147),
    (-0.28022018, -0.62888360, -0.03731204),
    (+0.05389371, -0.57381853, +0.19630494),
    (+0.09262866, -0.55485889, -0.15751914),
    (-0.05807583, -0.90413106, -0.00104673),
    ( -0.04795000, -0.66486000, +0.0000000),
    ( -0.04795000, +1.14952000, +0.0000000),
    ( +1.12523084, -0.68699049, +0.0301970),
    ( +0.40996981, +1.33508869, +0.8089839),
    ( +0.40987059, +1.34148952, -0.8106910),
    ( -0.49563876, -0.99517303, +0.6829207),
    ( -0.49640020, -0.89986161, -0.6743094),
    ( +0.00073876, +0.28757089, -0.0298617),
    ( -1.03186573, +1.29783767, -0.0035536),
    ( +0.04235081, -0.54885843, +0.1924678),
    ( +0.07365725, -0.59150454, -0.1951675),
    ( -0.28422422, -0.61466396, -0.0087913),
    ( -0.02352948, -1.0425011 ,+0.01253239)])
    
    fodup = Atoms([a for a in fod if a.symbol == 'X'])
    foddn = Atoms([a for a in fod if a.symbol == 'He'])
    print(fodup)
    print(foddn)
    
    from pyscf import gto, dft
    from flosic_scf import FLOSIC
    
    b = 'sto6g'
    spin = 0
    charge = 0
    
    mol = gto.M(atom=CH3SH, 
                basis=b,
                spin=spin,
                charge=charge)

    grid_level  = 5
    mol.verbose = 4
    mol.max_memory = 2000
    mol.build()
    xc = 'LDA,PW'
    
    
    # quick dft calculation
    mdft = dft.UKS(mol)
    mdft.xc = xc
    mdft.kernel()
    
    # build FLOSIC object
    mflosic = FLOSIC(mol, xc=xc,
        fod1=fodup,
        fod2=foddn,
        grid_level=grid_level, 
        init_dm=mdft.make_rdm1()
    )
    mflosic.max_cycle = 40
    mflosic.conv_tol = 1e-5  # just for testing
    
    calc = BasicFLOSICC(atoms=fod, mf=mflosic)
    #print(fod.get_potential_energy())
    #print(fod.get_forces())
    
    print(" >>>>>>>>>>>> OPTIMIZER TEST <<<<<<<<<<<<<<<<<<<<")
    
    # test the calculator by optimizing a bit
    from ase.optimize import  FIRE
    dyn = FIRE(atoms=fod,
        logfile='OPT_FRMORB_FIRE_TEST.log',
        #downhill_check=True,
        maxmove=0.05
    )
    dyn.run(fmax=0.005,steps=99)
    
    
    
    
    
