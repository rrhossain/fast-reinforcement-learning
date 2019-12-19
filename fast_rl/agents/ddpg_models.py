from math import ceil

from fastai.callback import OptimWrapper
from fastai.tabular import TabularModel
from fastai.vision import cnn_learner
from fastai.torch_core import *
from torch.nn import MSELoss
from torch.optim import Adam

from fast_rl.agents.agents_base import Flatten
from fast_rl.agents.dqn_models import conv_bn_lrelu, ks_stride


class CriticTabularEmbedWrapper(Module):
	def __init__(self, tabular_model: Module, exclude_cat):
		super().__init__()
		self.tabular_model = tabular_model
		self.exclude_cat = exclude_cat

	def forward(self, args):
		if not self.exclude_cat: return self.tabular_model(*args)
		else: return self.tabular_model(0, torch.cat(args, axis=1))


class ActorTabularEmbedWrapper(Module):
	def __init__(self, tabular_model: Module):
		super().__init__()
		self.tabular_model = tabular_model

	def forward(self, xi: Tensor, *args):
		return self.tabular_model(xi, xi)


class StateActionSplitter(Module):
	def forward(self, s_a_tuple):
		return s_a_tuple[0], s_a_tuple[1]


class StateActionPassThrough(nn.Module):
	def __init__(self, layers):
		super().__init__()
		self.layers = layers

	def forward(self, state_action):
		return (self.layers(state_action[0]), state_action[1])


class ChannelTranspose(Module):
	def forward(self, xi: Tensor):
		return xi.transpose(3, 1).transpose(3, 2)


class CriticModule(nn.Sequential):
	def __init__(self, ni: int, ao: int, layers: Collection[int], batch_norm=False,
				 n_conv_blocks: Collection[int] = 0, nc=3, emb_szs: ListSizes = None,
				 w=-1, h=-1, ks=None, stride=None, conv_kern_proportion=0.1, stride_proportion=0.1, pad=False):
		super().__init__()
		self.switched, self.batch_norm = False, batch_norm
		self.ks, self.stride = ([], []) if len(n_conv_blocks) == 0 else ks_stride(ks, stride, w, h, n_conv_blocks, conv_kern_proportion, stride_proportion)
		self.action_model = nn.Sequential()
		_layers = [conv_bn_lrelu(nc, self.nf, ks=ks, stride=stride, pad=pad) for self.nf, ks, stride in zip(n_conv_blocks, self.ks, self.stride)]
		if _layers: ni = self.setup_conv_block(_layers=_layers, ni=ni, nc=nc, w=w, h=h)
		self.setup_linear_block(_layers=_layers, ni=ni, nc=nc, w=w, h=h, emb_szs=emb_szs, layers=layers, ao=ao)
		self.init_weights(self)

	def setup_conv_block(self, _layers, ni, nc, w, h):
		self.add_module('conv_block', StateActionPassThrough(nn.Sequential(*(self.fix_switched_channels(ni, nc, _layers) + [Flatten()]))))
		return int(self(torch.zeros((2, 1, w, h, nc) if self.switched else (2, 1, nc, w, h)))[0].view(-1, ).shape[0])

	def setup_linear_block(self, _layers, ni, nc, w, h, emb_szs, layers, ao):
		tabular_model = TabularModel(emb_szs=emb_szs, n_cont=ni+ao if not emb_szs else ao, layers=layers, out_sz=1,
									 use_bn=self.batch_norm)
		if not emb_szs: tabular_model.embeds = None
		self.add_module('lin_block', CriticTabularEmbedWrapper(tabular_model, exclude_cat=not emb_szs))

	def fix_switched_channels(self, current_channels, expected_channels, layers: list):
		if current_channels == expected_channels:
			return layers
		else:
			self.switched = True
			return [ChannelTranspose()] + layers

	def init_weights(self, m):
		if type(m) == nn.Linear:
			torch.nn.init.xavier_uniform_(m.weight)
			m.bias.data.fill_(0.01)


class ActorModule(nn.Sequential):
	def __init__(self, ni: int, ao: int, layers: Collection[int],batch_norm = False,
				 n_conv_blocks: Collection[int] = 0, nc=3, emb_szs: ListSizes = None,
				 w=-1, h=-1, ks=None, stride=None, conv_kern_proportion=0.1, stride_proportion=0.1, pad=False):
		super().__init__()
		self.switched, self.batch_norm = False, batch_norm
		self.ks, self.stride = ([], []) if len(n_conv_blocks) == 0 else ks_stride(ks, stride, w, h, n_conv_blocks, conv_kern_proportion, stride_proportion)
		self.action_model = nn.Sequential()
		_layers = [conv_bn_lrelu(nc, self.nf, ks=ks, stride=stride, pad=pad) for self.nf, ks, stride in zip(n_conv_blocks, self.ks, self.stride)]
		if _layers: ni = self.setup_conv_block(_layers=_layers, ni=ni, nc=nc, w=w, h=h)
		self.setup_linear_block(_layers=_layers, ni=ni, nc=nc, w=w, h=h, emb_szs=emb_szs, layers=layers, ao=ao)
		self.init_weights(self)

	def setup_conv_block(self, _layers, ni, nc, w, h):
		self.add_module('conv_block', nn.Sequential(*(self.fix_switched_channels(ni, nc, _layers) + [Flatten()])))
		return int(self(torch.zeros((1, w, h, nc) if self.switched else (1, nc, w, h))).view(-1, ).shape[0])

	def setup_linear_block(self, _layers, ni, nc, w, h, emb_szs, layers, ao):
		tabular_model = TabularModel(emb_szs=emb_szs, n_cont=ni if not emb_szs else 0, layers=layers, out_sz=ao, use_bn=self.batch_norm)
		if not emb_szs: tabular_model.embeds = None
		self.add_module('lin_block', ActorTabularEmbedWrapper(tabular_model))

	def fix_switched_channels(self, current_channels, expected_channels, layers: list):
		if current_channels == expected_channels:
			return layers
		else:
			self.switched = True
			return [ChannelTranspose()] + layers

	def init_weights(self, m):
		if type(m) == nn.Linear:
			torch.nn.init.xavier_uniform_(m.weight)
			m.bias.data.fill_(0.01)

class DDPGModule(Module):
	def __init__(self, ni: int, ao: int, layers: Collection[int], discount: float = 0.99,
				 n_conv_blocks: Collection[int] = 0, nc=3, opt=None, emb_szs: ListSizes = None, loss_func=None,
				 w=-1, h=-1, ks=None, stride=None, grad_clip=5, tau=1e-3, lr=1e-3, actor_lr=1e-4,
				 batch_norm=False, **kwargs):
		r"""
		Implementation of a discrete control algorithm using an actor/critic architecture.

		Notes:
			Uses 4 networks, 2 actors, 2 critics.
			All models use batch norm for feature invariance.
			NNCritic simply predicts Q while the Actor proposes the actions to take given a s s.

		References:
			[1] Lillicrap, Timothy P., et al. "Continuous control with deep reinforcement learning."
			arXiv preprint arXiv:1509.02971 (2015).

		Args:
			data: Primary data object to use.
			memory: How big the tree buffer will be for offline training.
			tau: Defines how "soft/hard" we will copy the target networks over to the primary networks.
			discount: Determines the amount of discounting the existing Q reward.
			lr: Rate that the opt will learn parameter gradients.
		"""
		super().__init__()
		self.name = 'DDPG'
		self.lr = lr
		self.discount = discount
		self.tau = tau
		self.loss_func = None
		self.loss = None
		self.batch_norm = batch_norm

		self.action_model = ActorModule(ni=ni, ao=ao, layers=layers, nc=nc, emb_szs=emb_szs,batch_norm = batch_norm,
										w=w, h=h, ks=ks, n_conv_blocks=n_conv_blocks, stride=stride)
		self.critic_model = CriticModule(ni=ni, ao=ao, layers=layers, nc=nc, emb_szs=emb_szs, batch_norm = batch_norm,
										 w=w, h=h, ks=ks, n_conv_blocks=n_conv_blocks, stride=stride)

		self.opt = OptimWrapper.create(ifnone(opt, Adam), lr=actor_lr, layer_groups=[self.action_model])
		self.critic_optimizer = OptimWrapper.create(ifnone(opt, Adam), lr=lr, layer_groups=[self.critic_model])

		self.t_action_model = deepcopy(self.action_model)
		self.t_critic_model = deepcopy(self.critic_model)

		self.target_copy_over()
		self.tau = tau

	def optimize(self, sampled):
		r"""
		Performs separate updates to the actor and critic models.

		Get the predicted yi for optimizing the actor:

		.. math::
				y_i = r_i + \lambda Q^'(s_{i+1}, \; \mu^'(s_{i+1} \;|\; \Theta^{\mu'}}\;|\; \Theta^{Q'})

		On actor optimization, use the actor as the sample policy gradient.

		Returns:

		"""
		with torch.no_grad():
			r = torch.cat([item.reward.float() for item in sampled])#.to(self.data.device)
			s_prime = torch.cat([item.s_prime for item in sampled])#.to(self.data.device)
			s = torch.cat([item.s for item in sampled])#.to(self.data.device)
			a = torch.cat([item.a.float() for item in sampled])#.to(self.data.device)
			# d = torch.cat([item.done.float() for item in sampled]) # Do we need a mask??

		with torch.no_grad():
			y = r + self.discount * self.t_critic_model((s_prime, self.t_action_model(s_prime)))

		y_hat = self.critic_model((s, a))

		critic_loss = self.loss_func(y_hat, y)

		if self.training:
			# Optimize critic network
			self.critic_optimizer.zero_grad()
			critic_loss.backward()
			self.critic_optimizer.step()

		actor_loss = -self.critic_model((s, self.action_model(s))).mean()

		self.loss = critic_loss.cpu().detach()

		if self.training:
			# Optimize actor network
			self.opt.zero_grad()
			actor_loss.backward()
			self.opt.step()

		with torch.no_grad():
			post_info = {'td_error': (y - y_hat).cpu().numpy()}
			return post_info

	def forward(self, xi):
		training = self.training
		if xi.shape[0] == 1: self.eval()
		pred = self.action_model(xi)
		if training: self.train()
		return pred

	def target_copy_over(self):
		""" Soft target updates the actor and critic models.."""
		self.soft_target_copy_over(self.t_action_model, self.action_model, self.tau)
		self.soft_target_copy_over(self.t_critic_model, self.critic_model, self.tau)

	def soft_target_copy_over(self, t_m, f_m, tau):
		for target_param, local_param in zip(t_m.parameters(), f_m.parameters()):
			target_param.data.copy_(tau * local_param.data + (1.0 - tau) * target_param.data)

	def interpret_q(self, item):
		with torch.no_grad():
			return self.critic_model(torch.cat((item.s, item.a), 1))