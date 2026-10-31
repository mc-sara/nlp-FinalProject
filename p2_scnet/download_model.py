import json
import os
from pathlib import Path
from modelscope import snapshot_download

model=Path(os.environ['MODEL_DIR'])
model.mkdir(parents=True,exist_ok=True)
snapshot_download('Qwen/Qwen3.5-9B', local_dir=str(model), revision=os.environ.get('MODEL_REVISION','master'))
config=json.loads((model/'config.json').read_text(encoding='utf-8'))
if config.get('model_type') != 'qwen3_5':
    raise SystemExit('Unexpected model architecture; refusing to continue')
# Index-listed files must all exist before marking the download complete.
indices=list(model.glob('*.safetensors.index.json'))
if not indices: raise SystemExit('Missing safetensors weight index')
for index in indices:
    for name in set(json.loads(index.read_text(encoding='utf-8'))['weight_map'].values()):
        if not (model/name).is_file(): raise SystemExit(f'Missing weight shard: {name}')
import hashlib
manifest={str(p.relative_to(model)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in model.glob('*.json') if p.stat().st_size<10_000_000}
(model/'p2-download.complete').write_text(json.dumps({'model':'Qwen/Qwen3.5-9B',
    'revision_requested':os.environ.get('MODEL_REVISION','master'),'metadata_sha256':manifest},indent=2))
