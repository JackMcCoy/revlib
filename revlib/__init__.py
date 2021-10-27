import copy
import typing

import torch

DUAL_OR_QUAD_TENSOR = typing.Union[typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor],
                                   typing.Tuple[torch.Tensor, torch.Tensor]]


class MemoryModes:
    no_savings = 0
    checkpoint = 1
    autograd_graph = 2
    autograd_function = 3


MEMORY_MODES = typing.Union[MemoryModes.no_savings, MemoryModes.checkpoint, MemoryModes.autograd_graph,
                            MemoryModes.autograd_function]


class _ReplaceGrad(torch.autograd.Function):
    @staticmethod
    def forward(ctx, inp0: torch.Tensor, tmp_inp0: torch.Tensor, inp1: torch.Tensor, tmp_inp1: torch.Tensor):
        ctx.save_for_backward(inp0.detach(), inp1.detach())
        return inp0, inp1

    @staticmethod
    def backward(ctx, grad0: torch.Tensor, grad1: torch.Tensor):
        tmp_inp0, tmp_inp1 = ctx.saved_tensors
        return grad0, tmp_inp0, grad1, tmp_inp1


def _set_device(mod: torch.nn.Module, device: str) -> torch.nn.Module:
    if not device:
        return mod
    return copy.deepcopy(mod).to(device, non_blocking=True)


class _ReversibleHalfResidualSwapFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x0: torch.Tensor, x1: torch.Tensor, back_x0: torch.Tensor, back_x1: torch.Tensor,
                mod: torch.nn.Module, coupling_forward: typing.Callable,
                coupling_inverse: typing.Callable,
                target_device: str) -> DUAL_OR_QUAD_TENSOR:
        ctx.mod = mod
        ctx.target_device = target_device
        ctx.coupling_forward = coupling_forward
        ctx.coupling_inverse = coupling_inverse
        ctx.forward_rng_state = torch.get_rng_state()
        return x1, coupling_forward(x0, _set_device(mod, target_device)(x1)), back_x0, back_x1

    @staticmethod
    def backward(ctx, dy0: torch.Tensor, y0: torch.Tensor, dy1: torch.Tensor, y1: torch.Tensor
                 ) -> typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, None, None, None, None]:
        original_rng_state = torch.get_rng_state()
        torch.set_rng_state(ctx.forward_rng_state)
        with torch.enable_grad():
            y0 = y0.requires_grad_()
            y0.retain_grad()
            new_mod = _set_device(ctx.mod, ctx.target_device)
            mod_out = new_mod(y0)
        with torch.no_grad():
            x0 = ctx.coupling_inverse(y1, mod_out.detach())
        with torch.enable_grad():
            out = ctx.coupling_forward(x0, mod_out)
        torch.autograd.backward(out, dy1)
        if ctx.target_device:
            with torch.no_grad():
                for p, new_p in zip(ctx.mod.parameters(), new_mod.parameters()):
                    if new_p.grad is None:
                        continue
                    new_grad = new_p.grad.to(p.device, non_blocking=True)
                    if p.grad is None:
                        p.grad = new_grad
                        continue
                    p.grad.add_(new_grad)
        torch.set_rng_state(original_rng_state)
        with torch.enable_grad():
            return (dy1.detach(), ctx.coupling_forward(dy0, y0.grad).detach_(), x0.detach_(), y0.detach_(),
                    None, None, None, None)


class TensorOffload(torch.autograd.Function):
    @staticmethod
    def forward(ctx, inp: torch.Tensor, reference: torch.Tensor):
        ctx.device = inp.device
        return inp.to(device=reference.device, non_blocking=True)

    @staticmethod
    def backward(ctx, grad_outputs: torch.Tensor):
        return grad_outputs.to(ctx.device, non_blocking=True), None


offload_tensor = TensorOffload.apply
replace_grad = _ReplaceGrad.apply
reverse_and_swap = _ReversibleHalfResidualSwapFn.apply


def additive_coupling_forward(other_stream: torch.Tensor, fn_out: torch.Tensor) -> torch.Tensor:
    return other_stream + fn_out


def additive_coupling_inverse(output: torch.Tensor, fn_out: torch.Tensor) -> torch.Tensor:
    return output - fn_out


class ReversibleModuleCache:
    x0: torch.Tensor
    x1: torch.Tensor

    def __call__(self, x0: torch.Tensor, x1: torch.Tensor):
        self.x0 = x0
        self.x1 = x1


class ReversibleModule(torch.nn.Module):
    def __init__(self, wrapped_module: torch.nn.Module, coupling_forward: typing.Optional[typing.Callable] = None,
                 coupling_inverse: typing.Optional[typing.Callable] = None, memory_savings: bool = True,
                 cache: typing.Optional[ReversibleModuleCache] = None, target_device: str = ""):
        super(ReversibleModule, self).__init__()
        self.wrapped_module = wrapped_module
        self.target_device = target_device
        self.coupling_forward = coupling_forward or additive_coupling_forward
        self.coupling_inverse = coupling_inverse or additive_coupling_inverse
        self.memory_savings = memory_savings
        self.cache = cache

        self.cpu_state: torch.Tensor = torch.get_rng_state()
        self.cuda: bool = torch.cuda._initialized
        self.autocast: bool = torch.is_autocast_enabled()

        self.x0: typing.Optional[torch.Tensor] = None
        self.x1: typing.Optional[torch.Tensor] = None
        self.idx: int = 0
        self.counter: int = 0
        self.storage: typing.Dict[str, torch.Tensor] = {}

    def get_key(self, idx: int, inp: torch.Tensor):
        key = f'Index: {idx}\nSize: {inp.size()}\nDevice: {inp.device}\nDataType: {inp.dtype}'
        return key

    def pack(self, inp: torch.Tensor) -> str:
        self.counter += 1
        return self.get_key(self.counter - 1, inp)

    def inner_pack(self, inp: torch.Tensor):
        self.storage[self.get_key(len(self.storage), inp)] = inp

    def inner_unpack(self, key: str):
        raise RuntimeError(f'Tensor not found.\nSpec:\n{key}')

    def unpack(self, key: str) -> torch.Tensor:
        if self.storage:
            if key not in self.storage:
                self.inner_unpack(key)
            return self.storage[key]
        x0 = self.cache.x0
        x1 = self.cache.x1
        torch.set_rng_state(self.cpu_state)
        with torch.enable_grad(), torch.cuda.amp.autocast(self.autocast):
            with torch.autograd.graph.saved_tensors_hooks(self.inner_pack, self.inner_unpack):
                out = self.module_list[self.idx](self.x1)
            self.cache(x1.detach_(), self.coupling_inverse(x0, out.detach()).detach_())
            with torch.autograd.graph.saved_tensors_hooks(self.inner_pack, self.inner_unpack):
                _unused = self.coupling_forward(x0, out)
        return self.unpack(key)

    def forward(self, inp: DUAL_OR_QUAD_TENSOR) -> DUAL_OR_QUAD_TENSOR:
        x0, x1, *back = inp

        if not self.memory_savings:
            return x1, self.coupling_forward(x0, _set_device(self.wrapped_module, self.target_device)(x1))

        if self.cache is None:
            return reverse_and_swap(x0, x1, *back, self.wrapped_module, self.coupling_forward,
                                    self.coupling_inverse, self.target_device)

        self.cache(x0, x1)
        self.counter = 0
        self.storage = {}
        hook = torch.autograd.graph.saved_tensors_hooks(self.pack, self.unpack)
        with hook:
            y1 = self.coupling_forward(x0, self.module_list[self.idx](x1))
        return x1, y1

    def extra_repr(self) -> str:
        return '\n'.join([f'coupling_forward={self.coupling_forward.__name__}',
                          f'coupling_inverse={self.coupling_inverse.__name__}',
                          f'target_device={self.target_device if self.target_device else None}'])


class ReversibleSequential(torch.nn.Module):
    def __init__(self, *modules, split_dim=1,
                 coupling_forward: typing.Optional[typing.List[typing.Optional[typing.Callable]]] = None,
                 coupling_inverse: typing.Optional[typing.List[typing.Optional[typing.Callable]]] = None,
                 memory_mode: MEMORY_MODES = MemoryModes.autograd_function,
                 target_device: str = ""):
        super(ReversibleSequential, self).__init__()
        coupling_forward = list(coupling_forward) if coupling_forward else [None]
        coupling_inverse = list(coupling_inverse) if coupling_inverse else [None]
        memory_savings = memory_mode == MemoryModes.no_savings
        cache = ReversibleModuleCache() if memory_mode in (MemoryModes.checkpoint, MemoryModes.autograd_graph) else None
        self.stem = torch.nn.Sequential(*[m if isinstance(m, ReversibleModule) else
                                          ReversibleModule(m,
                                                           coupling_forward[i % len(coupling_forward)],
                                                           coupling_inverse[i % len(coupling_inverse)],
                                                           memory_savings,
                                                           copy.deepcopy(cache) if memory_mode == MemoryModes.checkpoint
                                                           else cache,
                                                           target_device
                                                           )
                                          for i, m in enumerate(modules)])
        self.split_dim = split_dim

    def forward(self, inp: torch.Tensor) -> torch.Tensor:
        inp0, inp1 = inp.chunk(2, self.split_dim)
        zeros = torch.zeros_like(inp0)
        return torch.cat(replace_grad(*self.stem((inp0, inp1, zeros, zeros))), dim=self.split_dim)
