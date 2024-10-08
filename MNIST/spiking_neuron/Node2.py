from abc import abstractmethod
from typing import Callable
import torch
from spiking_neuron import base


class BaseNode(base.MemoryModule):
    def __init__(self,
                 v_threshold: float = 1.,
                 v_reset: float = 0.,
                 surrogate_function: Callable = None,
                 detach_reset: bool = False,
                 step_mode='s', backend='torch',
                 store_v_seq: bool = False):

        # assert isinstance(v_reset, float) or v_reset is None
        # assert isinstance(v_threshold, float)
        # assert isinstance(detach_reset, bool)
        super().__init__()

        if v_reset is None:
            self.register_memory('v', 0.)  # self.name['v']是在这里被注册的
        else:
            self.register_memory('v', v_reset)

        self.v_threshold = v_threshold

        self.v_reset = v_reset
        self.detach_reset = detach_reset
        self.surrogate_function = surrogate_function

        self.step_mode = step_mode
        self.backend = backend

        self.store_v_seq = store_v_seq


    @property
    def store_v_seq(self):
        return self._store_v_seq

    @store_v_seq.setter
    def store_v_seq(self, value: bool):
        self._store_v_seq = value
        if value:
            if not hasattr(self, 'v_seq'):
                self.register_memory('v_seq', None)

    @staticmethod
    @torch.jit.script
    def jit_hard_reset(v: torch.Tensor, spike: torch.Tensor, v_reset: float):
        v = (1. - spike) * v + spike * v_reset  # 没什么用，一般都是用soft reset

        return v

    @staticmethod
    @torch.jit.script
    def jit_soft_reset(v: torch.Tensor, spike: torch.Tensor, v_threshold: float):
        v = v - spike * v_threshold
        # if v < 0:
        #     v = 0  # 防止减成负数
        return v

    @abstractmethod
    def neuronal_charge(self, x: torch.Tensor):
        raise NotImplementedError

    def neuronal_fire(self):
        # print(self.v_threshold)
        return self.surrogate_function(self.v - self.v_threshold[0][0] - self.v_threshold[0][1])

    def extra_repr(self):
        return f'v_threshold={self.v_threshold}, v_reset={self.v_reset}, detach_reset={self.detach_reset}, step_mode={self.step_mode}, backend={self.backend}'

    def single_step_forward(self, x: torch.Tensor):  # 这是训练的时候的核心部分
        self.v_float_to_tensor(x)  # layers中的相关调用是在这个函数里面
        self.neuronal_charge(x)
        spike = self.neuronal_fire()
        self.neuronal_reset(spike)
        return spike

    def multi_step_forward(self, x_seq: torch.Tensor):
        T = x_seq.shape[0]
        y_seq = []
        if self.store_v_seq:
            v_seq = []
        for t in range(T):
            y = self.single_step_forward(x_seq[t])
            y_seq.append(y)
            if self.store_v_seq:
                v_seq.append(self.v)

        if self.store_v_seq:
            self.v_seq = torch.stack(v_seq)

        return torch.stack(y_seq)

    def v_float_to_tensor(self, x: torch.Tensor):
        if isinstance(self.v, float):
            v_init = self.v
            self.v = torch.full_like(x.data, v_init)


class Node2(BaseNode):  # 关键在看懂这个node。其他的都是很简单的神经网络训练过程
    def __init__(self,
                 v_threshold: torch.Tensor = torch.full([1, 2], 0, dtype=torch.float),
                 v_reset=0.,
                 surrogate_function: Callable = None,
                 detach_reset=False,
                 hard_reset=False,
                 step_mode='s',
                 k=2,
                 decay_factor: torch.Tensor = torch.full([1, 4], 0, dtype=torch.float),
                 gamma: float = 0.5):
        super(Node2, self).__init__(v_threshold, v_reset, surrogate_function, detach_reset, step_mode)
        self.k = k
        for i in range(1, self.k + 1):
            self.register_memory('v' + str(i), 0.)

        self.names = self._memories
        self.hard_reset = hard_reset
        self.gamma = gamma
        self.decay = decay_factor
        self.decay_factor = torch.nn.Parameter(decay_factor)

    @property
    def supported_backends(self):
        if self.step_mode == 's':
            return ('torch',)
        elif self.step_mode == 'm':
            return ('torch', 'cupy')
        else:
            raise ValueError(self.step_mode)

    def neuronal_charge(self, x: torch.Tensor):
        # v1: membrane potential of dendritic compartment
        # v2: membrane potential of somatic compartment
        s1 = self.names['v1']
        s2 = self.names['v2']
        self.names['v1'] = torch.sigmoid(self.decay_factor[0][0]) * s1 + torch.sigmoid(self.decay_factor[0][1]) * s2 + x
        self.names['v2'] = torch.sigmoid(self.decay_factor[0][2]) * s2 + torch.sigmoid(self.decay_factor[0][3]) * s1 + x
        self.v = self.names['v1'] + self.names['v2']  # x是已经经过nn.Linear层的输出，即I(t)

    def neuronal_reset(self, spike):
        if self.detach_reset:
            spike_d = spike.detach()
        else:
            spike_d = spike

        if not self.hard_reset:  # 需要修改这里以及上面的neuronal_charge
            # soft reset
            self.names['v1'] = self.jit_soft_reset(self.names['v1'], spike_d, self.v_threshold[0][0])  # 这是放电过程。gamma小于1使得树突室为部分放电
            self.names['v2'] = self.jit_soft_reset(self.names['v2'], spike_d, self.v_threshold[0][1])
        else:  # 论文里采用的是soft reset。自己修改的时候，应该也是沿用soft reset
            # hard reset
            for i in range(2, self.k + 1):
                self.names['v' + str(i)] = self.jit_hard_reset(self.names['v' + str(i)], spike_d,  self.v_reset)

    def forward(self, x: torch.Tensor):
        return super().single_step_forward(x)  # 这里相当于single_step_forward

    def extra_repr(self):
        return f"v_threshold={self.v_threshold}, v_reset={self.v_reset}, detach_reset={self.detach_reset}, " \
               f"hard_reset={self.hard_reset}, " \
               f"gamma={self.gamma}, k={self.k}, step_mode={self.step_mode}, backend={self.backend}"

