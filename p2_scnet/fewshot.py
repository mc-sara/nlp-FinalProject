"""PLAN 5.2: three reviewed recovery demonstrations, ten diagnostic episodes."""
import argparse
import json
from pathlib import Path
import random
from p2 import (QwenPolicy, read_rows, trajectory_hash, messages_for, generate_scenarios,
                MockTiebaEnv, TiebaAdapter, MockBackend, run_episode, classify, dump)

def demonstration_prefix(rows, reviews):
    accepted=[]
    for t in rows:
        r=reviews.get(t['trajectory_id'], {})
        if not (r.get('approved') is True and r.get('label')=='SUCCESS_RECOVERED'):
            continue
        if not r.get('reviewer') or r.get('sha256')!=trajectory_hash(t):
            raise ValueError('Missing reviewer or stale review')
        if t['scenario']['split']!='train' or t.get('model_errors') or t.get('hit_step_limit') or t.get('parsing_errors'):
            raise ValueError('Demonstrations must be successful training trajectories')
        if not t.get('final_answer'):
            raise ValueError('Missing final answer')
        accepted.append(t)
    if len(accepted)<3:
        raise ValueError('Need three reviewed recovery demonstrations; obtain or correct them with P3 first')
    selected=sorted(accepted,key=lambda t:t['trajectory_id'])[:3]
    prefix=[]
    for t in selected:
        prefix.extend(messages_for(t['task'],t['tool_calls']))
        prefix.append(dict(role='assistant',content=t['final_answer']))
    return prefix, selected

def main():
    p=argparse.ArgumentParser()
    for name in ['model','input','review','out']: p.add_argument('--'+name,required=True)
    a=p.parse_args()
    out=Path(a.out)
    if out.exists(): raise FileExistsError(out)
    review=json.loads(Path(a.review).read_text(encoding='utf-8'))
    prefix, demos=demonstration_prefix(read_rows(a.input),{r['trajectory_id']:r for r in review})
    # Development diagnostic only: never draw from the final test split.
    families=['unknown_visible','unknown_delayed','unknown_not_published','unknown_resubmit_unknown']
    scenarios=generate_scenarios('train',per_family=3,seed=31415,families=families)
    random.Random(31415).shuffle(scenarios)
    policy=QwenPolicy(a.model,context=32768,demonstrations=prefix)
    manifest=dict(demonstrations=[dict(id=t['trajectory_id'],sha256=trajectory_hash(t)) for t in demos],
        seed=31415,episodes=10,context=32768,
        purpose='few-shot development diagnostic, not final A/B/C evaluation; shares training content pool')
    dump(out.with_suffix('.manifest.json'),manifest)
    labels=[]
    with out.open('x',encoding='utf-8') as f:
        for s in scenarios[:10]:
            policy.reset()
            env=MockTiebaEnv()
            t=run_episode(env,TiebaAdapter(MockBackend(env)),s,policy,group='fewshot',
                          trajectory_id=f'fewshot-31415-{s.scenario_id}')
            t.update(origin='fewshot_diagnostic',model_outputs=policy.records,parsing_errors=policy.errors)
            label=classify(t)
            t['p1_label']=label
            labels.append(label)
            f.write(json.dumps(t,ensure_ascii=False)+'\n'); f.flush()
            print(t['trajectory_id'],label,flush=True)
    dump(out.with_suffix('.provisional.json'),dict(successes=labels.count('SUCCESS_RECOVERED'),total=10,
        note='P3 must verify the ten labels before applying PLAN thresholds. Do not use diagnostics as SFT data.'))

if __name__=='__main__': main()
