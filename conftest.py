"""Pytest configuration: ensure module-level patches don't leak between test files."""
import pytest
import importlib
import sys


@pytest.fixture(autouse=True)
def restore_wmtm_modules():
    """Save and restore wmtm module attributes to prevent cross-test pollution."""
    # Save the original run_goalchainer_over_wmtm function
    saved = {}
    try:
        import wmtm.goalchainer_bridge as gb
        saved['gc_run'] = getattr(gb, 'run_goalchainer_over_wmtm', None)
    except ImportError:
        pass
    
    yield
    
    # Restore after test
    if 'gc_run' in saved and saved['gc_run'] is not None:
        try:
            import wmtm.goalchainer_bridge as gb
            gb.run_goalchainer_over_wmtm = saved['gc_run']
        except ImportError:
            pass
