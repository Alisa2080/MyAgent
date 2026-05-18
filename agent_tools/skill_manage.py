"""Compatibility shim. New code should import from agent_tools.public.skill_manage_impl."""

import sys
from importlib import import_module

_impl = import_module("agent_tools.public.skill_manage_impl")

sys.modules[__name__] = _impl
