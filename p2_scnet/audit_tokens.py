"""Audit exact template token lengths before SFT; never silently truncate."""
import argparse
import json
from pathlib import Path
from p2 import TEMPLATE, dump

def main():
    from llamafactory.hparams import DataArguments, ModelArguments
    from llamafactory.model import load_tokenizer
    from llamafactory.data import get_template_and_fix_tokenizer
    p=argparse.ArgumentParser()
    p.add_argument('--model',required=True)
    p.add_argument('--data',required=True)
    p.add_argument('--cutoff',type=int,default=8192)
    a=p.parse_args()
    module=load_tokenizer(ModelArguments(model_name_or_path=a.model))
    tok=module['tokenizer']
    template=get_template_and_fix_tokenizer(tok,DataArguments(template=TEMPLATE))
    roles={'human':'user','gpt':'assistant','function_call':'function','observation':'observation'}
    stats={}
    for group in ['B','C']:
        rows=json.loads((Path(a.data)/f'tieba_{group}.json').read_text(encoding='utf-8'))
        sizes=[]
        supervised=[]
        for row in rows:
            messages=[{'role':roles[m['from']],'content':m['value']} for m in row['conversations']]
            pairs=template.encode_multiturn(tok,messages,row['system'],row['tools'])
            sizes.append(sum(len(s)+len(t) for s,t in pairs))
            supervised.append(sum(len(t) for _,t in pairs))
        if not sizes or max(sizes)>a.cutoff:
            raise ValueError(f'{group}: empty data or sample exceeds cutoff; do not silently truncate')
        stats[group]={'samples':len(rows),'max_tokens':max(sizes),
            'supervised_tokens':sum(supervised),'mean_supervised_tokens':sum(supervised)/len(rows)}
    dump(Path(a.data)/'token_audit.json',stats)
    print(json.dumps(stats,indent=2))

if __name__=='__main__': main()
