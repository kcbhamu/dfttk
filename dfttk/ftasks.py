"""
Custom Firetasks for the DFTTK
"""
import subprocess
import os
import json
import numpy as np
import copy
import six
import shlex
from pymatgen import Structure
from pymatgen.io.vasp.inputs import Incar
from pymatgen.io.vasp.outputs import Vasprun
from custodian.custodian import Custodian
from custodian.vasp.handlers import VaspErrorHandler, AliasingErrorHandler, \
    MeshSymmetryErrorHandler, UnconvergedErrorHandler, PotimErrorHandler, \
    FrozenJobErrorHandler, NonConvergingErrorHandler, PositiveEnergyErrorHandler, \
    StdErrHandler, DriftErrorHandler
from custodian.vasp.jobs import VaspJob
from pymatgen.analysis.eos import Vinet, EOS
from fireworks import explicit_serialize, FiretaskBase, FWAction
from atomate.utils.utils import load_class, env_chk
from atomate.vasp.database import VaspCalcDb
from dfttk.analysis.phonon import get_f_vib_phonopy
from dfttk.analysis.relaxing import get_non_isotropic_strain, get_bond_distance_change
from dfttk.analysis.quasiharmonic import Quasiharmonic
from dfttk.utils import sort_x_by_y, update_pos_by_symbols, update_pot_by_symbols
from dfttk.custodian_jobs import ATATWalltimeHandler, ATATInfDetJob
from atomate import __version__ as atomate_ver
from dfttk import __version__ as dfttk_ver
from pymatgen import __version__ as pymatgen_ver


def extend_calc_locs(name, fw_spec):
    """
    Get the calc_locs from the FW spec and

    Parameters
    ----------
    name : str
        Name of the calc_loc
    fw_spec : dict
        Dictionary of the current Firework spec containing calc_locs.

    Returns
    -------
    list
        List of extended calc_locs
    """
    calc_locs = list(fw_spec.get("calc_locs", []))
    calc_locs.append({"name": name,
                      "filesystem": None,
                      "path": os.getcwd()})
    return calc_locs


@explicit_serialize
class WriteVaspFromIOSetPrevStructure(FiretaskBase):
    """
    Create VASP input files using implementations of pymatgen's VaspInputSet, overriding the
    Structure using a POSCAR in the current directory(in such case, donot provide structure as an input parameter). 
    An input set can be provided as an object or as a String/parameter combo.

    Required params:
        vasp_input_set (AbstractVaspInputSet or str): Either a VaspInputSet object or a string
            name for the VASP input set (e.g., "StaticSet").

    Optional params:
        vasp_input_params (dict): When using a string name for VASP input set, use this as a dict
            to specify kwargs for instantiating the input set parameters. For example, if you want
            to change the user_incar_settings, you should provide: {"user_incar_settings": ...}.
            This setting is ignored if you provide the full object representation of a VaspInputSet
            rather than a String.
        site_properties : dict
            Dictionary of {site_property: values} in pymatgen style. Will be applied if passed.
        structure : pymatgen.Structure
            If structure is passed, use it, otherwise using the POSCAR in current directory
    """

    required_params = ["vasp_input_set"]
    optional_params = ["vasp_input_params", "site_properties", "structure"]

    def run_task(self, fw_spec):
        struct = self.get("structure", None)
        if struct is None:
            struct = Structure.from_file('POSCAR')
        # if a full VaspInputSet object was provided
        if hasattr(self['vasp_input_set'], 'write_input'):
            vis = self['vasp_input_set']
            vis._structure = struct
        # if VaspInputSet String + parameters was provided
        else:
            vis_cls = load_class("pymatgen.io.vasp.sets", self["vasp_input_set"])
            vis = vis_cls(struct, **self.get("vasp_input_params", {}))
        # add site properties if they were added
        for prop, vals in self.get("site_properties", dict()).items():
            vis.structure.add_site_property(prop, vals)
        vis.write_input(".")
        #update_pos_by_symbols(vis)
        #update_pot_by_symbols(vis)


@explicit_serialize
class SupercellTransformation(FiretaskBase):
    """
    Transform a unitcell POSCAR to a supercell. Make a copy of the unitcell as a POSCAR-unitcell.

    This requires that a POSCAR is present in the current directory.
    """

    required_params = ['supercell_matrix']
    def run_task(self, fw_spec):
        unitcell = Structure.from_file('POSCAR')

        # create the unitcell file backup
        unitcell.to(filename='POSCAR-unitcell')

        # make the supercell and write to file
        unitcell.make_supercell(self['supercell_matrix'])
        unitcell.to(filename='POSCAR')


@explicit_serialize
class ScaleVolumeTransformation(FiretaskBase):
    """
    Scale the volume of a Structure (as a POSCAR) by a fraction and write to POSCAR.

    This requires that a POSCAR is present in the current directory, or pass the structure by structure parameter(optional).
    """

    required_params = ['scale_factor']
    optional_params = ["structure"]
    def run_task(self, fw_spec):
        struct = self.get("structure", None)
        if struct is None:
            struct = Structure.from_file('POSCAR')
        #cell = Structure.from_file('POSCAR')

        # create the unitcell file backup
        struct.to(filename='POSCAR.orig-volume')

        # make the supercell and write to file
        struct.scale_lattice(struct.volume*self['scale_factor'])
        struct.to(filename='POSCAR')


@explicit_serialize
class CheckSymmetry(FiretaskBase):
    """
    Check that the symmetry of

    Converts POSCAR to str.out and CONTCAR to str_relax.out and uses ATAT's checkrelax utility to check.
    """
    required_params = ['tolerance', 'db_file']
    optional_params = ['vasp_cmd', 'structure', 'metadata', 'name', 'modify_incar_params', 'modify_kpoints_params',
                       'run_isif2', 'pass_isif4']
    def run_task(self, fw_spec):
        # unrelaxed cell
        cell = Structure.from_file('POSCAR')
        cell.to(filename='str.out', fmt='mcsqs')

        # relaxed cell
        cell = Structure.from_file('CONTCAR')
        cell.to(filename='str_relax.out', fmt='mcsqs')

        # check the symmetry
        out = subprocess.run(['checkrelax', '-1'], stdout=subprocess.PIPE)
        relaxation = float(out.stdout)

        # we relax too much, add a volume relax and inflection detection WF as a detour
        if relaxation > self['tolerance']:
            from dfttk.fworks import OptimizeFW, InflectionDetectionFW
            from fireworks import Workflow
            from dfttk.input_sets import RelaxSet
            from dfttk.utils import add_modify_incar_by_FWname, add_modify_kpoints_by_FWname

            fws = []
            vis = RelaxSet(self.get('structure'), volume_relax=True)
            vol_relax_fw = OptimizeFW(self.get('structure'), symmetry_tolerance=None,
                                       job_type='normal', name='Volume relax', #record_path = True,
                                       vasp_input_set=vis, modify_incar = {'ISIF': 7},
                                       vasp_cmd=self.get('vasp_cmd'), db_file=self.get('db_file'),
                                       metadata=self.get('metadata'), run_isif2=self.get('run_isif2'),
                                       pass_isif4=self.get('pass_isif4')
                                      )
            fws.append(vol_relax_fw)

            modify_incar_params = self.get('modify_incar_params')
            modify_kpoints_params = self.get('modify_kpoints_params')

            # we have to add the calc locs for this calculation by hand
            # because the detour action seems to disable spec mods
            fws.append(InflectionDetectionFW(self.get('structure'), parents=[vol_relax_fw],
                                             run_isif2=self.get('run_isif2'), pass_isif4=self.get('pass_isif4'),
                                             metadata=self.get('metadata'), db_file=self.get('db_file'),
                                             spec={'calc_locs': extend_calc_locs(self.get('name', 'Full relax'), fw_spec)}))
            infdet_wf = Workflow(fws)
            add_modify_incar_by_FWname(infdet_wf, modify_incar_params = modify_incar_params)
            add_modify_kpoints_by_FWname(infdet_wf, modify_kpoints_params = modify_kpoints_params)
            return FWAction(detours=[infdet_wf])


@explicit_serialize
class CalculatePhononThermalProperties(FiretaskBase):
    """
    Phonon-related Firetask to calculate force constants and F_vib.

    This requires that a vasprun.xml from a force constants run and
    a POSCAR-unitcell be present in the current directory.
    """

    required_params = ['supercell_matrix', 't_min', 't_max', 't_step', 'db_file', 'tag']
    optional_params = ['metadata']

    def run_task(self, fw_spec):

        tag = self["tag"]
        metadata = self.get('metadata', {})
        metadata['tag'] = tag

        unitcell = Structure.from_file('POSCAR-unitcell')
        supercell_matrix = self['supercell_matrix']
        temperatures, f_vib, s_vib, cv_vib, force_constants = get_f_vib_phonopy(unitcell, supercell_matrix, vasprun_path='vasprun.xml', t_min=self['t_min'], t_max=self['t_max'], t_step=self['t_step'])
        if isinstance(supercell_matrix, np.ndarray):
            supercell_matrix = supercell_matrix.tolist()  # make serializable
        thermal_props_dict = {
            'volume': unitcell.volume,
            'F_vib': f_vib.tolist(),
            'CV_vib': cv_vib.tolist(),
            'S_vib': s_vib.tolist(),
            'temperatures': temperatures.tolist(),
            'force_constants': force_constants.tolist(),
            'metadata': metadata,
            'unitcell': unitcell.as_dict(),
            'supercell_matrix': supercell_matrix,
            'adopted' : True,
        }

        # insert into database
        db_file = env_chk(self["db_file"], fw_spec)
        vasp_db = VaspCalcDb.from_db_file(db_file, admin=True)
        vasp_db.db['phonon'].insert_one(thermal_props_dict)



@explicit_serialize
class QHAAnalysis(FiretaskBase):
    """
    Do the quasiharmonic calculation from either phonon or Debye.

    Required params
    ---------------
    tag : str
        Tag to search the database for static calculations (energies, volumes, eDOS) from this job.
    db_file : str
        Points to the database JSON file. If None (the default) is passed, the path will be looked up in the FWorker.
    phonon : bool
        True if f_vib comes from phonon calculations (in the spec). If False, it is calculated by the Debye model.
    t_min : float
        Minimum temperature
    t_step : float
        Temperature step size
    t_max : float
        Maximum temperature (inclusive)

    Optional params
    ---------------
    poisson : float
        Poisson ratio, defaults to 0.25. Only used in Debye
    bp2gru : float
        Debye model fitting parameter for dBdP in the Gruneisen parameter. 2/3 is the high temperature
        value and 1 is the low temperature value. Defaults to 1.

    Notes
    -----
    Heterogeneity in the sources of E_0/F_el and F_vib is solved by sorting them according to increasing volume.
    """

    required_params = ["phonon", "db_file", "t_min", "t_max", "t_step", "tag"]

    optional_params = ["poisson", "bp2gru", "metadata"]

    def run_task(self, fw_spec):
        # handle arguments and database setup
        db_file = env_chk(self.get("db_file"), fw_spec)
        tag = self["tag"]

        vasp_db = VaspCalcDb.from_db_file(db_file, admin=True)

        # get the energies, volumes and DOS objects by searching for the tag
        static_calculations = vasp_db.collection.find({'$and':[ {'metadata.tag': tag}, {'adopted': True} ]})

        energies = []
        volumes = []
        dos_objs = []  # pymatgen.electronic_structure.dos.Dos objects
        structure = None  # single Structure for QHA calculation
        for calc in static_calculations:
            energies.append(calc['output']['energy'])
            volumes.append(calc['output']['structure']['lattice']['volume'])
            dos_objs.append(vasp_db.get_dos(calc['task_id']))
            # get a Structure. We only need one for the masses and number of atoms in the unit cell.
            if structure is None:
                structure = Structure.from_dict(calc['output']['structure'])

        # sort everything in volume order
        # note that we are doing volume last because it is the thing we are sorting by!

        energies = sort_x_by_y(energies, volumes)
        dos_objs = sort_x_by_y(dos_objs, volumes)
        volumes = sorted(volumes)

        qha_result = {}
        qha_result['structure'] = structure.as_dict()
        qha_result['formula_pretty'] = structure.composition.reduced_formula
        qha_result['elements'] = sorted([el.name for el in structure.composition.elements])
        qha_result['metadata'] = self.get('metadata', {})
        qha_result['has_phonon'] = self['phonon']

        poisson = self.get('poisson', 0.363615)
        bp2gru = self.get('bp2gru', 1)

        # phonon properties
        if self['phonon']:
            # get the vibrational properties from the FW spec
            phonon_calculations = list(vasp_db.db['phonon'].find({'$and':[ {'metadata.tag': tag}, {'adopted': True} ]}))
            vol_vol = [calc['volume'] for calc in phonon_calculations]  # these are just used for sorting and will be thrown away
            vol_f_vib = [calc['F_vib'] for calc in phonon_calculations]
            # sort them order of the unit cell volumes
            vol_f_vib = sort_x_by_y(vol_f_vib, vol_vol)
            f_vib = np.vstack(vol_f_vib)
            qha = Quasiharmonic(energies, volumes, structure, dos_objects=dos_objs, F_vib=f_vib,
                                t_min=self['t_min'], t_max=self['t_max'], t_step=self['t_step'],
                                poisson=poisson, bp2gru=bp2gru)
            qha_result['phonon'] = qha.get_summary_dict()
            qha_result['phonon']['temperatures'] = qha_result['phonon']['temperatures'].tolist()

        # calculate the Debye model results no matter what
        qha_debye = Quasiharmonic(energies, volumes, structure, dos_objects=dos_objs, F_vib=None,
                                  t_min=self['t_min'], t_max=self['t_max'], t_step=self['t_step'],
                                  poisson=poisson, bp2gru=bp2gru)

        # fit 0 K EOS for good measure
        eos = Vinet(volumes, energies)
        eos.fit()
        errors = eos.func(volumes) - energies
        sum_square_error = float(np.sum(np.square(errors)))
        eos_res = {}
        eos_res['b0_GPa'] = float(eos.b0_GPa)
        eos_res['b0'] = float(eos.b0)
        eos_res['b1'] = float(eos.b1)
        eos_res['eq_volume'] = float(eos.v0)
        eos_res['eq_energy'] = float(eos.e0)
        eos_res['energies'] = energies
        eos_res['volumes'] = volumes
        eos_res['name'] = 'Vinet'
        eos_res['error'] = {}
        eos_res['error']['difference'] = errors.tolist()  # volume by volume differences
        eos_res['error']['sum_square_error'] = sum_square_error
        qha_result['eos'] = eos_res

        qha_result['debye'] = qha_debye.get_summary_dict()
        qha_result['debye']['poisson'] = poisson
        qha_result['debye']['bp2gru'] = bp2gru
        qha_result['debye']['temperatures'] = qha_result['debye']['temperatures'].tolist()

        qha_result['version_atomate'] = atomate_ver
        qha_result['version_dfttk'] = dfttk_ver
        volumes_false = []
        energies_false = []
        static_falses = vasp_db.collection.find({'$and':[ {'metadata.tag': tag}, {'adopted': False} ]})
        for static_false in static_falses:
            volumes_false.append(static_false['output']['structure']['lattice']['volume'])
            energies_false.append(static_false['output']['energy'])
        qha_result['Volumes_fitting_false'] = volumes_false
        qha_result['Energies_fitting_false'] = energies_false
        print('Volumes_fitting_false : %s' %volumes_false)
        print('Energies_fitting_false: %s' %energies_false)

        # write to JSON for debugging purposes
        import json
        with open('qha_summary.json', 'w') as fp:
            json.dump(qha_result, fp, indent=4)

        if self['phonon']:
            vasp_db.db['qha_phonon'].insert_one(qha_result)
        else:
            vasp_db.db['qha'].insert_one(qha_result)


@explicit_serialize
class EOSAnalysis(FiretaskBase):
    """
    Fit results from an E-V curve and enter the results the database

    Required params
    ---------------
    eos : str
        String name of the equation of state to use. See ``pymatgen.analysis.eos.EOS.MODELS`` for options.
    tag : str
        Tag to search the database for the volumetric calculations (energies, volumes) from this job.
    db_file : str
        Points to the database JSON file. If None (the default) is passed, the path will be looked up in the FWorker.

    Optional params
    ---------------
    metadata : dict
        Metadata about this workflow. Defaults to an empty dictionary

    """

    required_params = ["eos", "db_file", "tag"]
    optional_params = ["metadata", ]

    def run_task(self, fw_spec):
        db_file = env_chk(self.get("db_file"), fw_spec)
        tag = self["tag"]
        vasp_db = VaspCalcDb.from_db_file(db_file, admin=True)
        static_calculations = vasp_db.collection.find({"metadata.tag": tag})

        energies = []
        volumes = []
        structure = None  # single Structure for QHA calculation
        for calc in static_calculations:
            energies.append(calc['output']['energy'])
            volumes.append(calc['output']['structure']['lattice']['volume'])
            if structure is None:
                structure = Structure.from_dict(calc['output']['structure'])

        eos = EOS(self.get('eos'))
        ev_eos_fit = eos.fit(volumes, energies)
        equil_volume = ev_eos_fit.v0

        structure.scale_lattice(equil_volume)

        analysis_result = ev_eos_fit.results
        analysis_result['b0_GPa'] = float(ev_eos_fit.b0_GPa)
        analysis_result['structure'] = structure.as_dict()
        analysis_result['formula_pretty'] = structure.composition.reduced_formula
        analysis_result['metadata'] = self.get('metadata', {})
        analysis_result['energies'] = energies
        analysis_result['volumes'] = volumes


        # write to JSON for debugging purposes
        import json
        with open('eos_summary.json', 'w') as fp:
            json.dump(analysis_result, fp)

        vasp_db.db['eos'].insert_one(analysis_result)


@explicit_serialize
class TransmuteStructureFile(FiretaskBase):
    """
    Copy a file with the input_fname in the input_fmt format to an output_fname with an output_fmt format.

    Parameters
    ------
    input_fname : str
        Filename to read in as a Structure. Defaults to ``POSCAR``.
    output_fname : str
        Filename to write out as a Structure. Defaults to ``str.out``.
    input_fmt : str
        Format to read the input_fname. Defaults to ``POSCAR``.
    output_fmt : str
        Format to read the output_fname. Defaults to ``mcsqs``.
    """

    optional_params = ['input_fname', 'output_fname', 'input_fmt', 'output_fmt']
    def run_task(self, fw_spec):
        input_fname = self.get('input_fname', 'POSCAR')
        output_fname = self.get('output_fname', 'str.out')
        input_fmt = self.get('input_fmt', 'POSCAR')
        output_fmt = self.get('output_fmt', 'mcsqs')

        with open(input_fname) as fp:
            s = Structure.from_str(fp.read(), fmt=input_fmt)
        s.to(filename=output_fname, fmt=output_fmt)
        return FWAction()


@explicit_serialize
class WriteATATFromIOSet(FiretaskBase):
    """
    Write ATAT input as vaspid.wrap

    Parameters
    ------
    input_set : DictSet
        Input set that supports a ``write_input`` method.
    """

    required_params = ['input_set']
    def run_task(self, fw_spec):
        # To resolve input_set recoginized as str
        from dfttk.input_sets import ATATIDSet
        input_set = ATATIDSet(self['input_set'])
        input_set.write_input('.')

        return FWAction()


@explicit_serialize
class RunATATCustodian(FiretaskBase):
    """
    Run ATAT inflection detection with walltime handler.

    If the walltime handler is triggered, detour another InflectionDetection Firework.
    """

    optional_params = ['continuation', 'name']
    def run_task(self, fw_spec):
        continuation = self.get('continuation', False)
        # TODO: detour the firework pending the result
        c = Custodian([ATATWalltimeHandler()], [ATATInfDetJob(continuation=continuation)], monitor_freq=1, polling_time_step=300)
        cust_result = c.run()

        if len(cust_result[0]['corrections']) > 0:
            # we hit the walltime handler, detour another ID Firework
            os.remove('stop')
            from dfttk.fworks import InflectionDetectionFW
            from fireworks import Workflow
            # we have to add the calc locs for this calculation by hand
            # because the detour action seems to disable spec mods
            infdet_wf = Workflow([InflectionDetectionFW(Structure.from_file('POSCAR'), continuation=True, spec={'calc_locs': extend_calc_locs(self.get('name', 'InfDet'), fw_spec)})])
            return FWAction(detours=[infdet_wf])


@explicit_serialize
class RunVaspCustodianNoValidate(FiretaskBase):
    """
    Run VASP using custodian without validation, used in Phonon calcualations where xmls are fixed

    Required params:
        vasp_cmd (str): the name of the full executable for running VASP. Supports env_chk.

    Optional params:
        job_type: (str) - choose from "normal" (default), "double_relaxation_run" (two consecutive
            jobs), "full_opt_run" (multiple optimizations), and "neb"
        handler_group: (str) - group of handlers to use. See handler_groups dict in the code for
            the groups and complete list of handlers in each group.
        max_force_threshold: (float) - if >0, adds MaxForceErrorHandler. Not recommended for
            nscf runs.
        scratch_dir: (str) - if specified, uses this directory as the root scratch dir.
            Supports env_chk.
        gzip_output: (bool) - gzip output (default=T)
        max_errors: (int) - maximum # of errors to fix before giving up (default=5)
        ediffg: (float) shortcut for setting EDIFFG in special custodian jobs
        auto_npar: (bool) - use auto_npar (default=F). Recommended set to T
            for single-node jobs only. Supports env_chk.
        gamma_vasp_cmd: (str) - cmd for Gamma-optimized VASP compilation.
            Supports env_chk.
        wall_time (int): Total wall time in seconds. Activates WalltimeHandler if set.
        half_kpts_first_relax (bool): Use half the k-points for the first relaxation
    """
    required_params = ["vasp_cmd"]
    optional_params = ["job_type", "handler_group", "max_force_threshold", "scratch_dir",
                       "gzip_output", "max_errors", "ediffg", "auto_npar", "gamma_vasp_cmd",
                       "wall_time","half_kpts_first_relax"]

    def run_task(self, fw_spec):

        handler_groups = {
            "default": [VaspErrorHandler(), MeshSymmetryErrorHandler(), UnconvergedErrorHandler(),
                        NonConvergingErrorHandler(),PotimErrorHandler(),
                        PositiveEnergyErrorHandler(), FrozenJobErrorHandler(), StdErrHandler(),
                        DriftErrorHandler()],
            "strict": [VaspErrorHandler(), MeshSymmetryErrorHandler(), UnconvergedErrorHandler(),
                       NonConvergingErrorHandler(),PotimErrorHandler(),
                       PositiveEnergyErrorHandler(), FrozenJobErrorHandler(),
                       StdErrHandler(), AliasingErrorHandler(), DriftErrorHandler()],
            "md": [VaspErrorHandler(), NonConvergingErrorHandler()],
            "no_handler": []
            }

        vasp_cmd = env_chk(self["vasp_cmd"], fw_spec)

        if isinstance(vasp_cmd, six.string_types):
            vasp_cmd = os.path.expandvars(vasp_cmd)
            vasp_cmd = shlex.split(vasp_cmd)

        # initialize variables
        scratch_dir = env_chk(self.get("scratch_dir"), fw_spec)
        gzip_output = self.get("gzip_output", True)
        max_errors = self.get("max_errors", 5)
        auto_npar = env_chk(self.get("auto_npar"), fw_spec, strict=False, default=False)
        gamma_vasp_cmd = env_chk(self.get("gamma_vasp_cmd"), fw_spec, strict=False, default=None)

        jobs = [VaspJob(vasp_cmd, auto_npar=auto_npar, gamma_vasp_cmd=gamma_vasp_cmd)]

        # construct handlers
        handlers = handler_groups[self.get("handler_group", "default")]

        validators = []

        c = Custodian(handlers, jobs, validators=validators, max_errors=max_errors,
                      scratch_dir=scratch_dir, gzipped_output=gzip_output)

        c.run()


@explicit_serialize
class Record_relax_running_path(FiretaskBase):
    """
    To record relax running path for static calculation

    Required params:
    db_file : str
        Points to the database JSON file. If None (the default) is passed, the path will be looked up in the FWorker.

    """
    required_params = ["db_file", "metadata", 'run_isif2', 'pass_isif4']

    def run_task(self, fw_spec):
        db_file = env_chk(self["db_file"], fw_spec)
        vasp_db = VaspCalcDb.from_db_file(db_file, admin=True)
        content = {}
        content["metadata"] = self.get('metadata', {})
        content["path"] = fw_spec["calc_locs"][-1]["path"]
        content["run_isif2"] = self.get('run_isif2')
        content["pass_isif4"] = self.get('pass_isif4')
        vasp_db.db["relax"].insert_one(content)


@explicit_serialize
class Record_PreStatic_result(FiretaskBase):
    """
    To record relax running path for static calculation

    Required params:
    db_file : str
        Points to the database JSON file. If None (the default) is passed, the path will be looked up in the FWorker.

    """
    required_params = ["db_file", "metadata", "structure", "scale_lattice"]

    def run_task(self, fw_spec):
        from atomate.vasp.database import VaspCalcDb
        from pymatgen.io.vasp.outputs import Outcar
        outcar = Outcar('OUTCAR')
        db_file = env_chk(self.get('db_file'), fw_spec)
        vasp_db = VaspCalcDb.from_db_file(db_file, admin=True)
        content = {}
        content["metadata"] = self.get('metadata', {})
        structure = self.get('structure', {})
        content["structure"] = structure.as_dict()
        content["path"] = fw_spec["calc_locs"][-1]["path"]
        content["energy"] = outcar.final_energy
        content["scale_lattice"] = self.get('scale_lattice', 0)
        vasp_db.db["PreStatic"].insert_one(content)


@explicit_serialize
class empty_task(FiretaskBase):
    """
    The class used for generate a nothong todo task to avoid KeyError infw_spec["calc_locs"]
    """

    def run_task(self, fw_spec):
        pass


@explicit_serialize
class ModifyKpoints(FiretaskBase):
    """
    Modify an KPOINTS file.

    Required params:
        modify_kpoints_params: like [[3, 3, 3]]
    """

    required_params = ['modify_kpoints_params']

    def run_task(self, fw_spec):
        from pymatgen.io.vasp import Kpoints
        modify_kpoints_params = self.get('modify_kpoints_params')
        kpoint = Kpoints.from_file('KPOINTS')
        if 'kpts' in modify_kpoints_params.keys():
            kpoint.kpts = modify_kpoints_params['kpts']
        kpoint.write_file('KPOINTS')


@explicit_serialize
class CheckRelaxation(FiretaskBase):
    """Run VASP calculations to get symmetry conserved and symmetry broken structures.

    If symmetry never breaks, the result is successfully fully relaxed.

    Follow the following flow (assuming fixed volume):

                                          +--------+
                                          | ISIF 7 |
                                          |        |
                                          | Input  |
                                          +---+- --+
                                              |
                                              |
                                              v
                                          +---+----+
                                          | ISIF 5 |
                                          |        |
                                          | Shape  |
                                          | Only   |
                                          +-+----+-+
                                            |    |
                          +--------+        |    |       +--------+
                Pass      | ISIF 4 |  Pass  |    | Fail  | ISIF 2 |        Fail
             +------------+        +<-------+    +------>+        +--------------+
             |            | Shape  |                     | Ions   |              |
             |            | & Ions |                     | Only   |              |
             |            ----+----+                     +----+---+              |
             |                |                               |                  |
             |                |Fail                           |Pass              |
             |                |                               |                  |
             v                v                               v                  v
    +--------+------+  +------+--------------+  +-------------+--------+  +------+---------------+
    | Static        |  | Static              |  |  Static              |  |  Static              |
    |               |  |                     |  |                      |  |                      |
    | Fully relaxed |  | Constrained: ISIF 5 |  |  Constrained: ISIF 2 |  |  Constrained: ISIF 7 |
    |               |  | Broken     : ISIF 4 |  |  Broken     : ISIF 5 |  |  Broken     : ISIF 2 |
    +---------------+  +---------------------+  +----------------------+  +----------------------+

    Above diagram made with http://asciiflow.com

    """

    required_params = ["db_file", "tag", "common_kwargs"]
    optional_params = ["metadata", "tol_energy", "tol_strain", "tol_bond", "static_kwargs", "relax_kwargs"]

    def run_task(self, fw_spec):
        symm_check_data = self.check_symmetry()
        passed = symm_check_data["symmetry_checks_passed"]
        cur_isif = symm_check_data["isif"]
        next_steps = self.get_next_steps(passed, cur_isif)

        symm_check_data.update({
            "tag": self["tag"],
            "metadata": self.get("metadata"),
            "version_info": {
                "atomate": atomate_ver,
                "dfttk": dfttk_ver,
                "pymatgen": pymatgen_ver,
            },
            "next_steps": next_steps,
        })

        # write to JSON for debugging purposes
        with open('relaxation_check_summary.json', 'w') as fp:
            json.dump(symm_check_data, fp)

        self.db_file = env_chk(self.get("db_file"), fw_spec)
        vasp_db = VaspCalcDb.from_db_file(self.db_file, admin=True)
        vasp_db.db['relaxations'].insert_one(symm_check_data)

        return FWAction(detours=self.get_detour_workflow(next_steps, symm_check_data['final_energy_per_atom']))

    def check_symmetry(self):
        tol_energy = self.get("tol_energy", 0.025)
        tol_strain = self.get("tol_strain", 0.05)
        tol_bond = self.get("tol_bond", 0.10)

        # Get relevant files as pmg objects
        incar = Incar.from_file("INCAR")
        vasprun = Vasprun("vasprun.xml")
        inp_struct = Structure.from_file("POSCAR")
        out_struct = Structure.from_file("CONTCAR")

        current_isif = incar['ISIF']
        initial_energy = float(vasprun.ionic_steps[0]['e_wo_entrp'])/len(inp_struct)
        final_energy = float(vasprun.final_energy)/len(out_struct)

        # perform all symmetry breaking checks
        failures = []
        energy_difference = np.abs(final_energy - initial_energy)
        if energy_difference > tol_energy:
            fail_dict = {
                'reason': 'energy',
                'tolerance': tol_energy,
                'value': energy_difference,
            }
            failures.append(fail_dict)
        strain_norm = get_non_isotropic_strain(inp_struct.lattice.matrix, out_struct.lattice.matrix)
        if strain_norm > tol_strain:
            fail_dict = {
                'reason': 'strain',
                'tolerance': tol_strain,
                'value': strain_norm,
            }
            failures.append(fail_dict)
        bond_distance_change = get_bond_distance_change(inp_struct, out_struct)
        if bond_distance_change > tol_bond:
            fail_dict = {
                'reason': 'bond distance',
                'tolerance': tol_bond,
                'value': bond_distance_change,
            }
            failures.append(fail_dict)

        symm_data = {
            "initial_structure": inp_struct.as_dict(),
            "final_structure": out_struct.as_dict(),
            "isif": current_isif,
            "initial_energy_per_atom": initial_energy,
            "final_energy_per_atom": final_energy,
            "tolerances": {
                "energy": tol_energy,
                "strain": tol_strain,
                "bond": tol_bond,
            },
            "failures": failures,
            "number_of_failures": len(failures),
            "symmetry_checks_passed": len(failures) == 0,
        }
        return symm_data

    @staticmethod
    def get_next_steps(symmetry_checks_passed, current_isif):
        """Determine what to do next based on whether the checks passed and where we are at in the flowchart

        See the docstring for this class for reference to the flowchart.
        """
        next_steps = None
        # Relaxation passed
        if symmetry_checks_passed:
            if current_isif == 5:
                next_steps = [
                    {"job_type": "relax", "isif": 4, "structure": {"type": "final_structure", "isif": 5}},
                ]
            elif current_isif == 4:
                next_steps = [
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 4}, "symmetry_type": "constrained"}
                ]
            elif current_isif == 2:
                next_steps = [
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 2}, "symmetry_type": "constrained"},
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 5}, "symmetry_type": "broken"}
                ]
        # Relaxation failed
        else:
            if current_isif == 5:
                next_steps = [
                    {"job_type": "relax", "isif": 2, "structure": {"type": "initial_structure", "isif": 5}},
                ]
            elif current_isif == 4:
                next_steps = [
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 5}, "symmetry_type": "constrained"},
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 4}, "symmetry_type": "broken"}]
            elif current_isif == 2:
                next_steps = [
                    {"job_type": "static", "structure": {"type": "initial_structure", "isif": 5}, "symmetry_type": "constrained"},
                    {"job_type": "static", "structure": {"type": "final_structure", "isif": 2}, "symmetry_type": "broken"}]

        if next_steps is None:
            status = "passed" if symmetry_checks_passed else "failed"
            raise ValueError(f"Next steps not known for symmetry checks that {status} for ISIF={current_isif}")

        return next_steps

    def get_detour_workflow(self, next_steps, final_energy):
        # TODO: add all the necessary arguments and keyword arguments for the new Fireworks
        # TODO: add update metadata with the input metadata + the symmetry type for static
        # delayed imports to avoid circular import
        from fireworks import Workflow
        from .fworks import RobustOptimizeFW, StaticFW
        tol_energy = self.get("tol_energy", 0.025)
        tol_strain = self.get("tol_strain", 0.05)
        tol_bond = self.get("tol_bond", 0.10)
        symmetry_options = {"tol_energy": tol_energy, "tol_strain": tol_strain, "tol_bond": tol_bond}

        # Assume the data for the current step is already in the database
        db = VaspCalcDb.from_db_file(self.db_file, admin=True).db['relaxations']

        def _get_input_structure_for_step(step):
            # Get the structure of "type" from the "isif" step.
            relax_data = db.find_one({'$and': [{'tag': self["tag"]}, {'isif': step["structure"]["isif"]}]})
            return Structure.from_dict(relax_data[step["structure"]["type"]])

        detour_fws = []
        for step in next_steps:
            job_type = step["job_type"]
            inp_structure = _get_input_structure_for_step(step)
            if job_type == "static":
                common_copy = copy.deepcopy(self.get("common_kwargs", {}))
                md = common_copy.get("metadata", {})
                md['symmetry_type'] = step["symmetry_type"]
                common_copy["metadata"] = md
                detour_fws.append(StaticFW(inp_structure, **common_copy))
            elif job_type == "relax":
                detour_fws.append(RobustOptimizeFW(inp_structure, isif=step["isif"], override_symmetry_tolerances=symmetry_options, **self["common_kwargs"]))
            else:
                raise ValueError(f"Unknown job_type {job_type} for step {step}.")
        return detour_fws
