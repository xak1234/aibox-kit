"""Create a portable, credential-free source bundle without local runtime data."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent.parent
skip = {'node_modules', '.venv', '.git', '.local-state', '.openai', '__pycache__', '.mypy_cache', '.ruff_cache', '.pytest_cache', 'workspace'}
paths = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
         and not any(part in skip for part in p.relative_to(root).parts)
         and p.suffix not in ('.zip', '.pyc') and not p.name.endswith('.tar.gz') and p.name != '.env']
output = root / 'terminal-source.zip'
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(paths):
        archive.write(path, 'terminal-source/' + path.relative_to(root).as_posix())
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    names = archive.namelist()
    for required in ('Dockerfile', 'README.md', 'docs/INTEGRATION.md', 'PUSH-TO-GITHUB.md', 'terminal_app/browser.py', 'terminal_app/learning.py', '.github/workflows/ci.yml', 'vendor/harness2-0.1.0-py3-none-any.whl'):
        assert 'terminal-source/' + required in names, required
print(f'{output}: {len(paths)} files, {output.stat().st_size} bytes')
