"""Private helper for binding section methods to bot.py's live module globals.

This keeps legacy imports and monkeypatches stable while feature methods live in
separate files. The bot module remains the single owner of shared runtime state.
"""
from types import FunctionType


def _bind_function(fn, runtime_globals):
    bound = FunctionType(fn.__code__, runtime_globals, fn.__name__, fn.__defaults__, fn.__closure__)
    bound.__kwdefaults__ = fn.__kwdefaults__
    bound.__annotations__ = dict(getattr(fn, '__annotations__', {}))
    bound.__dict__.update(getattr(fn, '__dict__', {}))
    bound.__doc__ = fn.__doc__
    bound.__qualname__ = fn.__qualname__
    bound.__module__ = runtime_globals.get('__name__', fn.__module__)
    return bound


def bind_section_globals(section_class, runtime_globals):
    """Bind methods to the canonical bot globals without changing their code."""
    for name, descriptor in tuple(vars(section_class).items()):
        if isinstance(descriptor, staticmethod):
            setattr(section_class, name, staticmethod(_bind_function(descriptor.__func__, runtime_globals)))
        elif isinstance(descriptor, classmethod):
            setattr(section_class, name, classmethod(_bind_function(descriptor.__func__, runtime_globals)))
        elif isinstance(descriptor, property):
            setattr(section_class, name, property(
                _bind_function(descriptor.fget, runtime_globals) if descriptor.fget else None,
                _bind_function(descriptor.fset, runtime_globals) if descriptor.fset else None,
                _bind_function(descriptor.fdel, runtime_globals) if descriptor.fdel else None,
                descriptor.__doc__,
            ))
        elif isinstance(descriptor, FunctionType):
            setattr(section_class, name, _bind_function(descriptor, runtime_globals))
