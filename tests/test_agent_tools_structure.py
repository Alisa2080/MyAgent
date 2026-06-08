import subprocess
from pathlib import Path


def test_agent_tools_has_no_tracked_generated_cache_dirs():
    result = subprocess.run(
        ["git", "ls-files", "agent_tools"],
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_cache_files = [
        line for line in result.stdout.splitlines()
        if "__pycache__" in Path(line).parts
    ]
    assert tracked_cache_files == []


def test_agent_tools_has_no_editor_copy_python_files():
    copy_files = [path for path in Path("agent_tools").rglob("* copy.py")]
    assert copy_files == []


def test_agent_core_runtime_imports_preferred_public_facades():
    delegation_source = Path("agent_core/delegation.py").read_text()
    catalog_source = Path("agent_core/tool_catalog.py").read_text()

    assert "from agent_tools.public." in delegation_source
    assert "from agent_tools.public.memory import memory_manage" in catalog_source
