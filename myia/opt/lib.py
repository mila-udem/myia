"""Library of optimizations."""

from ..ir import replace, \
    Graph, Constant, is_constant, is_constant_graph, GraphCloner
from ..unify import Var, var, SVar
from ..prim import ops as P, Primitive
from ..prim.py_implementations import implementations as pyimpl
from ..cconv import NestingAnalyzer

from .opt import \
    sexp_to_node, \
    PatternSubstitutionOptimization as psub, \
    pattern_replacer


#####################
# Generic variables #
#####################


X = Var('X')
Y = Var('Y')
Z = Var('Z')
X1 = Var('X1')
Y1 = Var('Y1')
X2 = Var('X2')
Y2 = Var('Y2')

C = var(is_constant)
C1 = var(is_constant)
C2 = var(is_constant)
G = var(is_constant_graph)
NIL = var(lambda x: is_constant(x) and x.value == ())

Xs = SVar(Var())
Ys = SVar(Var())
Cs = SVar(var(is_constant))


def primset_var(*prims):
    """Create a variable that matches a Primitive node."""
    return var(lambda node: is_constant(node) and node.value in prims)


###############################
# Tuple-related optimizations #
###############################


@pattern_replacer(P.getitem, (P.cons_tuple, X, Y), C)
def getitem_tuple(node, equiv):
    """Match a constant index in an explicit tuple.

    (a, b, c, ...)[0] => a
    (a, b, c, ...)[1] => b
    ...
    """
    i = equiv[C].value
    assert isinstance(i, int)
    if i == 0:
        return equiv[X]
    else:
        return sexp_to_node((P.getitem, equiv[Y], i - 1), node.graph)


@pattern_replacer(P.setitem, (P.cons_tuple, X, Y), C, Z)
def setitem_tuple(node, equiv):
    """Match a constant setitem in an explicit tuple.

    setitem((a, b, c, ...), 0, z) => (z, b, c, ...)
    setitem((a, b, c, ...), 1, z) => (a, z, c, ...)
    ...
    """
    i = equiv[C].value
    assert isinstance(i, int)
    if i == 0:
        return sexp_to_node((P.cons_tuple, equiv[Z], equiv[Y]), node.graph)
    else:
        return sexp_to_node((P.cons_tuple, equiv[X],
                             (P.setitem, equiv[Y], i - 1, equiv[Z])),
                            node.graph)


# head((a, b, ...)) => a
head_tuple = psub(
    pattern=(P.head, (P.cons_tuple, X, Y)),
    replacement=X,
    name='head_tuple'
)


# tail((a, b, ...)) => (b, ...)
tail_tuple = psub(
    pattern=(P.tail, (P.cons_tuple, X, Y)),
    replacement=Y,
    name='tail_tuple'
)


# f((a, b, ...), (p, q, ...)) => (f(a, p), f(b, q), ...)
# For f in the following list:
_BubbleBinary = primset_var(P.add)

bubble_op_cons_binary = psub(
    pattern=(_BubbleBinary, (P.cons_tuple, X1, Y1), (P.cons_tuple, X2, Y2)),
    replacement=(P.cons_tuple,
                 (_BubbleBinary, X1, X2),
                 (_BubbleBinary, Y1, Y2)),
    name='bubble_op_cons_binary'
)


# f((), ()) => () -- this is a kind of constant prop
bubble_op_nil_binary = psub(
    pattern=(_BubbleBinary, NIL, NIL),
    replacement=NIL,
    name='bubble_op_nil_binary'
)


##############################
# Arithmetic simplifications #
##############################


multiply_by_zero_l = psub(
    pattern=(P.mul, 0, X),
    replacement=0,
    name='multiply_by_zero_l'
)

multiply_by_zero_r = psub(
    pattern=(P.mul, X, 0),
    replacement=0,
    name='multiply_by_zero_r'
)

multiply_by_one_l = psub(
    pattern=(P.mul, 1, X),
    replacement=X,
    name='multiply_by_one_l'
)

multiply_by_one_r = psub(
    pattern=(P.mul, X, 1),
    replacement=X,
    name='multiply_by_one_r'
)

add_zero_l = psub(
    pattern=(P.add, 0, X),
    replacement=X,
    name='add_zero_l'
)

add_zero_r = psub(
    pattern=(P.add, X, 0),
    replacement=X,
    name='add_zero_r'
)


########################
# Constant propagation #
########################

def make_constant_prop(impl):
    """Create a constant propagator that uses the given implementations."""
    @pattern_replacer(C, Cs)
    def constant_prop(node, equiv):
        fn = equiv[C].value
        args = [ct.value for ct in equiv[Cs]]
        if isinstance(fn, Primitive):
            if fn in impl:
                return Constant(impl[fn](*args))
            else:
                return node

        elif isinstance(fn, Graph):
            # We just ignore this case for now.
            # Have to check nesting first.
            return node

        else:
            raise TypeError(f'Cannot execute {fn}.')  # pragma: no cover

    return constant_prop


constant_prop = make_constant_prop(pyimpl)


############
# Inlining #
############


def make_inliner(inline_criterion, check_recursive):
    """Create an inliner.

    Args:
        inline_criterion: A function that takes (graph, node, args) and
            returns whether the graph should be inlined or not.
        check_recursive: Check whether a function is possibly recursive
            before inlining it. If it is, don't inline.
    """
    @pattern_replacer(G, Xs)
    def inline(node, equiv):
        g = equiv[G].value
        args = equiv[Xs]

        if inline_criterion is not None:
            if not inline_criterion(g, node, args):
                return node

        if check_recursive:
            nest = NestingAnalyzer(g)
            if g in nest.graphs_accessible()[g]:
                return node

        clone = GraphCloner(total=False)
        clone.add_clone(g, node.graph, args)
        return clone[g.output]

    return inline


inline = make_inliner(inline_criterion=None, check_recursive=True)


##################
# Specialization #
##################

def make_specializer(specialize_criterion):
    """Create an specializer.

    Args:
        specialize_criterion: A function that takes a node and returns
            whether to specialize on it or not.
    """
    @pattern_replacer(G, Xs)
    def specialize(node, equiv):
        g = equiv[G].value
        xs = equiv[Xs]

        specialize = [specialize_criterion(x) for x in xs]
        if not any(specialize):
            return node

        g2 = GraphCloner(g, relation='specialized')[g]
        for x, p, s in zip(xs, g2.parameters, specialize):
            if s:
                replace(p, x)

        g2.parameters = [p for s, p in zip(specialize, g2.parameters)
                         if not s]
        new_xs = [x for s, x in zip(specialize, xs) if not s]

        return sexp_to_node((g2, *new_xs), node.graph)

    return specialize


specialize = make_specializer(specialize_criterion=is_constant)