"""P2: model-driven collection on the local simulator only."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor' / 'p1'))
from tieba_env.adapter import MockBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.runner import FinalAction, ToolAction, run_episode
from tieba_env.scenarios import generate_scenarios
from tieba_env.tools import SYSTEM_PROMPT, TOOL_SCHEMAS
from tieba_env.validator import classify, summarize

TEMPLATE = 'qwen3_5_nothink'
TOOLS = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)

def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def read_rows(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]

def messages_for(task, history):
    # No scenario, fault plan, delay or truth enters prompts.
    messages = [{'role': 'user', 'content': task}]
    for c in history:
        messages += [dict(role='function', content=json.dumps(
            dict(name=c['tool_name'], arguments=c['arguments']), ensure_ascii=False)),
            dict(role='observation', content=c['observation'])]
    return messages

def decode_action(raw, extractor, finish_reason='stop'):
    if finish_reason == 'length':
        raise ValueError('generation truncated; action not executed')
    parsed = extractor(raw)
    if isinstance(parsed, str):
        if '<tool_call' in raw or '<function=' in raw or not raw.strip():
            raise ValueError('malformed or empty response')
        return FinalAction(raw)
    if len(parsed) != 1 or raw.count('<tool_call>') != 1:
        raise ValueError('one tool call per turn required; no partial execution')
    name, encoded = parsed[0]
    args = json.loads(encoded)
    if not isinstance(args, dict):
        raise ValueError('arguments must be object')
    schema = next((x['function'] for x in TOOL_SCHEMAS if x['function']['name'] == name), None)
    if schema is None:
        raise ValueError(f'unknown tool: {name}')
    p = schema['parameters']
    if set(args) - set(p['properties']) or set(p.get('required', [])) - set(args):
        raise ValueError('unexpected or missing arguments')
    for k, v in args.items():
        kind = p['properties'][k]['type']
        if kind == 'string' and not isinstance(v, str):
            raise ValueError(f'{k} must be string')
        if kind == 'integer' and (not isinstance(v, int) or isinstance(v, bool)):
            raise ValueError(f'{k} must be integer')
    return ToolAction(name, args)

class QwenPolicy:
    def __init__(self, model, adapter=None, context=8192, max_new=512, demonstrations=None):
        from llamafactory.chat import ChatModel
        config = dict(model_name_or_path=model, template=TEMPLATE,
            finetuning_type='lora', infer_backend='huggingface',
            infer_dtype='bfloat16', flash_attn='disabled', do_sample=False, max_new_tokens=max_new)
        if adapter:
            config['adapter_name_or_path'] = adapter
        self.chat = ChatModel(config)
        self.demonstrations = demonstrations or []
        self.context, self.max_new = context, max_new
        self.reset()

    def reset(self):
        self.records, self.errors = [], []

    def act(self, history, scenario):
        messages = self.demonstrations + messages_for(scenario.task, history)
        engine = self.chat.engine
        ids, _ = engine.template.encode_oneturn(engine.tokenizer,
            messages + [dict(role='assistant', content='')], SYSTEM_PROMPT, TOOLS)
        if len(ids) + self.max_new > self.context:
            self.errors.append('context budget exceeded; no silent truncation')
            return 'context budget exceeded'
        # Device failures abort the job rather than becoming model errors.
        response = self.chat.chat(messages, system=SYSTEM_PROMPT, tools=TOOLS,
                                  do_sample=False, max_new_tokens=self.max_new)[0]
        self.records.append(dict(raw=response.response_text, prompt_tokens=response.prompt_length,
            completion_tokens=response.response_length, finish_reason=response.finish_reason))
        try:
            return decode_action(response.response_text, engine.template.extract_tool, response.finish_reason)
        except (ValueError, TypeError, KeyError) as exc:
            self.errors.append(str(exc))
            return f'parse_error: {exc}'

def collect(a):
    if (a.group == 'A' and a.adapter) or (a.group != 'A' and not a.adapter):
        raise ValueError('A uses no adapter; B/C require their adapter')
    out = Path(a.out)
    if out.exists():
        raise FileExistsError('Use a new output path to preserve existing runs')
    scenarios = generate_scenarios(a.split, per_family=a.per_family, seed=a.seed)
    policy = QwenPolicy(a.model, a.adapter, a.context, a.max_new)
    manifest = {k: v for k, v in vars(a).items() if k != 'func'}
    manifest.update(template=TEMPLATE, decoding='greedy', tools=TOOL_SCHEMAS,
                    environment='P1 bundled mock, unchanged defaults')
    dump(out.with_suffix('.manifest.json'), manifest)
    with out.open('x', encoding='utf-8') as stream:
        for scenario in scenarios:
            policy.reset()
            env = MockTiebaEnv()
            traj = run_episode(env, TiebaAdapter(MockBackend(env)), scenario, policy, group=a.group,
                               trajectory_id=f'{a.group}-{a.seed}-{scenario.scenario_id}')
            traj.update(model_outputs=policy.records, parsing_errors=policy.errors,
                        p2_config=manifest, origin='model', p1_label=classify(traj))
            stream.write(json.dumps(traj, ensure_ascii=False) + '\n')
            stream.flush()
            print(traj['trajectory_id'], traj['p1_label'], flush=True)
    dump(out.with_suffix('.metrics.provisional.json'), summarize(read_rows(out)))

def trajectory_hash(traj):
    return hashlib.sha256(json.dumps(traj, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()

def review(a):
    if Path(a.out).exists():
        raise FileExistsError(a.out)
    dump(a.out, [dict(trajectory_id=x['trajectory_id'], sha256=trajectory_hash(x),
        p1_label=classify(x), approved=False, reviewer='', label='', notes='') for x in read_rows(a.input)])

def sharegpt(traj):
    role_map = dict(user='human', function='function_call', observation='observation')
    conversations = [{'from': role_map[m['role']], 'value': m['content']}
                     for m in messages_for(traj['task'], traj['tool_calls'])]
    final = traj.get('final_answer')
    if not isinstance(final, str) or not final.strip():
        raise ValueError('training trajectory requires nonempty final answer')
    conversations.append({'from': 'gpt', 'value': final})
    return dict(system=SYSTEM_PROMPT, tools=TOOLS, conversations=conversations)

def prepare(a):
    rows = read_rows(a.input)
    review_list = json.loads(Path(a.review).read_text(encoding='utf-8'))
    reviews = {x['trajectory_id']: x for x in review_list}
    if len(reviews) != len(review_list) or len({x['trajectory_id'] for x in rows}) != len(rows):
        raise ValueError('duplicate trajectory IDs')
    normal, recovered = [], []
    for t in rows:
        r = reviews.get(t['trajectory_id'], {})
        if r.get('approved') is not True:
            continue
        if not r.get('reviewer') or r.get('sha256') != trajectory_hash(t):
            raise ValueError('missing reviewer or trajectory changed after review')
        if t.get('origin') not in ('model', 'human_corrected'):
            raise ValueError('Training requires model trajectories or documented human corrections')
        if t.get('origin') == 'human_corrected' and not t.get('correction_provenance'):
            raise ValueError('Human corrections require correction_provenance')
        if t['scenario']['split'] != 'train':
            raise ValueError('test/other split must never enter training')
        if t.get('model_errors') or t.get('hit_step_limit') or t.get('parsing_errors'):
            raise ValueError('raw failed trajectory is not a positive example')
        if t.get('system_prompt') != SYSTEM_PROMPT:
            raise ValueError('inconsistent system prompt')
        label = r.get('label')
        if label not in ('SUCCESS_NORMAL', 'SUCCESS_RECOVERED'):
            raise ValueError('approved label must be SUCCESS_NORMAL or SUCCESS_RECOVERED')
        if (label == 'SUCCESS_NORMAL') != (t['scenario']['family'] == 'normal'):
            raise ValueError('label/family mismatch')
        (normal if label == 'SUCCESS_NORMAL' else recovered).append((t, sharegpt(t)))
    if not normal or not recovered:
        raise ValueError('Need reviewed normal AND recovery samples; do not fabricate missing data')
    output = Path(a.out)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError('dataset output must be empty')
    b, c = list(normal), list(normal) + list(recovered)
    rng = random.Random(42)
    rng.shuffle(b)
    rng.shuffle(c)
    info = {}
    for group, samples in [('B', b), ('C', c)]:
        name = f'tieba_{group}'
        dump(output / f'{name}.json', [s for _, s in samples])
        info[name] = dict(file_name=f'{name}.json', formatting='sharegpt',
            columns=dict(messages='conversations', system='system', tools='tools'),
            tags=dict(role_tag='from', content_tag='value', user_tag='human',
                      assistant_tag='gpt', observation_tag='observation', function_tag='function_call'))
    dump(output / 'dataset_info.json', info)
    dump(output / 'provenance.json', dict(B=[t['trajectory_id'] for t, _ in b],
        C=[t['trajectory_id'] for t, _ in c], budget_note='Equal update steps, not equal supervised tokens.'))

def train_config(a):
    import yaml
    config = dict(model_name_or_path=a.model, stage='sft', do_train=True,
        finetuning_type='lora', lora_rank=8, lora_alpha=16, lora_target='all',
        freeze_vision_tower=True, freeze_multi_modal_projector=True,
        template=TEMPLATE, dataset=f'tieba_{a.group}', dataset_dir=str(Path(a.data).resolve()),
        cutoff_len=8192, train_on_prompt=False, mask_history=False, packing=False,
        preprocessing_num_workers=1, dataloader_num_workers=0,
        output_dir=str(Path(a.output).resolve()), overwrite_output_dir=False,
        per_device_train_batch_size=1, gradient_accumulation_steps=8, learning_rate=5e-5,
        max_steps=a.steps, lr_scheduler_type='cosine', warmup_ratio=0.1,
        bf16=True, flash_attn='disabled', gradient_checkpointing=True, optim='adamw_torch',
        logging_steps=1, save_steps=20, save_total_limit=2, report_to='none', seed=42, data_seed=42)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('collect')
    p.add_argument('--model', required=True)
    p.add_argument('--adapter')
    p.add_argument('--group', choices=['A', 'B', 'C'], default='A')
    p.add_argument('--split', choices=['train', 'test'], required=True)
    p.add_argument('--per-family', type=int, default=20)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--context', type=int, default=8192)
    p.add_argument('--max-new', type=int, default=512)
    p.add_argument('--out', required=True)
    p.set_defaults(func=collect)
    p = sub.add_parser('review')
    p.add_argument('--input', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=review)
    p = sub.add_parser('prepare')
    p.add_argument('--input', required=True)
    p.add_argument('--review', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=prepare)
    p = sub.add_parser('config')
    p.add_argument('--model', required=True)
    p.add_argument('--group', choices=['B', 'C'], required=True)
    p.add_argument('--data', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--steps', type=int, default=100)
    p.add_argument('--out', required=True)
    p.set_defaults(func=train_config)
    args = parser.parse_args()
    args.func(args)

if __name__ == '__main__':
    main()
