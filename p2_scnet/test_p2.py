"""CPU integration tests; no model weights or accelerator needed."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import p2
from tieba_env.policies import ReferencePolicy

# Use the actual pinned LlamaFactory parser without importing its ML runtime.
spec=importlib.util.spec_from_file_location('lf_tools',p2.ROOT/'vendor/LlamaFactory/src/llamafactory/data/tool_utils.py')
lf_tools=importlib.util.module_from_spec(spec)
import sys
sys.modules['lf_tools']=lf_tools
spec.loader.exec_module(lf_tools)
extract=lf_tools.Qwen35ToolUtils.tool_extractor

def example(family='normal', split='train'):
    s=p2.generate_scenarios(split,per_family=1,families=[family],seed=7)[0]
    e=p2.MockTiebaEnv()
    t=p2.run_episode(e,p2.TiebaAdapter(p2.MockBackend(e)),s,ReferencePolicy(verify_budget=5))
    t['origin']='model'  # Synthetic metadata for testing the ingestion contract only.
    return t

class Integration(unittest.TestCase):
    def test_actual_qwen35_parser_roundtrip(self):
        call=lf_tools.FunctionCall('publish',json.dumps(dict(kw='测试',title='标题',content='正文')))
        raw=lf_tools.Qwen35ToolUtils.function_formatter([call])
        action=p2.decode_action(raw,extract)
        self.assertEqual(action.arguments['content'],'正文')

    def test_malformed_truncated_multiple_and_wrong_type_rejected(self):
        raw=lf_tools.Qwen35ToolUtils.function_formatter([lf_tools.FunctionCall('get_candidates','{}')])
        for bad,reason in [(raw,'length'),(raw+raw,'stop'),('<tool_call>broken','stop')]:
            with self.assertRaises(ValueError): p2.decode_action(bad,extract,reason)
        wrong=lf_tools.Qwen35ToolUtils.function_formatter([lf_tools.FunctionCall('publish','{"kw":12,"title":"a","content":"b"}')])
        with self.assertRaises(ValueError): p2.decode_action(wrong,extract)

    def test_prompt_contains_observations_not_truth(self):
        t=example('unknown_delayed')
        text=json.dumps(p2.messages_for(t['task'],t['tool_calls']))
        for key in ['fault_plan','expected_post_count','delay_k','truth','scenario_id']:
            self.assertNotIn(key,text)
        self.assertEqual(p2.messages_for(t['task'],t['tool_calls'])[2]['content'],t['tool_calls'][0]['observation'])

    def test_five_simulator_families(self):
        for family in ['normal','unknown_visible','unknown_delayed','unknown_not_published','unknown_resubmit_unknown']:
            t=example(family)
            self.assertIn(p2.classify(t),['SUCCESS_NORMAL','SUCCESS_RECOVERED'])

    def test_conversion_excludes_private_metadata(self):
        t=example(); t['cookies']={'raw':'SECRET_SENTINEL'}
        s=p2.sharegpt(t)
        self.assertNotIn('SECRET_SENTINEL',json.dumps(s))
        self.assertEqual(s['conversations'][1]['from'],'function_call')
        self.assertEqual(s['conversations'][2]['from'],'observation')
        self.assertEqual(json.loads(s['tools']),p2.TOOL_SCHEMAS)

    def prepare_fixture(self,tmp,rows):
        inp=Path(tmp)/'rows.jsonl'; inp.write_text('\n'.join(json.dumps(t) for t in rows),encoding='utf-8')
        rev=Path(tmp)/'review.json'
        reviews=[dict(trajectory_id=t['trajectory_id'],sha256=p2.trajectory_hash(t),approved=True,
                      reviewer='unit-test',label=p2.classify(t)) for t in rows]
        p2.dump(rev,reviews)
        return SimpleNamespace(input=str(inp),review=str(rev),out=str(Path(tmp)/'data'))

    def test_scripted_data_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            t=example(); t['origin']='scripted_test_fixture'
            a=self.prepare_fixture(tmp,[t,example('unknown_visible')])
            with self.assertRaisesRegex(ValueError,'Training requires'): p2.prepare(a)

    def test_fewshot_prefix_requires_three_current_reviews(self):
        from fewshot import demonstration_prefix
        rows=[example(f) for f in ['unknown_visible','unknown_delayed','unknown_not_published']]
        reviews={t['trajectory_id']:dict(approved=True,label='SUCCESS_RECOVERED',reviewer='test',sha256=p2.trajectory_hash(t)) for t in rows}
        prefix,selected=demonstration_prefix(rows,reviews)
        self.assertEqual(len(selected),3)
        self.assertEqual(prefix[-1]['role'],'assistant')
        with self.assertRaisesRegex(ValueError,'three'): demonstration_prefix(rows[:2],reviews)
        rows[0]['final_answer']='edited'
        with self.assertRaisesRegex(ValueError,'stale'): demonstration_prefix(rows,reviews)

    def test_fewshot_gate_thresholds(self):
        from assess_fewshot import assess
        rows=[]
        for i in range(10):
            t=example('unknown_visible'); t['trajectory_id']=str(i); t['origin']='fewshot_diagnostic'
            rows.append(t)
        for successes in [2,3,8,9]:
            reviews={t['trajectory_id']:dict(approved=True,reviewer='test',sha256=p2.trajectory_hash(t),
                label='SUCCESS_RECOVERED' if i<successes else 'FAIL') for i,t in enumerate(rows)}
            if 3<=successes<=8: self.assertEqual(assess(rows,reviews),successes)
            else:
                with self.assertRaisesRegex(ValueError,'redesign'): assess(rows,reviews)

    def test_reviewed_B_C_split_and_data_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.prepare_fixture(tmp,[example(),example('unknown_visible')])
            p2.prepare(a)
            b=json.loads((Path(a.out)/'tieba_B.json').read_text(encoding='utf-8'))
            c=json.loads((Path(a.out)/'tieba_C.json').read_text(encoding='utf-8'))
            self.assertEqual((len(b),len(c)),(1,2))
            self.assertEqual(json.loads((Path(a.out)/'dataset_info.json').read_text(encoding='utf-8'))['tieba_B']['tags']['observation_tag'],'observation')

    def test_test_split_and_stale_review_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=self.prepare_fixture(tmp,[example(split='test'),example('unknown_visible')])
            with self.assertRaisesRegex(ValueError,'test/other'): p2.prepare(a)
        with tempfile.TemporaryDirectory() as tmp:
            a=self.prepare_fixture(tmp,[example(),example('unknown_visible')])
            rows=p2.read_rows(a.input); rows[0]['final_answer']='changed'
            Path(a.input).write_text('\n'.join(json.dumps(t) for t in rows))
            with self.assertRaisesRegex(ValueError,'changed'): p2.prepare(a)

if __name__=='__main__': unittest.main(verbosity=2)
