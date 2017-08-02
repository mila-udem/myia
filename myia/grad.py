from typing import Dict, List

from .ast import \
    Transformer, GenSym, MyiaASTNode, \
    Symbol, Value, Lambda, Let, Apply, Tuple, Closure
from .interpret import \
    global_env, impl, myia_impl, evaluate, \
    PrimitiveImpl, FunctionImpl, ClosureImpl
from .front import Env, parse_function0, get_global_env
from .symbols import builtins, bsym, gsym
from copy import copy
from .compile import a_normal
from .util import Props
from .buche import buche as _buche


buche = _buche['log']
# fbuche = _buche['funcs2']


builtins.fill = gsym('fill')
builtins.zero = gsym('zero')
builtins.one = gsym('one')
builtins.merge = gsym('merge')
builtins.J = gsym('J')
builtins.JX = gsym('JX')
builtins.Jinv = gsym('Jinv')
# builtins.shift_grad = bsym('shift_grad')
# builtins.skim = bsym('skim')


######################################
# Decorator for gradient definitions #
######################################


def macro_grad_for(nclos_args):
    def macro_grad(*args):
        return Tuple([Tuple(args[:nclos_args]), *args[nclos_args:]])
    return macro_grad


def prim_rgrad(sym):
    # Copy symbol to grad namespace
    rsym = Symbol(sym, namespace='builtin', relation='♢*')
    #Symbol(sym.label, namespace='grad:builtin')

    prim = global_env[sym]
    assert isinstance(prim, PrimitiveImpl)

    def decorator(fn):

        # Wrap the primitive and a closure-converted backpropagator
        # in a combined method that follows the protocol
        G = GenSym()
        args = [G.sym(a) for a in prim.argnames]
        forward = Apply(builtins.J,
                        Apply(sym, *[Apply(builtins.Jinv, a)
                                     for a in args]))
        backward = Closure(rsym, args)
        ast = Lambda(args, Tuple([forward, backward]), G)
        impl = FunctionImpl(ast, (global_env,))
        prim.grad = impl
        impl.primal = prim

        global_env[rsym] = PrimitiveImpl(fn)
        return impl

    return decorator


def rgrad(sym):
    #Symbol(sym.label, namespace='grad:builtin')

    def decorator(orig_fn):
        prim = global_env[sym]
        assert isinstance(prim, PrimitiveImpl)

        _cache = {}

        # Wrap the primitive and a closure-converted backpropagator
        # in a combined method that follows the protocol
        def mkgrad(nargs_closure):
            cached = _cache.get(nargs_closure, None)
            if cached:
                return cached

            # Copy symbol to grad namespace
            rsym = Symbol(sym,
                          version=nargs_closure,
                          namespace='builtin',
                          relation='♢*')

            r, bindings = parse_function0(
                orig_fn,
                macros={'GRAD': macro_grad_for(nargs_closure)}
            )
            fn = evaluate(r, bindings)
            G = GenSym()
            args = [G.sym(a) for a in prim.argnames]
            forward = Apply(builtins.J,
                            Apply(sym, *[Apply(builtins.Jinv, a)
                                         for a in args]))
            backward = Closure(rsym, args)
            ast = Lambda(args, Tuple([forward, backward]), G)
            impl = FunctionImpl(ast, (global_env,))
            impl.primal = prim
            global_env[rsym] = fn
            _cache[nargs_closure] = impl
            return impl

        prim.grad = mkgrad

        return impl

    return decorator


################################################
# Implementation of primitives needed for Grad #
################################################


# @impl(builtins.shift_grad)
# def shift_grad(closure, n):
#     """Given a transformed closure, transforms its bprop
#     so that it groups the first n arguments together (these
#     arguments are assumed to be the variables closed over)."""
#     # TODO: this functionality should be implemented elsewhere,
#     # as it is it will play awkwardly with grad(grad), I think.

#     # assert isinstance(closure, ClosureImpl)
#     def f(*args):
#         result, bprop = closure(*args)

#         def bprop2(*args):
#             results = bprop(*args)
#             return (results[0] + results[1:1 + n], *results[1 + n:])
#         return (result, PrimitiveImpl(bprop2, name=f'bprop{str(closure)}'))
#     prim = PrimitiveImpl(f, name=f'shift{str(closure)}')
#     prim.primal = closure.primal
#     return prim


# @impl(builtins.skim)
# def skim(tup, n):
#     if n >= 0:
#         return (tup[0] + tup[1:n + 1], *tup[n + 1:])
#     else:
#         return (tup[0][:-n], *tup[0][-n:], *tup[1:])


@impl(builtins.fill)
def fill(x, value):
    if isinstance(x, (int, float)):
        return value
    elif isinstance(x, tuple):
        return tuple(fill(a, value) for a in x)
    elif isinstance(x, (PrimitiveImpl, FunctionImpl)):
        return ()
    elif isinstance(x, ClosureImpl):
        return tuple(fill(a, value) for a in x.args)
    else:
        raise TypeError(f'Cannot create a {value} conformant with {x}')


@impl(builtins.zero)
def zero(x):
    return fill(x, 0)


@impl(builtins.one)
def one(x):
    return fill(x, 1)


@impl(builtins.merge)
def merge(x, y):
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return x + y
    elif type(x) is not type(y):
        raise TypeError(f'Cannot merge {x} and {y} (not same type).')
    elif isinstance(x, tuple):
        assert len(x) == len(y)
        return tuple(merge(a, b) for a, b in zip(x, y))
    else:
        raise TypeError(f'Cannot merge values of type {type(x)}')


def JGrad(x):
    _cache = {}

    def make_grad(nargs_closure):
        gfn = _cache.get(nargs_closure, None)
        if gfn:
            return gfn

        G = Grad(
            name = x.ast.ref or x.ast.gen('???'),
            primal = a_normal(x.ast),
            nargs_closure = nargs_closure,
            global_env = get_global_env()
        )
        g = G.transform()
        # bindings = {**x.bindings, **G.global_env.bindings}

        bindings = {}
        bindings.update(G.global_env.bindings)
        for env in reversed(x.envs):
            bindings.update(env)

        gfn = evaluate(g, bindings)
        gfn.primal = x
        # x.grad = gfn
        _cache[nargs_closure] = gfn
        return gfn
    return make_grad


@impl(builtins.JX)
def JX(x, nargs_closure):
    if isinstance(x, PrimitiveImpl):
        assert x.grad is not None
        return x.grad(nargs_closure)
    elif isinstance(x, FunctionImpl):
        if not x.grad:
            x.grad = JGrad(x)
        return x.grad(nargs_closure)
    else:
        raise TypeError(f'JX applied on wrong type: {x}')


@impl(builtins.J)
def J(x):
    if isinstance(x, (int, float)):
        return x
    elif isinstance(x, tuple):
        return tuple(J(a) for a in x)
    elif isinstance(x, (PrimitiveImpl, FunctionImpl)):
        return JX(x, 0)
    elif isinstance(x, ClosureImpl):
        c = ClosureImpl(JX(x.fn, len(x.args)),
                        J(tuple(x.args)))
        c.primal = x
        return c
    elif x is None:
        return None
    else:
        raise TypeError(f'Invalid argument for J: {x}')


@impl(builtins.Jinv)
def Jinv(x):
    if isinstance(x, (int, float)):
        return x
    elif isinstance(x, tuple):
        return tuple(Jinv(a) for a in x)
    elif isinstance(x, (PrimitiveImpl, FunctionImpl)):
        assert x.primal is not None
        return x.primal
    elif isinstance(x, ClosureImpl):
        c = ClosureImpl(Jinv(x.fn), Jinv(tuple(x.args)))
        return c
    elif x is None:
        return x
    else:
        raise TypeError(f'Invalid argument for Jinv: {x}')


###########################################
# Gradients of primitives needed for Grad #
###########################################


myia_builtins = Props(globals())


@rgrad(builtins.zero)
def gzero(x, d):
    return GRAD(zero(x))


@rgrad(builtins.merge)
def gmerge(x, y, d):
    return GRAD(d, d)


@rgrad(builtins.JX)
def gJX(x, n, d):
    return GRAD(Jinv(d), 0)


@rgrad(builtins.J)
def gJ(x, d):
    return GRAD(Jinv(d))


@rgrad(builtins.Jinv)
def gJinv(x, d):
    return GRAD(J(d))


######################################
# Gradients of arithmetic primitives #
######################################


@rgrad(builtins.add)
def gadd(x, y, dz):
    return GRAD(dz, dz)


@rgrad(builtins.subtract)
def gsubtract(x, y, dz):
    return GRAD(dz, -dz)


@rgrad(builtins.multiply)
def gmultiply(x, y, dz):
    return GRAD(dz * y, dz * x)


@rgrad(builtins.divide)
def gdivide(x, y, dz):
    return GRAD(dz / y, -dz * x / (y * y))


@rgrad(builtins.unary_subtract)
def gunary_subtract(x, dz):
    return GRAD(-dz)


###################################################
# Gradients of boolean and conditional primitives #
###################################################


@rgrad(builtins.greater)
def ggreater(x, y, dz):
    return GRAD(False, False)


@rgrad(builtins.less)
def gless(x, y, dz):
    return GRAD(False, False)


@rgrad(builtins.lazy_if)
def glazy_if(c, t, f, dz):
    if c:
        return GRAD(
            False,
            t()[1](dz)[0],
            myia_builtins.zero(myia_builtins.Jinv(f))
        )
    else:
        return GRAD(
            False,
            myia_builtins.zero(myia_builtins.Jinv(t)),
            f()[1](dz)[0]
        )


@rgrad(builtins.half_lazy_if)
def ghalf_lazy_if(c, t, f, dz):
    if c:
        return GRAD(
            (),
            False,
            t()[1](dz)[0],
            myia_builtins.zero(myia_builtins.Jinv(f))
        )
    else:
        return GRAD(
            False,
            myia_builtins.zero(myia_builtins.Jinv(t)),
            dz
        )


@rgrad(builtins.switch)
def gswitch(c, t, f, dz):
    if c:
        return GRAD(
            False,
            dz,
            myia_builtins.zero(myia_builtins.Jinv(f))
        )
    else:
        return GRAD(
            False,
            myia_builtins.zero(myia_builtins.Jinv(t)),
            dz
        )


@rgrad(builtins.identity)
def gidentity(v, dz):
    return GRAD(dz)


#################################
# Gradients of other primitives #
#################################


@rgrad(builtins.index)
def gindex(tup, idx, dz):
    def f(pair):
        return switch(pair[0] == idx, dz, 0)
    rval = map(f, enumerate(tup))
    return GRAD(rval, 0)


@rgrad(builtins.len)
def glen(xs, dz):
    return GRAD(myia_builtins.zero(myia_builtins.Jinv(xs)))


@rgrad(builtins.range)
def grange(n, dz):
    return GRAD(0)


@rgrad(builtins.map)
def gmap(f, xs, dz):
    # I... think that's right?
    # TODO: test it
    d = map(f(xs)[1], dz)
    df = reduce(myia_builtins.merge, map(first, d))
    dxs = map(second, d)
    return GRAD(df, dxs)


@rgrad(builtins.enumerate)
def genumerate(xs, dz):
    return GRAD(map(second, dz))


# Following the methodology in the following paper:
#   http://www.bcl.hamilton.ie/~barak/papers/toplas-reverse.pdf

class Grad:
    # Notation:
    # x_up is the reverse (backprop-ready) version of x
    # x_bprop is a function that takes the sensitivity of x and
    #     returns the sensitivity of the inputs of the function
    #     that returns x
    # x_sen is the sensitivity of the gradient to changes in x,
    #     i.e. the quantity we are ultimately interested in

    def __init__(self,
                 name: Symbol,
                 primal: Lambda,
                 global_env: Env,
                 nargs_closure = 0) -> None:
        self.name = name
        assert(isinstance(primal, Lambda))
        self.primal = primal
        self.gensym = primal.gen
        self.global_env = global_env or Env(namespace='global')
        self.tagged_map: Dict[Symbol, Symbol] = {}
        self.sensitivity_map: Dict[Symbol, Symbol] = {}
        self.backpropagator_map: Dict[Symbol, Symbol] = {}
        self.zeroes: List[MyiaASTNode] = []
        self.nargs_closure = nargs_closure

    def phi(self, var, value):
        # phi (p. 26) transformation on let bindings, transforms
        # the forward phase.

        if isinstance(value, Symbol):
            # x = y ==> x_up = y_up
            return [(self.tagged_var(var), self.tagged_var(value)),
                    (self.backpropagator_var(var), Value(None))]

        elif isinstance(value, Value):
            # x = 5 ==> x_up = 5
            return [(self.tagged_var(var), value),
                    (self.backpropagator_var(var), Value(None))]

        elif isinstance(value, Apply):
            # x = f(y) ==> (x_up, x_bprop) = f_up(y_up)
            tmp = self.gensym('tmp')
            return [(tmp,
                     Apply(self.tagged_var(value.fn),
                           *[self.tagged_var(a) for a in value.args])),
                    (self.tagged_var(var),
                     Apply(builtins.index, tmp, Value(0))),
                    (self.backpropagator_var(var),
                     Apply(builtins.index, tmp, Value(1)))]

        elif isinstance(value, Closure):
            # x = lambda y: ... ==> x_up = (lambda y: ...)_up
            # But in our system, we feed free variables explicitly
            # through Closure, and lambda has no freevars, so we do:
            # x = Closure(f, w, z) ==> x_up = Closure(f_up, w_up, z_up) (???)

            args = [self.tagged_var(a) for a in value.args]
            clos = Closure(value.fn, args)
            expr = Apply(builtins.J, clos)
            fn = Apply(builtins.JX, value.fn, Value(len(args)))
            expr = Closure(fn, args)
            return [(self.tagged_var(var), expr),
                    (self.backpropagator_var(var), Value(None))]

        elif isinstance(value, Tuple):
            return [(self.tagged_var(var),
                     Tuple(self.tagged_var(a) for a in value.values)),
                    (self.backpropagator_var(var), Value(None))]

        else:
            raise Exception(f'phi is not defined on node type: {value}')

    def rho(self, var, value):
        # rho (p. 26) transformation on let bindings, represents the
        # corresponding operations to do in the backward phase

        if isinstance(value, Symbol):
            # x = y ==> y_sen += x_sen
            return self.accum([value], Tuple([self.sensitivity_var(var)]))

        elif isinstance(value, Value):
            # x = 5 ==> <nothing>
            return []

        elif isinstance(value, Apply):
            # x = f(y) ==> (f_sen, y_sen) += x_bprop(x_sen)
            args = [value.fn, *value.args]
            increment = Apply(self.backpropagator_var(var),
                              self.sensitivity_var(var))
            return self.accum(args, increment)

        elif isinstance(value, Closure):
            # x = Closure(f, w, z) ==> (w_sen, z_sen) += x_sen
            return self.accum(value.args, self.sensitivity_var(var))

        elif isinstance(value, Tuple):
            return self.accum(value.values, self.sensitivity_var(var))

        else:
            raise Exception(f'rho is not defined on node type: {value}')

    def zero_init(self, var):
        new_var = self.new_sensitivity_var(var)
        init = (new_var,
                Apply(builtins.zero,
                      Apply(builtins.Jinv, self.tagged_var(var))))
        self.zeroes.append(init)
        return new_var

    def accum(self, vars, value):
        if isinstance(vars, list):
            vvars = [(i, v) for i, v in enumerate(vars)
                     if not isinstance(v, Value)]
            sens = [self.sensitivity_var(v) or
                    Apply(builtins.zero, Apply(builtins.Jinv, v))
                    for v in vars]
            new_sens = [self.new_sensitivity_var(v) for _, v in vvars]
            tmp = self.gensym('tmp')
            group = Tuple(sens)
            app = Apply(builtins.merge, group, value)
            rval = [(tmp, app)]
            for new_sen, (i, _) in zip(new_sens, vvars):
                rval.append((new_sen, Apply(builtins.index, tmp, Value(i))))
            return rval
        else:
            sen = self.sensitivity_var(var)
            new_sen = self.new_sensitivity_var(var)
            app = Apply(builtins.merge, sen, value)
            return [(new_sen, app)]

    def tagged_var(self, v):
        # Maps v to the v_up variable i.e. the tagged variable for v
        assert isinstance(v, (Symbol, Value))
        if isinstance(v, Value):
            return v
        if v.namespace in {'global', 'builtin'}:
            return Apply(builtins.J, v)
        else:
            return copy(self.tagged_map.setdefault(v, self.gensym(v, '↑')))

    def sensitivity_var(self, v):
        # Maps v to the v_sen variable i.e. the gradient of v
        if isinstance(v, Value):
            return None
        assert isinstance(v, Symbol)
        try:
            return copy(self.sensitivity_map[v])
        except KeyError:
            # self.zeroes.append(self.zero_init(v))
            # return self.new_sensitivity_var(v)
            return self.zero_init(v)

    def new_sensitivity_var(self, v):
        # Create a new sensitivity variable for v. This is used to preserve
        # the single-assignment property: instead of v_sen = v_sen + x,
        # we do v_sen2 = v_sen + x. self.sensitivity_var maps to the latest
        # return value for this function.
        assert isinstance(v, Symbol)
        new_v = self.gensym(v, '∇')
        self.sensitivity_map[v] = new_v
        return new_v

    def backpropagator_var(self, v):
        # Maps v to the v_bprop variable i.e. the backpropagator for v
        return copy(self.backpropagator_map.setdefault(v, self.gensym(v, '♢')))

    def transform(self):
        args = self.primal.args
        let = self.primal.body

        if isinstance(let, Symbol):
            tmp = self.gensym('tmp')
            let = Let([(tmp, let)], tmp)
        assert isinstance(let, Let)  # TODO: could be symbol too

        # Create this sensitivity variable first (it's an argument).
        out_sen = self.new_sensitivity_var(let.body)

        forward = []
        backward = []
        for s, v in let.bindings:
            forward += self.phi(s, v)

        for s, v in reversed(let.bindings):
            backward += self.rho(s, v)

        backp_bargs = [self.backpropagator_var(s) for s, _ in let.bindings]
        backp_cargs = [self.tagged_var(s) for s, _ in let.bindings]
        backp_rargs = [self.tagged_var(arg) for arg in args]
        backp_args = backp_bargs + backp_cargs + backp_rargs
        backp_all_ret = [self.sensitivity_var(arg) for arg in args]
        backp_ret = Tuple([
            # Tuple([self.sensitivity_var(arg.label) for arg in backp_cargs]),
            Tuple(backp_all_ret[:self.nargs_closure]),
            *backp_all_ret[self.nargs_closure:]
        ])
        backp_fn = Lambda([*map(copy, backp_args), out_sen],
                          Let(self.zeroes + backward, backp_ret),
                          self.gensym)
        backp_sym = self.global_env.gen(self.name, '♢*')
        backp_fn.ref = backp_sym
        # fbuche[str(backp_sym)](backp_fn)
        self.global_env[backp_sym] = backp_fn

        backp_cl = Closure(backp_sym, backp_args)
        backp_clsym = self.gensym(self.name, '♢')
        forward.append((backp_clsym, backp_cl))
        new_body = Let(forward,
                       Tuple([self.tagged_var(let.body), backp_clsym]))

        new_args = list(map(self.tagged_var, args))
        ret_fn = Lambda(new_args, new_body, self.gensym)
        ret_sym = self.global_env.gen(self.name, '↑')
        ret_fn.ref = ret_sym
        # fbuche[str(ret_sym)](ret_fn)
        self.global_env[ret_sym] = ret_fn
        return ret_sym