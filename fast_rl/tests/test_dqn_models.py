from fastai.basic_train import LearnerCallback, DatasetType
from fastai.callback import Callback
from fastai.tabular import tabular_learner
from fastai.vision import cnn_learner, models

import numpy as np
from traitlets import List
from typing import Collection

from fast_rl.agents.BaseAgent import BaseAgent
from fast_rl.agents.DQN import DQN, FixedTargetDQN, DoubleDQN, DuelingDQN, DoubleDuelingDQN
from fast_rl.core.Interpreter import AgentInterpretationAlpha
from fast_rl.core.Learner import AgentLearner
from fast_rl.core.MarkovDecisionProcess import MDPDataBunch
from fast_rl.core.agent_core import GreedyEpsilon


def test_basic_dqn_model_maze():
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=200)
    model = DQN(data)
    learn = AgentLearner(data, model)

    learn.fit(5)


def test_fixed_target_dqn_model_maze():
    print('\n')
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=1000)
    model = FixedTargetDQN(data)
    learn = AgentLearner(data, model)

    learn.fit(5)


def test_fixed_target_dqn_no_explore_model_maze():
    print('\n')
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=1000, add_valid=False)
    model = FixedTargetDQN(data, lr=0.01, discount=0.8,
                           exploration_strategy=GreedyEpsilon(epsilon_start=0, epsilon_end=0,
                                                                               decay=0.001, do_exploration=False))
    learn = AgentLearner(data, model)

    learn.fit(5)


def test_double_dqn_model_maze():
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=1000)
    model = DoubleDQN(data)
    learn = AgentLearner(data, model)

    learn.fit(5)


def test_dueling_dqn_model_maze():
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=1000)
    model = DuelingDQN(data)
    learn = AgentLearner(data, model)

    learn.fit(5)


def test_double_dueling_dqn_model_maze():
    data = MDPDataBunch.from_env('maze-random-5x5-v0', render='human', max_steps=1000)
    model = DoubleDuelingDQN(data)
    learn = AgentLearner(data, model)

    learn.fit(5)
