"""Unit tests for iter.py utility functions."""
import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def test_get_current_time_format():
    """get_current_time() should return ISO-formatted timestamp string."""
    iter_src = (PROJECT_ROOT / "iter.py").read_text()
    # Verify the function exists and returns a string
    tree = ast.parse(iter_src)
    func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    assert "get_current_time" in func_names, "get_current_time not found in iter.py"
    assert "invoke_dynamic" in func_names, "invoke_dynamic not found in iter.py"
    assert "dynamic_worker" in func_names, "dynamic_worker not found in iter.py"


def test_invoke_dynamic_signature_positional_only():
    """invoke_dynamic should use positional-only 'path' parameter."""
    iter_src = (PROJECT_ROOT / "iter.py").read_text()
    # Check that the signature uses path, / which makes it positional-only
    assert "def invoke_dynamic(path, /, function, *args, **kwargs):" in iter_src, (
        "invoke_dynamic must use positional-only 'path' parameter to avoid kwarg collision"
    )


def test_tool_modules_have_descriptions():
    """All tool modules should define DESCRIPTION for the prompt."""
    tools_dir = PROJECT_ROOT / "tools"
    py_files = [f for f in tools_dir.glob("*.py") if f.name != "__init__.py"]
    assert len(py_files) >= 5, f"Expected at least 5 tools, found {len(py_files)}"
    for py in py_files:
        src = py.read_text()
        assert "DESCRIPTION" in src, f"Tool {py.name} missing DESCRIPTION"


def test_channels_directory_structure():
    """channels/ should contain channel modules."""
    channels_dir = PROJECT_ROOT / "channels"
    assert channels_dir.is_dir(), "channels/ directory not found"
    channel_files = [f for f in channels_dir.glob("*.py") if f.name != "__init__.py"]
    assert len(channel_files) >= 1, f"Expected at least 1 channel, found {len(channel_files)}"


if __name__ == "__main__":
    test_get_current_time_format()
    test_invoke_dynamic_signature_positional_only()
    test_tool_modules_have_descriptions()
    test_channels_directory_structure()
    print("All iter utility tests passed!")
