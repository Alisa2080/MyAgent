"""Compatibility shim. New code should import from agent_tools.shared.file_policy."""

import sys
from importlib import import_module

_impl = import_module("agent_tools.shared.file_policy")

sys.modules[__name__] = _impl
