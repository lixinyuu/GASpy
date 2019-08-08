'''
This module analyzes our raw data and parses it into various metadata
'''

__authors__ = ['Zachary W. Ulissi', 'Kevin Tran']
__emails__ = ['zulissi@andrew.cmu.edu', 'ktran@andrew.cmu.edu']

import sys
import pickle
import numpy as np
import luigi
import statsmodels.api as statsmodels
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.core.surface import SlabGenerator
from .core import (schedule_tasks,
                   save_task_output,
                   make_task_output_object,
                   get_task_output)
from .calculation_finders import FindBulk, FindGas, FindAdslab, FindSurface
from ..mongo import make_atoms_from_doc
from .. import utils
from .. import defaults

GASDB_PATH = utils.read_rc('gasdb_path')
DFT_CALCULATOR = defaults.DFT_CALCULATOR
GAS_SETTINGS = defaults.gas_settings()
BULK_SETTINGS = defaults.bulk_settings()
SE_BULK_SETTINGS = defaults.surface_energy_bulk_settings()
SLAB_SETTINGS = defaults.slab_settings()
ADSLAB_SETTINGS = defaults.adslab_settings()
MAX_FIZZLES = defaults.MAX_FIZZLES


def submit_adsorption_calculations(adsorbate, catalog_docs, **kwargs):
    '''
    Light wrapper for submitting adsorption calculations given documents from
    the catalog. This should be used as a reference for beginners. Any advanced
    usage should be based on the code inside this function.

    Arg:
        adsorbate       A string indicating which adsorbate you want to submit
                        a calculation for. See `gaspy.defaults.adsorbates` for
                        possible values.
        catalog_docs    Any portion of the list of dictionaries obtained from
                        `gaspy.gasdb.get_catalog_docs` that you want to run.
        kwargs          If you want to override any arguments for the
                        `gaspy.tasks.metadata_calculators.CalculateAdsorptionEnergy`
                        task, then just supply them here. Note that if you
                        supply a value for a field that is inside one of the
                        dictionaries in the `site_docs` argument, the site
                        document will override whatever you provide. This will
                        prevent the user from trying to do calculations on
                        sites that are not real sites, which will probably mess
                        things up downstream.
    '''
    tasks = []

    # Take out the basic arguments from each site document
    for doc in catalog_docs:
        kwargs['adsorption_site'] = doc['adsorption_site']
        kwargs['mpid'] = doc['mpid']
        kwargs['miller_indices'] = doc['miller']
        kwargs['shift'] = doc['shift']
        kwargs['top'] = doc['top']

        # Create and submit the tasks/jobs
        task = CalculateAdsorptionEnergy(adsorbate_name=adsorbate, **kwargs)
        tasks.append(task)
    schedule_tasks(tasks)


def submit_rism_adsorption_calculations(adsorbate, catalog_docs, target_fermi,
                                        anion_concs, cation_concs, **kwargs):
    '''
    Wrapper for submitting RISM-type adsorption calculations given documents
    from the catalog. This should be used as a reference for beginners. Any
    advanced usage should be based on the code inside this function.

    Arg:
        adsorbate       A string indicating which adsorbate you want to submit
                        a calculation for. See `gaspy.defaults.adsorbates` for
                        possible values.
        catalog_docs    Any portion of the list of dictionaries obtained from
                        `gaspy.gasdb.get_catalog_docs` that you want to run.
        target_fermi    The Fermi level you want to set. This is your effective
                        knob for applied potential. What you supply here will
                        override the default target Fermi level in
                        `gaspy.defaults`.
        anion_concs     A dictionary whose keys are the anions you want in the
                        system and whose values are their concentrations in
                        units of mol/L. What you provide here will override the
                        default in `gaspy.defaults`.
        cation_concs    A dictionary whose keys are the cations you want in the
                        system and whose values are their concentrations in
                        units of mol/L. What you provide here will override the
                        default in `gaspy.defaults`.
        kwargs          If you want to override any arguments for the
                        `gaspy.tasks.metadata_calculators.CalculateAdsorptionEnergy`
                        task, then just supply them here. Note that if you
                        supply a value for a field that is inside one of the
                        dictionaries in the `site_docs` argument, the site
                        document will override whatever you provide. This will
                        prevent the user from trying to do calculations on
                        sites that are not real sites, which will probably mess
                        things up downstream.
    '''
    tasks = []

    # Take out the basic arguments from each site document
    for doc in catalog_docs:
        kwargs['adsorption_site'] = doc['adsorption_site']
        kwargs['mpid'] = doc['mpid']
        kwargs['miller_indices'] = doc['miller']
        kwargs['shift'] = doc['shift']
        kwargs['top'] = doc['top']

        # Define the RISM settings
        if 'gas_dft_settings' not in kwargs:
            kwargs['gas_dft_settings'] = GAS_SETTINGS['rism']
        if 'bulk_dft_settings' not in kwargs:
            kwargs['bulk_dft_settings'] = BULK_SETTINGS['rism']
        if 'bare_slab_dft_settings' not in kwargs:
            kwargs['bare_slab_dft_settings'] = ADSLAB_SETTINGS['rism']
        if 'adslab_dft_settings' not in kwargs:
            kwargs['adslab_dft_settings'] = ADSLAB_SETTINGS['rism']
        # Override default RISM settings with user-supplied ones
        for calculation_type in ['gas_dft_settings',
                                 'bare_slab_dft_settings',
                                 'adslab_dft_settings']:
            kwargs[calculation_type]['anion_concs'] = anion_concs
            kwargs[calculation_type]['cation_concs'] = cation_concs
            if calculation_type != 'gas_dft_settings':
                kwargs[calculation_type]['target_fermi'] = target_fermi

        # Create and submit the tasks/jobs
        task = CalculateAdsorptionEnergy(adsorbate_name=adsorbate, **kwargs)
        tasks.append(task)
    schedule_tasks(tasks)


class CalculateAdsorptionEnergy(luigi.Task):
    '''
    This task will calculate the adsorption energy of a system you specify.

    Args:
        adsorption_site         A 3-tuple of floats containing the Cartesian
                                coordinates of the adsorption site you want to
                                make a FW for
        shift                   A float indicating the shift of the slab
        top                     A Boolean indicating whether the adsorption
                                site is on the top or the bottom of the slab
        adsorbate_name          A string indicating which adsorbate to use. It
                                should be one of the keys within the
                                `gaspy.defaults.adsorbates` dictionary. If you
                                want an adsorbate that is not in the dictionary,
                                then you will need to add the adsorbate to that
                                dictionary.
        rotation                A dictionary containing the angles (in degrees)
                                in which to rotate the adsorbate after it is
                                placed at the adsorption site. The keys for
                                each of the angles are 'phi', 'theta', and
                                psi'.
        mpid                    A string indicating the Materials Project ID of
                                the bulk you want to enumerate sites from
        miller_indices          A 3-tuple containing the three Miller indices
                                of the slab[s] you want to enumerate sites from
        min_xy                  A float indicating the minimum width (in both
                                the x and y directions) of the slab (Angstroms)
                                before we enumerate adsorption sites on it.
        slab_generator_settings We use pymatgen's `SlabGenerator` class to
                                enumerate surfaces. You can feed the arguments
                                for that class here as a dictionary.
        get_slab_settings       We use the `get_slabs` method of pymatgen's
                                `SlabGenerator` class. You can feed the
                                arguments for the `get_slabs` method here
                                as a dictionary.
        gas_dft_settings        A dictionary containing the DFT settings of
                                the gas relaxation of the adsorbate
        bulk_dft_settings       A dictionary containing the DFT settings of
                                the relaxed bulk to enumerate slabs from
        bare_slab_dft_settings  A dictionary containing your DFT settings
                                for the bare slab relaxation
        adslab_dft_settings     A dictionary containing your DFT settings
                                for the adslab relaxation
        max_fizzles             The maximum number of times you want any single
                                DFT calculation to fail before giving up on this.
    Returns:
        doc A dictionary with the following keys:
                adsorption_energy   A float indicating the adsorption energy
                fwids               A subdictionary whose keys are 'adslab' and
                                    'slab', and whose values are the FireWork
                                    IDs of the respective calculations.
    '''
    adsorption_site = luigi.TupleParameter()
    shift = luigi.FloatParameter()
    top = luigi.BoolParameter()
    adsorbate_name = luigi.Parameter()
    rotation = luigi.DictParameter(ADSLAB_SETTINGS['rotation'])
    mpid = luigi.Parameter()
    miller_indices = luigi.TupleParameter()
    min_xy = luigi.FloatParameter(ADSLAB_SETTINGS['min_xy'])
    slab_generator_settings = luigi.DictParameter(SLAB_SETTINGS['slab_generator_settings'])
    get_slab_settings = luigi.DictParameter(SLAB_SETTINGS['get_slab_settings'])
    gas_dft_settings = luigi.DictParameter(GAS_SETTINGS[DFT_CALCULATOR])
    bulk_dft_settings = luigi.DictParameter(BULK_SETTINGS[DFT_CALCULATOR])
    bare_slab_dft_settings = luigi.DictParameter(ADSLAB_SETTINGS[DFT_CALCULATOR])
    adslab_dft_settings = luigi.DictParameter(ADSLAB_SETTINGS[DFT_CALCULATOR])
    max_fizzles = luigi.IntParameter(MAX_FIZZLES)

    def requires(self):
        return {'adsorbate_energy': CalculateAdsorbateEnergy(self.adsorbate_name,
                                                             self.gas_dft_settings,
                                                             max_fizzles=self.max_fizzles),
                'bare_slab_doc': FindAdslab(adsorption_site=(0., 0., 0.),
                                            shift=self.shift,
                                            top=self.top,
                                            dft_settings=self.bare_slab_dft_settings,
                                            adsorbate_name='',
                                            rotation={'phi': 0., 'theta': 0., 'psi': 0.},
                                            mpid=self.mpid,
                                            miller_indices=self.miller_indices,
                                            min_xy=self.min_xy,
                                            slab_generator_settings=self.slab_generator_settings,
                                            get_slab_settings=self.get_slab_settings,
                                            bulk_dft_settings=self.bulk_dft_settings,
                                            max_fizzles=self.max_fizzles),
                'adslab_doc': FindAdslab(adsorption_site=self.adsorption_site,
                                         shift=self.shift,
                                         top=self.top,
                                         dft_settings=self.adslab_dft_settings,
                                         adsorbate_name=self.adsorbate_name,
                                         rotation=self.rotation,
                                         mpid=self.mpid,
                                         miller_indices=self.miller_indices,
                                         min_xy=self.min_xy,
                                         slab_generator_settings=self.slab_generator_settings,
                                         get_slab_settings=self.get_slab_settings,
                                         bulk_dft_settings=self.bulk_dft_settings,
                                         max_fizzles=self.max_fizzles)}

    def run(self):
        with open(self.input()['adsorbate_energy'].path, 'rb') as file_handle:
            ads_energy = pickle.load(file_handle)

        with open(self.input()['bare_slab_doc'].path, 'rb') as file_handle:
            slab_doc = pickle.load(file_handle)
        slab_atoms = make_atoms_from_doc(slab_doc)
        slab_energy = slab_atoms.get_potential_energy(apply_constraint=False)

        with open(self.input()['adslab_doc'].path, 'rb') as file_handle:
            adslab_doc = pickle.load(file_handle)
        adslab_atoms = make_atoms_from_doc(adslab_doc)
        adslab_energy = adslab_atoms.get_potential_energy(apply_constraint=False)

        adsorption_energy = adslab_energy - slab_energy - ads_energy
        doc = {'adsorption_energy': adsorption_energy,
               'fwids': {'adslab': adslab_doc['fwid'],
                         'slab': slab_doc['fwid']}}
        save_task_output(self, doc)

    def output(self):
        return make_task_output_object(self)


class CalculateAdsorbateEnergy(luigi.Task):
    '''
    This task will calculate the energy of an adsorbate via algebraic
    summation/subtraction of a basis set of energies.

    Arg:
        adsorbate_name  A string indicating the name of the adsorbate you
                        want to calculate the energy of
        dft_settings    A dictionary containing the DFT settings you want to
                        use for the DFT relaxations of the basis set
        max_fizzles     The maximum number of times you want any single
                        DFT calculation to fail before giving up on this.
    Returns:
        energy  The DFT-calculated energy of the adsorbate
    '''
    adsorbate_name = luigi.Parameter()
    dft_settings = luigi.DictParameter(GAS_SETTINGS[DFT_CALCULATOR])
    max_fizzles = luigi.IntParameter(MAX_FIZZLES)

    def requires(self):
        return CalculateAdsorbateBasisEnergies(self.dft_settings)

    def run(self):
        with open(self.input().path, 'rb') as file_handle:
            basis_energies = pickle.load(file_handle)

        # Fetch the adsorbate from our dictionary. If it's not there, yell
        try:
            adsorbate = defaults.adsorbates()[self.adsorbate_name]
        except KeyError as error:
            raise type(error)('You are trying to calculate the adsorbate energy '
                              'of an undefined adsorbate, %s. Please define the '
                              'adsorbate within `gaspy.defaults.adsorbates' %
                              self.adsorbate_name).with_traceback(sys.exc_info()[2])

        energy = sum(basis_energies[atom] for atom in adsorbate.get_chemical_symbols())
        save_task_output(self, energy)

    def output(self):
        return make_task_output_object(self)


class CalculateAdsorbateBasisEnergies(luigi.Task):
    '''
    When calculating adsorption energies, we first need the energy of the
    adsorbate. Sometimes the adsorbate does not exist in the gas phase, so we
    can't get the DFT energy. To address this, we can actually calculate the
    adsorbate energy as a sum of basis energies for each atom in the adsorbate.
    For example:  `E(CH3OH)` can be calculated by adding `3*E(C) + 4*E(H) +
    1*E(O)`. To get the energies of the single atoms, we can relax normal gases
    and perform similar algebra, e.g., `E(H) = E(H2)/2` or `E(O) = E(H2O) -
    E(H2)`. This task will calculate the basis energies for H, O, C, and N for
    you so that you can use these energies in other calculations.

    Arg:
        dft_settings    A dictionary containing the DFT settings you want to
                        use for the DFT relaxations of the gases.
        max_fizzles     The maximum number of times you want any single
                        DFT calculation to fail before giving up on this.
    Returns:
        basis_energies  A dictionary whose keys are the basis elements and
                        whose values are their respective energies, e.g.,
                        {'H': foo, 'O': bar}
    '''
    dft_settings = luigi.DictParameter(GAS_SETTINGS[DFT_CALCULATOR])
    max_fizzles = luigi.IntParameter(MAX_FIZZLES)

    def requires(self):
        return {'CO': FindGas(gas_name='CO', dft_settings=self.dft_settings,
                              max_fizzles=self.max_fizzles),
                'H2': FindGas(gas_name='H2', dft_settings=self.dft_settings,
                              max_fizzles=self.max_fizzles),
                'H2O': FindGas(gas_name='H2O', dft_settings=self.dft_settings,
                               max_fizzles=self.max_fizzles),
                'N2': FindGas(gas_name='N2', dft_settings=self.dft_settings,
                              max_fizzles=self.max_fizzles)}

    def run(self):
        # Load each gas and calculate their energies
        gas_energies = dict.fromkeys(self.input())
        for adsorbate_name, target in self.input().items():
            with open(target.path, 'rb') as file_handle:
                doc = pickle.load(file_handle)
            atoms = make_atoms_from_doc(doc)
            gas_energies[adsorbate_name] = atoms.get_potential_energy(apply_constraint=False)

        # Calculate and save the basis energies from the gas phase energies
        basis_energies = {'H': gas_energies['H2']/2.,
                          'O': gas_energies['H2O'] - gas_energies['H2'],
                          'C': gas_energies['CO'] - (gas_energies['H2O']-gas_energies['H2']),
                          'N': gas_energies['N2']/2.}
        save_task_output(self, basis_energies)

    def output(self):
        return make_task_output_object(self)


class CalculateSurfaceEnergy(luigi.Task):
    '''
    Calculate the surface energy of a slab

    Args:
        mpid                    A string indicating the Materials Project ID of
                                the bulk you want to get a surface from
        miller_indices          A 3-tuple containing the three Miller indices
                                of the surface you want to find
        shift                   A float indicating the shift of the
                                surface---i.e., the termination that pymatgen
                                finds
        max_atoms               The maximum number of atoms you're willing to
                                run a DFT calculation on
        slab_generator_settings We use pymatgen's `SlabGenerator` class to
                                enumerate surfaces. You can feed the arguments
                                for that class here as a dictionary.
        get_slab_settings       We use the `get_slabs` method of pymatgen's
                                `SlabGenerator` class. You can feed the
                                arguments for the `get_slabs` method here
                                as a dictionary.
        dft_settings            A dictionary containing your DFT settings for
                                the surface relaxation
        bulk_dft_settings       A dictionary containing the DFT settings of
                                the relaxed bulk to enumerate surfaces from
        max_fizzles             The maximum number of times you want any single
                                DFT calculation to fail before giving up on this.
    Returns::
        doc A dictionary with the following keys:
                surface_structures              A list of three dictionaries
                                                for each of the surfaces. These
                                                dictionaries are the documents
                                                found in the `atoms` collection
                                                `gaspy.mongo.make_doc_from_atoms`.
                surface_energy                  A float indicating the surface
                                                energy in eV/Angstrom**2
                surface_energy_standard_error   A float indicating the standard
                                                error of our estimate of the
                                                surface energy
    '''
    mpid = luigi.Parameter()
    miller_indices = luigi.TupleParameter()
    shift = luigi.FloatParameter()
    max_atoms = luigi.IntParameter(SLAB_SETTINGS['max_atoms'])
    slab_generator_settings = luigi.DictParameter(SLAB_SETTINGS['slab_generator_settings'])
    get_slab_settings = luigi.DictParameter(SLAB_SETTINGS['get_slab_settings'])
    dft_settings = luigi.DictParameter(SLAB_SETTINGS[DFT_CALCULATOR])
    bulk_dft_settings = luigi.DictParameter(SE_BULK_SETTINGS[DFT_CALCULATOR])
    max_fizzles = luigi.IntParameter(MAX_FIZZLES)

    def _static_requires(self):
        '''
        We have both static and dynamic depenencies, and Luigi expects us to
        yield them in the `run` method. We put the code for the static
        depenenices here in `_static_requires` for organizational purposes, and
        then just call it first in `run`.
        '''
        # Define our static dependency, the bulk relaxation
        find_bulk_task = FindBulk(mpid=self.mpid,
                                  dft_settings=self.bulk_dft_settings,
                                  max_fizzles=self.max_fizzles)

        # If our dependency is done, then save the relaxed bulk atoms object as
        # an attribute for use by the other methods
        try:
            bulk_doc = get_task_output(find_bulk_task)
            self.bulk_atoms = make_atoms_from_doc(bulk_doc)

            # Let's not calculate the surface energy if it requires us to relax a
            # slab that's too big
            self.__terminate_if_too_large()

        # If the dependency is not done, then Luigi expects us to yield/return
        # the dependency task anyway
        except FileNotFoundError:
            pass
        return find_bulk_task

    def __terminate_if_too_large(self):
        '''
        Surface energy calculations require us to relax three slabs of varying
        thicknesses. If the thickest slab is too big, then we shouldn't bother
        with the other two. This method will figure that out for us and return
        an error if this happens.

        Saved Attribute:
            min_repeats A integer for the minimum number of times we need to
                        repeat a unit slab in order for the height to exceed
                        the minimum slab height
        '''
        self.__calculate_unit_slab()

        # Calculate how many atoms are in the biggest slab/surface that we'd
        # need to calculate
        min_slab_size = self.slab_generator_settings['min_slab_size']
        min_unit_slab_repeats = int(np.ceil(min_slab_size/self.unit_slab_height))
        max_slab_size = (min_unit_slab_repeats+2) * len(self.unit_slab)

        # Throw an error if it's too big
        if max_slab_size > self.max_atoms:
            raise RuntimeError('Cannot calculate surface energy of (%s, %s, %s) '
                               'because we would need to perform a relaxation on '
                               'a slab with %i atoms, which exceeds the limit of '
                               '%i. Increase the `max_atoms` argument if you '
                               'really want to run it anyway.'
                               % (self.mpid, self.miller_indices, self.shift,
                                  max_slab_size, self.max_atoms))

        self.min_repeats = min_unit_slab_repeats

    def __calculate_unit_slab(self):
        '''
        Calculates the height of the smallest unit slab from a given bulk and
        Miller cut

        Saved attributes:
            unit                A `pymatgen.Structure` instance for the unit
                                slab
            unit_slab_height    The height of the unit slab in Angstroms
        '''
        # Luigi will probably call this method multiple times. We only need to
        # do it once though.
        if not hasattr(self, 'unit_slab_height'):

            # Delete some slab generator settings that we don't care about for a
            # unit slab
            slab_generator_settings = utils.unfreeze_dict(self.slab_generator_settings)
            del slab_generator_settings['min_vacuum_size']
            del slab_generator_settings['min_slab_size']

            # Instantiate a pymatgen `SlabGenerator`
            bulk_structure = AseAtomsAdaptor.get_structure(self.bulk_atoms)
            sga = SpacegroupAnalyzer(bulk_structure, symprec=0.1)
            bulk_structure = sga.get_conventional_standard_structure()
            gen = SlabGenerator(initial_structure=bulk_structure,
                                miller_index=self.miller_indices,
                                min_vacuum_size=0.,
                                min_slab_size=1.,
                                **slab_generator_settings)

            # Generate the unit slab and find its height
            self.unit_slab = gen.get_slab(self.shift, tol=self.get_slab_settings['tol'])
            self.unit_slab_height = gen._proj_height

    def _dynamic_requires(self):
        '''
        We have both static and dynamic depenencies, and Luigi expects us to
        yield them in the `run` method. We put the code for the dynamic
        depenenices here in `_dynamic_requires` for organizational purposes, and
        then just call it first in `run`.
        '''
        # For some reason, Luigi might not run the `_static_requires` before
        # running this method. If this happens, then call it manually. This
        # ensures that some of the correct attributes are assigned.
        try:
            min_repeats = self.min_repeats
        except AttributeError:
            _ = self._static_requires()  # noqa: F841
            min_repeats = self.min_repeats

        # Calculate the height of each slab
        surface_relaxation_tasks = []
        for n_repeats in range(min_repeats, min_repeats+3):
            min_height = n_repeats * self.unit_slab_height

            # Instantiate and return each task
            task = FindSurface(mpid=self.mpid,
                               miller_indices=self.miller_indices,
                               shift=self.shift,
                               min_height=min_height,
                               dft_settings=self.dft_settings,
                               bulk_dft_settings=self.bulk_dft_settings,
                               max_fizzles=self.max_fizzles)
            surface_relaxation_tasks.append(task)

        # Save these tasks as an attribute so we can use the actual tasks later.
        # We do this because we need to hack Luigi by using
        # `gaspy.tasks.core.run_task` instead of
        # `gaspy.tasks.core.schedule_tasks`, but `run_task` doesn't play well
        # with dynamic dependencies.
        self.surface_relaxation_tasks = surface_relaxation_tasks

        # Need to return/yield it anyway for Luigi's sake
        return surface_relaxation_tasks

    def run(self):
        '''
        We have both static and dynamic dependencies. Luigi only accepts
        dynamic ones though, so we treat our static dependency (bulk
        relaxation) as a "first dynamic depenency".

        After we finish the dependencies, we then calculate the surface energy.
        '''
        # Run the dependencies first
        yield self._static_requires()
        yield self._dynamic_requires()

        # Fetch the results of the surface relaxations
        surface_docs = []
        for task in self.surface_relaxation_tasks:
            with open(task.output().path, 'rb') as file_handle:
                surface_doc = pickle.load(file_handle)
            surface_docs.append(surface_doc)

        # Use the results of the surface relaxations to calculate the surface
        # energy
        surface_energy, surface_energy_se = self._calculate_surface_energy(surface_docs)

        # Parse the results into a document to save
        doc = {'surface_structures': surface_docs}
        doc['surface_energy'] = surface_energy
        doc['surface_energy_standard_error'] = surface_energy_se
        save_task_output(self, doc)

    def _calculate_surface_energy(self, docs):
        '''
        Given three Mongo documents/dictionaries for each of the surfaces,
        calculates the surface energy by performing a linear regression on the
        energies of slabs with varying numbers of atoms, and then extrapolating
        that linear model down to zero atoms.

        Arg:
            docs    A list of Mongo documents/dictionaries that can be turned
                    into `ase.Atoms` objects via
                    `gaspy.mongo.make_atoms_from_doc`. These documents should
                    be created from relaxed atoms objects.
        Returns:
            surface_energy                  The surface energy prediction
                                            (eV/Angstrom**2)
            surface_energy_standard_error   The standard error of our estimate
                                            on the surface energy
                                            (eV/Angstrom**2)
        '''
        # Load each surface
        atoms_list = [make_atoms_from_doc(doc) for doc in docs]

        # Count the number of atoms in each surface
        n_atoms = [len(atoms) for atoms in atoms_list]

        # Pull the energies from each slab
        slab_energies = [atoms.get_potential_energy() for atoms in atoms_list]
        # We multiply the area by two because there's a top and bottom side of
        # each slab
        area = 2 * np.linalg.norm(np.cross(atoms_list[0].cell[0], atoms_list[0].cell[1]))
        slab_energies_per_area = slab_energies/area

        # Perform the linear regression to get the surface energy and uncertainty
        data = statsmodels.add_constant(n_atoms)
        mod = statsmodels.OLS(slab_energies_per_area, data)
        res = mod.fit()
        surface_energy = res.params[0]
        surface_energy_standard_error = res.bse[0]
        return surface_energy, surface_energy_standard_error

    def output(self):
        return make_task_output_object(self)
