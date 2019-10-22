import copy
from copy import deepcopy
from functools import partial

import numpy as np
import torch
from fastai.basic_train import LearnerCallback, Any, F, OptimWrapper, ifnone
from torch import optim, nn

from fast_rl.agents.BaseAgent import BaseAgent, create_nn_model, create_cnn_model, ToLong, get_embedded, Flatten
from fast_rl.core.MarkovDecisionProcess import MDPDataBunchAlpha, FEED_TYPE_IMAGE, MDPDataBunch, MDPDataset, State, \
    Action
from fast_rl.core.agent_core import ExperienceReplay, GreedyEpsilon


class BaseDQNCallback(LearnerCallback):
    def __init__(self, learn, max_episodes=None):
        """Handles basic DQN end of step model optimization."""
        super().__init__(learn)
        self.n_skipped = 0
        self._persist = max_episodes is not None
        self.max_episodes = max_episodes
        self.episode = -1
        self.iteration = 0
        # For the callback handler
        self._order = 0
        self.previous_item = None

    def on_train_begin(self, n_epochs, **kwargs: Any):
        self.max_episodes = n_epochs if not self._persist else self.max_episodes

    def on_epoch_begin(self, epoch, **kwargs: Any):
        self.episode = epoch if not self._persist else self.episode + 1
        self.iteration = 0

    def on_loss_begin(self, **kwargs: Any):
        """Performs memory updates, exploration updates, and model optimization."""
        if self.learn.model.training and self.previous_item is not None:
            if self.learn.data.x.items[-2].done: self.previous_item.done = self.learn.data.x.items[-2].done
            self.learn.model.memory.update(item=self.previous_item)
        self.previous_item = copy.deepcopy(self.learn.data.x.items[-1])
        self.learn.model.exploration_strategy.update(self.episode, max_episodes=self.max_episodes,
                                                     do_exploration=self.learn.model.training)
        post_optimize = self.learn.model.optimize()
        if self.learn.model.training: self.learn.model.memory.refresh(post_optimize=post_optimize)
        self.iteration += 1




class FixedTargetDQNCallback(LearnerCallback):
    def __init__(self, learn, copy_over_frequency=3):
        """Handles updating the target model in a fixed target DQN.

        Args:
            learn: Basic Learner.
            copy_over_frequency: Per how many episodes we want to update the target model.
        """
        super().__init__(learn)
        self._order = 1
        self.iteration = 0
        self.copy_over_frequency = copy_over_frequency

    def on_step_end(self, **kwargs: Any):
        self.iteration += 1
        if self.iteration % self.copy_over_frequency == 0 and self.learn.model.training:
            self.learn.model.target_copy_over()


class DQNActionNN(nn.Module):
    def __init__(self, layers, action: Action, state: State, activation=nn.ReLU, embed=False):
        super().__init__()

        module_layers = []
        for i, size in enumerate(layers):
            if i == 0:
                if embed:
                    embedded, out = get_embedded(state.s.shape[1], size, state.n_possible_values, 5)
                    module_layers += [ToLong(), embedded, Flatten(), nn.Linear(out, size)]
                else:
                    module_layers.append(nn.Linear(state.s.shape[1], size))
            else:
                module_layers.append(nn.Linear(layers[i-1], size))
            module_layers.append(activation())

        module_layers.append(nn.Linear(layers[-1], action.n_possible_values))
        self.model = nn.Sequential(*module_layers)

    def forward(self, x, **kwargs: Any):
        return self.model(x)


class DQN(BaseAgent):
    def __init__(self, data: MDPDataBunch, memory=None, lr=0.01, discount=0.95, grad_clip=5,
                 max_episodes=None, exploration_strategy=None, use_embeddings=False):
        """Trains an Agent using the Q Learning method on a neural net.

        Notes:
            This is not a true implementation of [1]. A true implementation uses a fixed target network.

        References:
            [1] Mnih, Volodymyr, et al. "Playing atari with deep reinforcement learning."
            arXiv preprint arXiv:1312.5602 (2013).

        Args:
            data: Used for size input / output information.
        """
        super().__init__(data)
        # TODO add recommend cnn based on s size?
        self.name = 'DQN'
        self.use_embeddings = use_embeddings
        self.batch_size = data.train_ds.bs
        self.discount = discount
        self.warming_up = True
        self.lr = lr
        self.gradient_clipping_norm = grad_clip
        self.loss_func = F.mse_loss
        self.memory = ifnone(memory, ExperienceReplay(10000))
        self.action_model = self.initialize_action_model([24, 24], data.train_ds)
        self.opt = OptimWrapper.create(optim.Adam, lr=self.lr, layer_groups=[self.action_model])
        self.learner_callbacks += [partial(BaseDQNCallback, max_episodes=max_episodes)] + self.memory.callbacks
        self.exploration_strategy = ifnone(exploration_strategy, GreedyEpsilon(epsilon_start=1, epsilon_end=0.1,
                                                                               decay=0.001,
                                                                               do_exploration=self.training))

    def init_weights(self, m):
        if type(m) == nn.Linear:
            torch.nn.init.xavier_uniform_(m.weight)
            m.bias.data.fill_(0.01)

    def initialize_action_model(self, layers, data: MDPDataset):
        # if self.data.train_ds.feed_type == FEED_TYPE_IMAGE: model = create_cnn_model(layers, *data.get_action_state_size(), action_val_to_dim=True)
        # else: model = create_nn_model(layers, *data.get_action_state_size(), use_embed=False, action_val_to_dim=True)
        model = DQNActionNN(layers, data.action, data.state, embed=self.use_embeddings)  # type: nn.Module

        model.apply(self.init_weights)
        return model

    def forward(self, x):
        x = super(DQN, self).forward(x)
        return self.action_model(x)

    def optimize(self):
        r"""Uses ER to optimize the Q-net (without fixed targets).
        
        Uses the equation:

        .. math::
                Q^{*}(s, a) = \mathbb{E}_{s'∼ \Big\epsilon} \Big[r + \lambda \displaystyle\max_{a'}(Q^{*}(s' , a'))
                \;|\; s, a \Big]

        
        Returns (dict): Optimization information

        """
        if len(self.memory) > self.batch_size:
            self.warming_up = False
            # Perhaps have memory as another itemlist? Should investigate.
            sampled = self.memory.sample(self.batch_size)
            with torch.no_grad():
                r = torch.cat([item.reward for item in sampled]).float()
                s_prime = torch.cat([item.s_prime for item in sampled]).float()
                s = torch.cat([item.s for item in sampled]).float()
                a = torch.cat([item.a for item in sampled]).long()
                d = torch.cat([item.done for item in sampled]).float()

            masking = torch.sub(1.0, d)
            # Traditional `maze-random-5x5-v0` with have a model output a Nx4 output.
            # since r is just Nx1, we spread the reward into the actions.
            y_hat = self.action_model(s).gather(1, a)
            y = self.discount * self.action_model(s_prime).max(axis=1)[0].unsqueeze(1) * masking + r.expand_as(y_hat)

            loss = self.loss_func(y, y_hat)

            if self.training:
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.action_model.parameters(), self.gradient_clipping_norm)
                for param in self.action_model.parameters():
                    param.grad.data.clamp_(-1, 1)
                self.opt.step()

            with torch.no_grad():
                self.loss = loss
                post_info = {'td_error': (y - y_hat).numpy()}
                return post_info

    def interpret_q(self, items):
        with torch.no_grad():
            r = torch.from_numpy(np.array([item.reward for item in items])).float()
            s_prime = torch.from_numpy(np.array([item.result_state for item in items])).float()
            s = torch.from_numpy(np.array([item.current_state for item in items])).float()
            a = torch.from_numpy(np.array([item.actions for item in items])).long()

            return self.action_model(s).gather(1, a)


class FixedTargetDQN(DQN):
    def __init__(self, data: MDPDataBunchAlpha, memory=None, tau=0.01, copy_over_frequency=3, **kwargs):
        """Trains an Agent using the Q Learning method on a 2 neural nets.

        Notes:
            Unlike the base DQN, this is a true reflection of ref [1]. We use 2 models instead of one to allow for
            training the action model more stably.

        Args:
            data: Used for size input / output information.

        References:
            [1] Mnih, Volodymyr, et al. "Playing atari with deep reinforcement learning."
            arXiv preprint arXiv:1312.5602 (2013).
        """
        super().__init__(data, memory, **kwargs)
        self.name = 'DQN Fixed Targeting'
        self.tau = tau
        self.target_net = deepcopy(self.action_model)
        self.learner_callbacks += [partial(FixedTargetDQNCallback, copy_over_frequency=copy_over_frequency)]

    def target_copy_over(self):
        """ Updates the target network from calls in the FixedTargetDQNCallback callback."""
        # self.target_net.load_state_dict(self.action_model.state_dict())
        for target_param, local_param in zip(self.target_net.parameters(), self.action_model.parameters()):
            target_param.data.copy_(self.tau * local_param.data + (1.0 - self.tau) * target_param.data)

    def optimize(self):
        r"""Uses ER to optimize the Q-net.

        Uses the equation:

        .. math::
                Q^{*}(s, a) = \mathbb{E}_{s'∼ \Big\epsilon} \Big[r + \lambda \displaystyle\max_{a'}(Q^{*}(s' , a'))
                \;|\; s, a \Big]


        Returns (dict): Optimization information
        """
        if len(self.memory) > self.batch_size:
            self.warming_up = False
            # Perhaps have memory as another item list? Should investigate.
            sampled = self.memory.sample(self.batch_size)

            with torch.no_grad():
                r = torch.from_numpy(np.array([item.reward for item in sampled])).float()
                s_prime = torch.from_numpy(np.array([item.result_state for item in sampled])).float()
                s = torch.from_numpy(np.array([item.current_state for item in sampled])).float()
                a = torch.from_numpy(np.array([item.actions for item in sampled])).long()
                d = torch.from_numpy(np.array([item.done for item in sampled])).float()

            # Traditional `maze-random-5x5-v0` with have a model output a Nx4 output.
            # since r is just Nx1, we spread the reward into the actions.
            y_hat = self.action_model(s).gather(1, a)

            masking = torch.sub(1.0, d).unsqueeze(1)
            y = self.discount * self.target_net(s_prime).max(axis=1)[0].unsqueeze(1) * masking + r.expand_as(y_hat)

            loss = self.loss_func(y, y_hat)
            self.loss = loss.cpu().detach()

            if self.training:
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.action_model.parameters(), self.gradient_clipping_norm)
                for param in self.action_model.parameters():
                    param.grad.data.clamp_(-1, 1)
                self.opt.step()

            with torch.no_grad():
                post_info = {'td_error': (y - y_hat).numpy()}
                return post_info


class DoubleDQN(FixedTargetDQN):
    def __init__(self, data: MDPDataBunchAlpha, memory=None, copy_over_frequency=3, **kwargs):
        """
        Double DQN training.

        References:
            [1] Van Hasselt, Hado, Arthur Guez, and David Silver. "Deep reinforcement learning with double q-learning."
            Thirtieth AAAI conference on artificial intelligence. 2016.

        Args:
            data: Used for size input / output information.
        """
        super().__init__(data=data, memory=memory, copy_over_frequency=copy_over_frequency, **kwargs)
        self.name = 'DDQN'

    def optimize(self):
        r"""Uses ER to optimize the Q-net.

        Uses the equation:

        .. math::
                Q^{*}(s, a) = \mathbb{E}_{s'∼ \Big\epsilon} \Big[r + \lambda \displaystyle\max_{}(Q^{*}(s' , \
                argmax_{a'}(Q(s', \Theta)), \Theta^{-})) \;|\; s, a \Big]

        Returns (dict): Optimization information
        """
        if len(self.memory) > self.batch_size:
            self.warming_up = False
            # Perhaps have memory as another itemlist? Should investigate.
            sampled = self.memory.sample(self.batch_size)
            with torch.no_grad():
                r = torch.from_numpy(np.array([item.reward for item in sampled])).float()
                s_prime = torch.from_numpy(np.array([item.result_state for item in sampled])).float()
                s = torch.from_numpy(np.array([item.current_state for item in sampled])).float()
                a = torch.from_numpy(np.array([item.actions for item in sampled])).long()

            # Traditional `maze-random-5x5-v0` with have a model output a Nx4 output.
            # since r is just Nx1, we spread the reward into the actions.
            y_hat = self.action_model(s).gather(1, a)
            y = self.discount * self.target_net(s_prime).gather(1, self.action_model(s_prime).argmax(axis=1).unsqueeze(
                1)) + r.expand_as(y_hat)

            loss = self.loss_func(y, y_hat)
            self.loss = loss

            if self.training:
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.action_model.parameters(), self.gradient_clipping_norm)
                for param in self.action_model.parameters():
                    param.grad.data.clamp_(-1, 1)
                self.opt.step()

            with torch.no_grad():
                post_info = {'td_error': (y - y_hat).numpy()}
                return post_info


class DuelingDQNModule(nn.Module):
    def __init__(self, a_s, stream_input_size):
        super().__init__()

        self.val = create_nn_model([stream_input_size], (0, 1), (stream_input_size, 0))
        self.adv = create_nn_model([stream_input_size], a_s[0], (stream_input_size, 0))

    def forward(self, x):
        r"""Splits the base neural net output into 2 streams to evaluate the advantage and values of the s space and
        corresponding actions.

        .. math::
           Q(s,a;\; \Theta, \\alpha, \\beta) = V(s;\; \Theta, \\beta) + A(s, a;\; \Theta, \\alpha) - \\frac{1}{|A|}
           \\Big\\sum_{a'} A(s, a';\; \Theta, \\alpha)

        Args:
            x:

        Returns:
        """
        val = self.val(x)
        adv = self.adv(x)

        x = val.expand_as(adv) + (adv - adv.mean()).squeeze(0)
        return x


class DuelingDQN(FixedTargetDQN):
    def __init__(self, data: MDPDataBunchAlpha, memory=None, **kwargs):
        """Replaces the basic action model with a DuelingDQNModule which splits the basic model into 2 streams.


        References:
            [1] Wang, Ziyu, et al. "Dueling network architectures for deep reinforcement learning."
            arXiv preprint arXiv:1511.06581 (2015).

        Args:
            data:
        """
        super().__init__(data, memory, **kwargs)
        self.name = 'Dueling DQN'

    def initialize_action_model(self, layers, data):
        base = create_nn_model(layers, *data.get_action_state_size())[:-1]
        a_s = data.get_action_state_size()
        stream_input_size = base[-2].out_features
        dueling_head = DuelingDQNModule(a_s=a_s, stream_input_size=stream_input_size)
        return nn.Sequential(base, dueling_head)


class DoubleDuelingDQN(DoubleDQN, DuelingDQN):
    def __init__(self, data: MDPDataBunchAlpha, memory=None, **kwargs):
        """
        Combines both Dueling DQN and DDQN.

        Args:
            data: Used for size input / output information.
        """
        super().__init__(data, memory, **kwargs)
        self.name = 'DDDQN'
