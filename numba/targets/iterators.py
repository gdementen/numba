"""
Implementation of various iterable and iterator types.
"""

from numba import types, cgutils
from numba.targets.imputils import (
    builtin, implement, iternext_impl, call_iternext, call_getiter,
    struct_factory, impl_ret_borrowed, impl_ret_new_ref)


@builtin
@implement('getiter', types.Kind(types.IteratorType))
def iterator_getiter(context, builder, sig, args):
    [it] = args
    return impl_ret_borrowed(context, builder, sig.return_type, it)

#-------------------------------------------------------------------------------
# builtin `enumerate` implementation

@struct_factory(types.EnumerateType)
def make_enumerate_cls(enum_type):
    """
    Return the Structure representation of the given *enum_type* (an
    instance of types.EnumerateType).
    """
    return cgutils.create_struct_proxy(enum_type)


@builtin
@implement(enumerate, types.Kind(types.IterableType))
@implement(enumerate, types.Kind(types.IterableType), types.Kind(types.Integer))
def make_enumerate_object(context, builder, sig, args):
    assert len(args) == 1 or len(args) == 2 # enumerate(it) or enumerate(it, start)
    srcty = sig.args[0]

    if len(args) == 1:
        src = args[0]
        start_val = context.get_constant(types.intp, 0)
    elif len(args) == 2:
        src = args[0]
        start_val = context.cast(builder, args[1], sig.args[1], types.intp)

    iterobj = call_getiter(context, builder, srcty, src)

    enumcls = make_enumerate_cls(sig.return_type)
    enum = enumcls(context, builder)

    countptr = cgutils.alloca_once(builder, start_val.type)
    builder.store(start_val, countptr)

    enum.count = countptr
    enum.iter = iterobj

    res = enum._getvalue()
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@builtin
@implement('iternext', types.Kind(types.EnumerateType))
@iternext_impl
def iternext_enumerate(context, builder, sig, args, result):
    [enumty] = sig.args
    [enum] = args

    enumcls = make_enumerate_cls(enumty)
    enum = enumcls(context, builder, value=enum)

    count = builder.load(enum.count)
    ncount = builder.add(count, context.get_constant(types.intp, 1))
    builder.store(ncount, enum.count)

    srcres = call_iternext(context, builder, enumty.source_type, enum.iter)
    is_valid = srcres.is_valid()
    result.set_valid(is_valid)

    with builder.if_then(is_valid):
        srcval = srcres.yielded_value()
        result.yield_(cgutils.make_anonymous_struct(builder, [count, srcval]))


#-------------------------------------------------------------------------------
# builtin `zip` implementation

@struct_factory(types.ZipType)
def make_zip_cls(zip_type):
    """
    Return the Structure representation of the given *zip_type* (an
    instance of types.ZipType).
    """
    return cgutils.create_struct_proxy(zip_type)

@builtin
@implement(zip, types.VarArg(types.Any))
def make_zip_object(context, builder, sig, args):
    zip_type = sig.return_type

    assert len(args) == len(zip_type.source_types)

    zipcls = make_zip_cls(zip_type)
    zipobj = zipcls(context, builder)

    for i, (arg, srcty) in enumerate(zip(args, sig.args)):
        zipobj[i] = call_getiter(context, builder, srcty, arg)

    res = zipobj._getvalue()
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@builtin
@implement('iternext', types.Kind(types.ZipType))
@iternext_impl
def iternext_zip(context, builder, sig, args, result):
    [zip_type] = sig.args
    [zipobj] = args

    zipcls = make_zip_cls(zip_type)
    zipobj = zipcls(context, builder, value=zipobj)

    if len(zipobj) == 0:
        # zip() is an empty iterator
        result.set_exhausted()
        return

    is_valid = context.get_constant(types.boolean, True)
    values = []

    for iterobj, srcty in zip(zipobj, zip_type.source_types):
        srcres = call_iternext(context, builder, srcty, iterobj)
        is_valid = builder.and_(is_valid, srcres.is_valid())
        values.append(srcres.yielded_value())

    result.set_valid(is_valid)
    with builder.if_then(is_valid):
        result.yield_(cgutils.make_anonymous_struct(builder, values))


#-------------------------------------------------------------------------------
# generator implementation

@builtin
@implement('iternext', types.Kind(types.Generator))
@iternext_impl
def iternext_zip(context, builder, sig, args, result):
    genty, = sig.args
    gen, = args
    # XXX We should link with the generator's library.
    # Currently, this doesn't make a difference as the library has already
    # been linked for the generator init function.
    impl = context.get_generator_impl(genty)
    status, retval = impl(context, builder, sig, args)
    with cgutils.if_likely(builder, status.is_ok):
        result.set_valid(True)
        result.yield_(retval)
    with cgutils.if_unlikely(builder, status.is_stop_iteration):
        result.set_exhausted()
    with cgutils.if_unlikely(builder,
                             builder.and_(status.is_error,
                                          builder.not_(status.is_stop_iteration))):
        context.call_conv.return_status_propagate(builder, status)
