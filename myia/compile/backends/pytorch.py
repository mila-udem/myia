"""Linear implementation using pytorch."""

import torch
import torch.utils.dlpack

from . import Backend
from ..transform import CompileGraphs, nonlinear_ops

from ...dtype import Int, UInt, Float, Bool
from ...prim import Primitive, ops as P


_type_map = {
    Int[8]: torch.int8,
    Int[16]: torch.int16,
    Int[32]: torch.int32,
    Int[64]: torch.int64,
    UInt[8]: torch.uint8,
    Float[16]: torch.float16,
    Float[32]: torch.float32,
    Float[64]: torch.float64,
    Bool: torch.uint8,
    # This is a hack but we really need uint64 support
    UInt[64]: torch.int64,
}


def type_to_pytorch_type(t):
    """Map myia types to pytorch types."""
    if t not in _type_map:  # pragma: no cover
        raise TypeError(f"Unsupported type: {t}")
    return _type_map[t]


simple_mapping = {
    P.scalar_add: lambda a, b: a + b,
    P.scalar_sub: lambda a, b: a - b,
    P.scalar_mul: lambda a, b: a * b,
    P.scalar_div: lambda a, b: a / b,
    P.scalar_mod: lambda a, b: a % b,
    P.scalar_pow: lambda a, b: a ** b,
    P.scalar_floor: torch.floor,
    P.scalar_uadd: lambda a: a,
    P.scalar_usub: lambda a: -a,
    P.scalar_exp: torch.exp,
    P.scalar_log: torch.log,
    P.scalar_tan: torch.tan,
    P.scalar_tanh: torch.tanh,

    P.scalar_eq: torch.eq,
    P.scalar_lt: torch.lt,
    P.scalar_gt: torch.gt,
    P.scalar_ne: torch.ne,
    P.scalar_le: torch.le,
    P.scalar_ge: torch.ge,

    P.bool_and: lambda a, b: a & b,
    P.bool_or: lambda a, b: a | b,
    P.bool_eq: torch.eq,
    P.bool_not: lambda a: ~a,

    P.distribute: lambda a, shp: a.expand(*shp),
    P.transpose: lambda a, perm: a.permute(*perm),
    P.dot: torch.mm,

    P.scalar_to_array: lambda x: x,
}


def pytorch_array_map(op):
    """Implementation of array_map for pytorch."""
    fn = op.inputs[1]
    assert fn.is_constant(Primitive)
    fn = fn.value
    if fn in simple_mapping:
        impl = simple_mapping[fn]
    else:
        raise NotImplementedError(f'array_map of {fn}')

    def _impl(*args):
        return (impl(*args),)
    return _impl, op.inputs[2:]


def pytorch_array_reduce(op):
    """Implementation of array_reduce for pytorch."""
    fn = op.inputs[1]
    shape = op.inputs[3]
    assert fn.is_constant(Primitive)
    assert shape.is_constant(tuple)
    fn = fn.value
    tshp = shape.value

    if fn == P.scalar_add:
        impl = torch.sum
    else:
        raise NotImplementedError(f"reduce with {fn}")

    def _impl(array):
        ashp = array.shape

        if len(tshp) < len(ashp):
            ts = (1,) * (len(ashp) - len(tshp)) + tshp
        else:
            ts = tshp
        axis = list(i for i, t in enumerate(ts) if t == 1)
        if len(axis) == 1:
            axis = axis[0]
        res = impl(array, axis, keepdim=True)
        if len(tshp) < len(ashp):
            res = torch.reshape(res, shape=tshp)
        return (res,)
    return _impl, (op.inputs[2],)


_mapping = {
    P.array_map: pytorch_array_map,
    P.array_reduce: pytorch_array_reduce,
}

for k, v in simple_mapping.items():
    _mapping[k] = lambda op, v=v: (lambda *args: (v(*args),), op.inputs[1:])


def pytorch_convert(lst):
    """Convert myia op to pytorch op."""
    assert len(lst) == 1
    op = lst[0]

    assert op.is_apply()
    assert op.inputs[0].is_constant(Primitive)

    fn = op.inputs[0].value
    mapper = _mapping.get(fn, None)
    if mapper is None:
        raise NotImplementedError(fn)
    impl, inputs = mapper(op)
    return impl, inputs, [op]


class PyTorchBackend(Backend):
    """Backend to run using pytorch.

    Backend options:
        device: the target device for data storage ('cpu', 'cuda', 'cuda:X')

    """

    def __init__(self, device='cpu'):
        """Create a PyTorch backend on the given device."""
        self.device = torch.device(device)
        self.compiler = CompileGraphs(pytorch_convert, nonlinear_ops, self,
                                      split_linear=True)

    def compile(self, graph):
        """Compile a graph."""
        return self.compiler.compile_and_link(graph)

    def to_numpy(self, v):
        """Make a numpy array from a torch tensor."""
        return v.numpy()

    def from_numpy(self, a):
        """Make a torch tensor from a numpy array."""
        return torch.from_numpy(a).to(self.device)

    def to_scalar(self, v):
        """Convert a torch tensor to a scalar."""
        return v.item()

    def from_scalar(self, s, t):
        """Convert a scalar to a torch tensor."""
        dt = type_to_pytorch_type(t)
        return torch.tensor(s, dtype=dt, device=self.device)

    def to_dlpack(self, v):
        """Make a dlpack capsule from a torch tensor."""
        return torch.utils.dlpack.to_dlpack(v)

    def from_dlpack(self, dlp):
        """Make a torch tensor from a dlpack capsule."""
        return torch.utils.dlpack.from_dlpack(dlp).to(self.device)

    def check_array(self, v, t):
        """Check if the value is a torch tensor of the right dtype."""
        if not isinstance(v, torch.Tensor):
            raise TypeError("Expected torch.Tensor")
        if v.device != self.device:  # pragma: no cover
            raise RuntimeError("Tensor on wrong device.")
        if v.dtype != type_to_pytorch_type(t):
            raise TypeError("Wrong dtype")