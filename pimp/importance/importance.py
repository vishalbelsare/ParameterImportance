import glob
import json
import logging
import os
import sys

import matplotlib

# Force matplotlib to not use any Xwindows backend.
matplotlib.use('Agg')

import numpy as np

from smac.utils.util_funcs import get_types
from smac.tae.execute_ta_run import StatusType
from smac.epm.rfr_imputator import RFRImputator
from smac.epm.rf_with_instances import RandomForestWithInstances

from pimp.configspace import CategoricalHyperparameter, Configuration, FloatHyperparameter, IntegerHyperparameter
from pimp.epm.unlogged_rf_with_instances import UnloggedRandomForestWithInstances
from pimp.evaluator.ablation import Ablation
from pimp.evaluator.fanova import fANOVA
from pimp.evaluator.forward_selection import ForwardSelector
from pimp.evaluator.influence_models import InfluenceModel
from pimp.utils import RunHistory, RunHistory2EPM4Cost, RunHistory2EPM4LogCost, Scenario, average_cost

__author__ = "Andre Biedenkapp"
__copyright__ = "Copyright 2016, ML4AAD"
__license__ = "3-clause BSD"
__maintainer__ = "Andre Biedenkapp"
__email__ = "biedenka@cs.uni-freiburg.de"


class Importance(object):
    """
    Importance Object. Handles the construction of the data and training of the model. Easy interface to the different
    evaluators
    """

    def __init__(self, scenario_file, runhistory_files, seed: int = 12345,
                 parameters_to_evaluate: int = -1, traj_file=None, threshold=None, margin=None,
                 save_folder='PIMP', impute_censored: bool=False):
        self.logger = logging.getLogger("Importance")
        self.impute = impute_censored
        self.logger.info('Reading Scenario file and files specified in the scenario')
        self.scenario = Scenario(scenario=scenario_file, cmd_args={'output_dir': save_folder}, run_id=1)

        self.logger.info('Reading Runhistory')
        self.runhistory = RunHistory(aggregate_func=average_cost)

        globed_files = glob.glob(runhistory_files)
        self.logger.info('#RunHistories found: %d' % len(globed_files))
        if not globed_files:
            self.logger.error('No runhistory files found!')
            sys.exit(1)
        self.runhistory.load_json(globed_files[0], self.scenario.cs)
        for rh_file in globed_files[1:]:
            self.runhistory.update_from_json(rh_file, self.scenario.cs)
        self.logger.info('Combined number of Runhistory data points: %d' % len(self.runhistory.data))
        self.seed = seed
        self.logger.info('Number of Configurations: %d' % (len(self.runhistory.get_all_configs())))

        self.logger.info('Converting Data and constructing Model')
        self.X = None
        self.y = None
        self.types = None
        self.bounds = None
        self._model = None
        self.incumbent = (None, None)
        self.logged_y = False
        self._convert_data()
        self._evaluator = None

        if traj_file is not None:
            self.incumbent = self._read_traj_file(traj_file)
            self.logger.debug('Incumbent %s' % str(self.incumbent))

        self.logger.info('Setting up Evaluation Method')
        self._parameters_to_evaluate = parameters_to_evaluate
        self.margin = margin
        self.threshold = threshold
        # self.evaluator = evaluation_method

    def _read_traj_file(self, fn):
        """
        Simple method to read in a trajectory file in the json format / aclib2 format
        :param fn:
            file name
        :return:
            tuple of (incumbent [Configuration], incumbent_cost [float])
        """
        if not (os.path.exists(fn) and os.path.isfile(fn)):  # File existence check
            raise FileNotFoundError('File %s not found!' % fn)
        with open(fn, 'r') as fh:
            for line in fh.readlines():
                pass
        line = line.strip()
        incumbent_dict = json.loads(line)
        inc_dict = {}
        for key_val in incumbent_dict['incumbent']:  # convert string to Configuration
            key, val = key_val.replace("'", '').split('=')
            if isinstance(self.scenario.cs.get_hyperparameter(key), (CategoricalHyperparameter)):
                inc_dict[key] = val
            elif isinstance(self.scenario.cs.get_hyperparameter(key), (FloatHyperparameter)):
                inc_dict[key] = float(val)
            elif isinstance(self.scenario.cs.get_hyperparameter(key), (IntegerHyperparameter)):
                inc_dict[key] = int(val)
        incumbent = Configuration(self.scenario.cs, inc_dict)
        incumbent_cost = incumbent_dict['cost']
        return incumbent, incumbent_cost

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model_short_name='urfi'):
        self.types, self.bounds = get_types(self.scenario.cs, self.scenario.feature_array)
        if model_short_name not in ['urfi', 'rfi']:
            raise ValueError('Specified model %s does not exist or not supported!' % model_short_name)
        elif model_short_name == 'rfi':
            self._model = RandomForestWithInstances(self.types, self.bounds,
                                                    instance_features=self.scenario.feature_array,
                                                    seed=self.seed, do_bootstrapping=True)
        elif model_short_name == 'urfi':
            self._model = UnloggedRandomForestWithInstances(self.types, self.bounds,
                                                            self.scenario.feature_array, seed=self.seed,
                                                            cutoff=self.cutoff, threshold=self.threshold,
                                                            do_bootstrapping=True)
        self._model.rf_opts.compute_oob_error = True

    @property
    def evaluator(self):
        return self._evaluator

    @evaluator.setter
    def evaluator(self, evaluation_method):
        if evaluation_method not in ['ablation', 'fanova', 'forward-selection', 'influence-model']:
            raise ValueError('Specified evaluation method %s does not exist!' % evaluation_method)
        if evaluation_method == 'ablation':
            if self.incumbent[0] is None:
                raise ValueError('Incumbent is %s!\n \
                                 Incumbent has to be read from a trajectory file before ablation can be used!'
                                 % self.incumbent[0])
            evaluator = Ablation(scenario=self.scenario,
                                 cs=self.scenario.cs,
                                 model=self._model,
                                 to_evaluate=self._parameters_to_evaluate,
                                 incumbent=self.incumbent[0],
                                 logy=self.logged_y,
                                 target_performance=self.incumbent[1])
        elif evaluation_method == 'influence-model':
            evaluator = InfluenceModel(scenario=self.scenario,
                                       cs=self.scenario.cs,
                                       model=self._model,
                                       to_evaluate=self._parameters_to_evaluate,
                                       margin=self.margin,
                                       threshold=self.threshold)
        elif evaluation_method == 'fanova':
            evaluator = fANOVA(scenario=self.scenario,
                               cs=self.scenario.cs,
                               model=self._model,
                               to_evaluate=self._parameters_to_evaluate,
                               runhist=self.runhistory)
        else:
            evaluator = ForwardSelector(scenario=self.scenario,
                                        cs=self.scenario.cs,
                                        model=self._model,
                                        to_evaluate=self._parameters_to_evaluate)
        self._evaluator = evaluator

    def _convert_data(self):  # From Marius
        '''
            converts data from runhistory into EPM format

            Parameters
            ----------
            scenario: Scenario
                smac.scenario.scenario.Scenario Object
            runhistory: RunHistory
                smac.runhistory.runhistory.RunHistory Object with all necessary data

            Returns
            -------
            np.array
                X matrix with configuartion x features for all observed samples
            np.array
                y matrix with all observations
            np.array
                types of X cols -- necessary to train our RF implementation
        '''

        params = self.scenario.cs.get_hyperparameters()
        num_params = len(params)

        if self.scenario.run_obj == "runtime":
            self.cutoff = self.scenario.cutoff
            self.threshold = self.scenario.cutoff * self.scenario.par_factor
            self.model = 'urfi'
            self.logged_y = True
            # if we log the performance data,
            # the RFRImputator will already get
            # log transform data from the runhistory
            cutoff = np.log10(self.scenario.cutoff)
            threshold = np.log10(self.scenario.cutoff *
                                 self.scenario.par_factor)
            model = RandomForestWithInstances(self.types, self.bounds,
                                              instance_features=self.scenario.feature_array,
                                              seed=self.seed, do_bootstrapping=True)

            imputor = RFRImputator(rs=np.random.RandomState(self.seed),
                                   cutoff=cutoff,
                                   threshold=threshold,
                                   model=model,
                                   change_threshold=0.01,
                                   max_iter=10)
            rh2EPM = RunHistory2EPM4LogCost(scenario=self.scenario,
                                            num_params=num_params,
                                            success_states=[
                                                StatusType.SUCCESS, ],
                                            impute_censored_data=self.impute,
                                            impute_state=[
                                                StatusType.TIMEOUT, StatusType.CAPPED],
                                            imputor=imputor)
        else:
            self.model = 'rfi'
            rh2EPM = RunHistory2EPM4Cost(scenario=self.scenario,
                                         num_params=num_params,
                                         success_states=None,
                                         impute_censored_data=self.impute,
                                         impute_state=None)
        X, Y = rh2EPM.transform(self.runhistory)

        self.X = X
        self.y = Y
        self.logger.info('Size of training X: %s' % str(self.X.shape))
        self.logger.info('Size of training y: %s' % str(self.y.shape))
        self.logger.info('Data was %s imputed' % ('not' if not self.impute else ''))
        if not self.impute:
            self.logger.info('Thus the size of X might be smaller than the datapoints in the RunHistory')
        self.model.train(X, Y)

    def evaluate_scenario(self, evaluation_method='all'):
        if evaluation_method == 'all':
            evaluators = []
            methods = ['ablation', 'influence-model', 'forward-selection', 'fanova']
            dict_ = {}
            for method in methods:
                self.evaluator = method
                dict_[method] = self.evaluator.run()
                evaluators.append(self.evaluator)
            return dict_, evaluators
        else:
            self.evaluator = evaluation_method
            self.logger.info('Running evaluation method %s' % self.evaluator.name)
            return {evaluation_method: self.evaluator.run()}

    def plot_results(self, name=None, evaluators=None):
        if evaluators:
            for eval, name_ in zip(evaluators, name):
                eval.plot_result(name_)
        else:
            self.evaluator.plot_result(name)
