def test_file_tool_schemas_do_not_expose_task_id():
    from agent_tools.public.files import patch, read_file, search_files, write_file

    assert "task_id" not in read_file.args
    assert "task_id" not in write_file.args
    assert "task_id" not in patch.args
    assert "task_id" not in search_files.args

    assert "path" in read_file.args
    assert "path" in write_file.args
    assert "mode" in patch.args
    assert "pattern" in search_files.args
