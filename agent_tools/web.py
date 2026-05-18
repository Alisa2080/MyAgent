"""Compatibility shim. New code should import from agent_tools.public.web."""

import sys
from importlib import import_module

_impl = import_module("agent_tools.public.web")

sys.modules[__name__] = _impl
