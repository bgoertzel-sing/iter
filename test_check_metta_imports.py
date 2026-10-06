"""I1 loud-import check tests."""
import subprocess, sys, pathlib
SCRIPT = pathlib.Path(__file__).parent / 'scripts' / 'check_metta_imports.py'

def _run(tmp, body):
    (tmp / 'lib_omega.metta').write_text(body, encoding='utf-8')
    return subprocess.run([sys.executable, str(SCRIPT), str(tmp / 'lib_omega.metta')], capture_output=True, text=True)

def test_all_present(tmp_path):
    (tmp_path / 'src').mkdir(); (tmp_path / 'src/loop.metta').write_text(''); (tmp_path / 'src/helper.py').write_text('')
    r = _run(tmp_path, '!(import! &self (library Omega ./src/loop))\n!(import! &self (library Omega ./src/helper.py))\n')
    assert r.returncode == 0 and '0 missing' in r.stdout

def test_missing_is_loud(tmp_path):
    (tmp_path / 'src').mkdir(); (tmp_path / 'src/loop.metta').write_text('')
    r = _run(tmp_path, '!(import! &self (library Omega ./src/loop))\n!(import! &self (library Omega ./src/context))\n')
    assert r.returncode == 1 and 'MISSING' in r.stderr and './src/context' in r.stderr

def test_comment_ignored(tmp_path):
    r = _run(tmp_path, '; !(import! &self (library Omega ./src/gone))\n')
    assert r.returncode == 0
