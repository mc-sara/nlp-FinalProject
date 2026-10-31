"""Apply PLAN 5.2 thresholds to the ten P3-reviewed diagnostic episodes."""
import json
from pathlib import Path
from p2 import read_rows, trajectory_hash

def assess(rows, reviews):
    if len(rows)!=10 or len({t['trajectory_id'] for t in rows})!=10:
        raise ValueError('Expected ten distinct diagnostic episodes')
    count=0
    for t in rows:
        r=reviews.get(t['trajectory_id'],{})
        if t.get('origin')!='fewshot_diagnostic':
            raise ValueError('Expected few-shot diagnostic records')
        if r.get('approved') is not True or not r.get('reviewer') or r.get('sha256')!=trajectory_hash(t):
            raise ValueError('All ten diagnostic labels require current P3 review (including failures)')
        if not r.get('label'):
            raise ValueError('Missing reviewed label')
        count+=r['label']=='SUCCESS_RECOVERED'
    if count<3 or count>8:
        raise ValueError(f'{count}/10: PLAN requires investigation or experiment redesign before training')
    return count

if __name__=='__main__':
    root=Path(__file__).resolve().parent/'artifacts/runs'
    rows=read_rows(root/'fewshot_31415.jsonl')
    review=json.loads((root/'fewshot_31415.review.json').read_text(encoding='utf-8'))
    if len({r['trajectory_id'] for r in review})!=len(review):
        raise ValueError('Duplicate review IDs')
    print('Reviewed few-shot:',assess(rows,{r['trajectory_id']:r for r in review}),'/ 10; PLAN allows training')
