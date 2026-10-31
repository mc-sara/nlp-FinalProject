"""Install application dependencies without replacing SCNet accelerator packages."""
from pathlib import Path
import importlib.metadata as md
import json
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parent
LOG = ROOT / 'artifacts' / 'environment'
LOG.mkdir(parents=True, exist_ok=True)
protected = {'torch', 'torchvision', 'torchaudio', 'triton', 'pytorch-triton', 'pytorch-triton-rocm'}
before = {}
for name in protected:
    try: before[name] = md.version(name)
    except md.PackageNotFoundError: pass
# DTK image reports a NumPy 1.x ABI; normalize before importing torch.
try:
    numpy_version = md.version('numpy')
except md.PackageNotFoundError:
    numpy_version = None
if numpy_version != '1.26.4':
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', 'numpy==1.26.4'], check=True)
    import os
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
import torch
if not torch.cuda.is_available():
    raise SystemExit('SCNet accelerator is unavailable in this image. Do not install a CUDA wheel over DTK torch.')
print('Platform torch:', torch.__version__, 'device:', torch.cuda.get_device_name(0), flush=True)
(LOG / 'platform.json').write_text(json.dumps(dict(packages=before, torch_file=torch.__file__,
    hip=torch.version.hip, cuda=torch.version.cuda, device=torch.cuda.get_device_name(0)), indent=2))
constraints = LOG / 'platform-constraints.txt'
constraints.write_text('\n'.join(f'{k}=={v}' for k,v in before.items())+'\n')
source = ROOT / 'vendor' / 'LlamaFactory'
metadata = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
requirements = [r for r in metadata['project']['dependencies'] if r.split('>')[0] not in protected]
requirements = [r for r in requirements if not r.startswith('transformers') and r != 'numpy']
requirements += ['transformers==5.8.0', 'numpy==1.26.4', 'hatchling', 'editables>=0.3,<1']
reqfile = LOG / 'requirements.txt'
reqfile.write_text('\n'.join(requirements)+'\n')
report = LOG / 'pip-plan.json'
cmd = [sys.executable, '-m', 'pip', 'install', '-c', str(constraints), '-r', str(reqfile)]
subprocess.run(cmd+['--dry-run', '--report', str(report)], check=True)
plan = json.loads(report.read_text(encoding='utf-8'))
for item in plan.get('install', []):
    name = item['metadata']['name'].lower().replace('_','-')
    if name in protected or name.startswith(('nvidia-', 'triton', 'pytorch-triton')):
        raise SystemExit(f'Refusing dependency replacement: {name}. Use matching SCNet platform packages.')
subprocess.run(cmd, check=True)
subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e',str(source)],check=True)
for name, version in before.items():
    if md.version(name) != version:
        raise SystemExit(f'Platform package changed unexpectedly: {name}')
freeze = subprocess.check_output([sys.executable,'-m','pip','freeze'], text=True)
(LOG/'pip-freeze.txt').write_text(freeze)
# A fresh interpreter processes newly installed editable .pth files.
subprocess.run([sys.executable, '-c',
    "from llamafactory.data.template import TEMPLATES; assert 'qwen3_5_nothink' in TEMPLATES"], check=True)
(LOG/'setup.complete').write_text('Application dependencies installed; model execution not yet tested.\n')
