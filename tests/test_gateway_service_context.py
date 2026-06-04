from __future__ import annotations


def test_gateway_service_context_detects_repo_root_from_module_path(tmp_path):
    from gateway.service_context import build_service_runtime_context

    repo = tmp_path / "repo"
    nested = repo / "subdir"
    nested.mkdir(parents=True)
    (repo / "agent_cli").mkdir()
    (repo / "gateway").mkdir()
    module_file = nested / "script.py"
    module_file.write_text("", encoding="utf-8")

    context = build_service_runtime_context(module_path=module_file)

    assert context.project_root == repo
    assert context.pythonpath == str(repo)


def test_gateway_service_context_prefers_explicit_project_root(tmp_path):
    from gateway.service_context import build_service_runtime_context

    context = build_service_runtime_context(project_root=tmp_path)

    assert context.project_root == tmp_path.resolve()
    assert context.pythonpath == str(tmp_path.resolve())
