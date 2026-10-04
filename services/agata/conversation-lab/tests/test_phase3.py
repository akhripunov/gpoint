import copy
import importlib.util
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LAB=Path(__file__).resolve().parents[1]

def load_module(name, path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class Phase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p=load_module('pilot',LAB/'evaluators/pilot.py')
        cls.a=load_module('adapters',LAB/'evaluators/adapters.py')
        cls.b=load_module('budget',LAB/'evaluators/budget.py')
        cls.r=load_module('ratings',LAB/'evaluators/ratings.py')
        cls.frozen=cls.p.load_frozen_pilot(LAB)

    def test_budget_fixture(self):
        budget=self.b.load_budget(LAB);budget['billing_status']='VERIFIED';budget['full_pilot_max_usd']=20
        return budget

    def complete_ratings(self, packet):
        ratings=self.r.empty_ratings(packet,LAB)
        for row in ratings['ratings']:
            row['scores']={dimension:3 for dimension in self.r.DIMENSIONS}
            row['supplemental']={dimension:3 for dimension in self.r.SUPPLEMENTAL}
        for preference in ratings['pairwise_preferences']:
            preference['preferred_label']='tie'
        return ratings

    def test_exactly_sixteen_frozen_scenarios_and_all_hashes(self):
        self.assertEqual(len(self.frozen['scenario_ids']),16)
        self.assertEqual(len(set(self.frozen['scenario_hashes'].values())),16)
        self.assertEqual(len([x['memory'] for x in self.frozen['scenario_input_hashes'].values()]),16)
        self.assertEqual(len([x['context'] for x in self.frozen['scenario_input_hashes'].values()]),16)
        tampered=copy.deepcopy(self.frozen);tampered['scenario_hashes'][tampered['scenario_ids'][0]]='bad'
        with self.assertRaises(ValueError):self.p.validate_frozen_pilot(LAB,tampered)

    def test_provider_payload_contract_has_no_future_or_rubric(self):
        for model,cls in [('gpt-6.1-sol',self.a.OpenAIResponsesAdapter),('claude-sonnet-5-5',self.a.AnthropicMessagesAdapter),('gemini-3.8-flash',self.a.GeminiGenerateContentAdapter)]:
            payload=self.p.build_invocation(LAB,self.frozen,'long_dialogue-01',model,0,[])
            built=cls().build(payload)
            body=json.dumps(built,ensure_ascii=False)
            self.assertNotIn('evaluation_notes',body);self.assertNotIn('expected_behavior',body)
            self.assertNotIn('dialogue_turns',body);self.assertNotIn('evaluation_notes',body)
            self.assertIn('AGATA request context',body)
            self.assertNotIn('Three dated preferences compete',body)

    def test_turn_replay_is_candidate_specific_and_bounded_to_reached_turn(self):
        scenario=self.p.scenario_by_id(LAB,self.frozen,'long_dialogue-01')
        history=[]
        for i,turn in enumerate(scenario['dialogue_turns'][:4]):
            history.append({'role':turn['role'],'text':turn['text'] if turn['role']=='user' else 'candidate response'})
        payload=self.p.build_invocation(LAB,self.frozen,'long_dialogue-01','gpt-6.1-sol',2,history)
        self.assertEqual(payload['history'],history)
        self.assertEqual(payload['user_turn'],self.p.scenario_by_id(LAB,self.frozen,'long_dialogue-01')['dialogue_turns'][4]['text'])
        self.assertNotIn('будущий секрет',json.dumps(payload,ensure_ascii=False))

    def test_candidate_payloads_share_canonical_agata_worker_request(self):
        first_user=self.p.scenario_by_id(LAB,self.frozen,'long_memory-01')['dialogue_turns'][0]['text']
        history=[{'role':'user','text':first_user},{'role':'assistant','text':'ответ первого кандидата'}]
        invocation=self.p.build_invocation(LAB,self.frozen,'long_memory-01','gpt-6.1-sol',1,history)
        canonical=invocation['core_request']
        self.assertEqual(canonical['history'],[{'role':'user','content':first_user},{'role':'assistant','content':'ответ первого кандидата'}])
        self.assertEqual(canonical['user_message'],invocation['user_turn'])
        self.assertEqual(canonical['persona']['response_language'],'ru')
        self.assertEqual(canonical['communication_settings'],{'tone':'warm','response_length':'short','use_name':False})
        self.assertEqual(invocation['context'],{'as_of':'2026-10-04','current_session':'s1','current_session_at':'2026-10-04T18:00:00+03:00'})
        self.assertIn('Synthetic evaluation context',canonical['dialogue_summaries'][0])
        source=(LAB.parents[0]/'worker/gpoint_mac_worker.py').read_text()
        self.assertIn("Return only an object with one key, ",source)
        self.assertIn('text, containing the in-character answer.',source)
        for adapter in (self.a.OpenAIResponsesAdapter(),self.a.AnthropicMessagesAdapter(),self.a.GeminiGenerateContentAdapter()):
            provider_invocation=self.p.build_invocation(LAB,self.frozen,'long_memory-01',adapter.model_id,1,history)
            payload=adapter.build(provider_invocation)
            serialized=json.dumps(payload,ensure_ascii=False)
            self.assertIn('ответ первого кандидата',serialized)
            self.assertIn(invocation['user_turn'],serialized)
            self.assertNotIn('evaluation_notes',serialized)
            if isinstance(adapter,self.a.OpenAIResponsesAdapter):schema=payload['text']['format']['schema']
            elif isinstance(adapter,self.a.AnthropicMessagesAdapter):schema=payload['output_config']['format']['schema']
            else:schema=payload['generationConfig']['responseSchema']
            self.assertEqual(schema,self.a.AGATA_RESPONSE_SCHEMA)

    def test_adapter_models_are_exact_and_have_no_sender(self):
        adapters=[self.a.OpenAIResponsesAdapter(),self.a.AnthropicMessagesAdapter(),self.a.GeminiGenerateContentAdapter()]
        self.assertEqual([x.model_id for x in adapters],['gpt-6.1-sol','claude-sonnet-5-5','gemini-3.8-flash'])
        self.assertTrue(all(not hasattr(x,'send') and not hasattr(x,'complete') for x in adapters))
        baseline=self.p.build_invocation(LAB,self.frozen,'daily-01','codex-cli-baseline',0,[])
        descriptor=self.a.CodexBaselineDescriptor().build(baseline)
        self.assertEqual(descriptor['reasoning_effort'],'medium')
        self.assertEqual(descriptor['max_output_characters'],4096)
        self.assertEqual(descriptor['timeout_seconds'],180)
        self.assertEqual(descriptor['worker_request']['user_message'],baseline['user_turn'])
        self.assertEqual(descriptor['worker_request']['history'],[])
        self.assertEqual(descriptor['response_schema'],self.a.AGATA_RESPONSE_SCHEMA)
        with self.assertRaises(PermissionError):self.a.OpenAIResponsesAdapter().build({**baseline,'fixture_only':False})

    def test_all_provider_parsers_preserve_errors_and_usage(self):
        self.assertEqual(self.a.OpenAIResponsesAdapter().parse({'status':'completed','output_text':'{"text":"hello"}','usage':{'input_tokens':4,'output_tokens':2}})['text'],'hello')
        refusal=self.a.AnthropicMessagesAdapter().parse({'stop_reason':'refusal','content':[]})
        self.assertEqual(refusal['status'],'refusal')
        error=self.a.GeminiGenerateContentAdapter().parse({'error':{'code':429}})
        self.assertEqual(error['status'],'error')
        partial=self.a.OpenAIResponsesAdapter().parse({'status':'incomplete','output_text':'','usage':{'output_tokens':2048}})
        self.assertEqual(partial['status'],'partial');self.assertEqual(partial['text'],'')
        self.assertEqual(self.a.OpenAIResponsesAdapter().parse({'status':'completed','output_text':'not json'})['error'],'response_contract_invalid')

    def test_offline_dry_run_covers_288_without_network_or_subprocess(self):
        with patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')), patch('subprocess.Popen',side_effect=AssertionError('subprocess forbidden')):
            result=self.p.run_fixture_dry_run(LAB,self.frozen)
        self.assertEqual(result['planned_replies'],288)
        self.assertEqual(result['fixture_responses'],288)
        self.assertEqual(result['live_inference_requests'],0)
        self.assertEqual(result['billing_status'],'NOT_INCURRED_FIXTURE_ONLY')

    def test_billing_arithmetic_and_maximum_are_conservative(self):
        budget=self.b.load_budget(LAB)
        estimate=self.b.calculate_candidate_budget(budget)
        self.assertAlmostEqual(estimate['expected_candidate_usd'],3.808512,places=6)
        self.assertAlmostEqual(estimate['maximum_candidate_usd'],9.129063,places=6)
        self.assertLessEqual(estimate['maximum_candidate_usd'],10)
        self.assertEqual(estimate['candidate_external_calls_max'],219)
        self.assertEqual(estimate['full_pilot_budget_status'],'UNKNOWN_BASELINE')

    def test_guard_fails_closed_without_grant_or_unknown_baseline(self):
        with self.assertRaises(PermissionError):self.b.BudgetGuard(self.b.load_budget(LAB),None)
        grant={'owner_approval_id':'fixture-test-only','baseline_billing_verified':False}
        with self.assertRaises(PermissionError):self.b.BudgetGuard(self.b.load_budget(LAB),grant)

    def test_unknown_baseline_is_not_reported_as_free_or_fully_budgeted(self):
        budget=self.b.load_budget(LAB);estimate=self.b.calculate_candidate_budget(budget)
        self.assertIsNone(budget['full_pilot_max_usd'])
        self.assertEqual(estimate['full_pilot_budget_status'],'UNKNOWN_BASELINE')

    def test_guard_limits_usd_tokens_calls_and_retries(self):
        budget=self.test_budget_fixture();grant=self.b.test_grant(budget)
        guard=self.b.BudgetGuard(budget,grant)
        guard.authorize_request('gpt-6.1-sol',input_tokens=100,output_tokens=100)
        with self.assertRaises(PermissionError):guard.authorize_request('gpt-6.1-sol',input_tokens=32769,output_tokens=100)
        with self.assertRaises(PermissionError):guard.authorize_request('unlisted-model',input_tokens=1,output_tokens=1)
        self.assertEqual(len(guard.snapshot()['requests']),1)
        expected=(100*budget['price_basis']['gpt-6.1-sol']['cache_write_input_per_million']/1_000_000+100*budget['price_basis']['gpt-6.1-sol']['output_per_million']/1_000_000)*budget['contingency_factor']
        self.assertAlmostEqual(guard.snapshot()['estimated_candidate_usd'],expected)
        bounded=self.b.BudgetGuard(budget,self.b.test_grant(budget))
        for _ in range(20):bounded.authorize_request('gpt-6.1-sol',input_tokens=32768,output_tokens=2048)
        with self.assertRaises(PermissionError):bounded.authorize_request('gpt-6.1-sol',input_tokens=32768,output_tokens=2048)
        bounded.authorize_request('gpt-6.1-sol',input_tokens=32768,output_tokens=2048,retry=True)
        with self.assertRaises(PermissionError):bounded.authorize_request('gpt-6.1-sol',input_tokens=32768,output_tokens=2048,retry=True)
        self.assertEqual(bounded.snapshot()['candidate_tokens_by_model']['gpt-6.1-sol']['input'],21*32768)

    def test_guard_full_frozen_candidate_envelope_stays_inside_reserve(self):
        budget=self.test_budget_fixture();guard=self.b.BudgetGuard(budget,self.b.test_grant(budget))
        for model in self.b.load_budget(LAB)['price_basis']:
            for _ in range(72):guard.authorize_request(model,input_tokens=9344,output_tokens=2048)
            guard.authorize_request(model,input_tokens=32768,output_tokens=2048,retry=True)
        self.assertEqual(guard.snapshot()['candidate_calls'],219)
        self.assertLessEqual(guard.snapshot()['estimated_candidate_usd'],self.b.calculate_candidate_budget(budget)['maximum_candidate_usd'])

    def test_retry_limit_and_refusal_policy(self):
        b=self.test_budget_fixture();guard=self.b.BudgetGuard(b,self.b.test_grant(b))
        self.assertTrue(guard.retry_allowed('gpt-6.1-sol','transport_error'))
        self.assertFalse(guard.retry_allowed('gpt-6.1-sol','refusal'))
        self.assertFalse(guard.retry_allowed('gpt-6.1-sol','max_tokens'))
        guard.authorize_request('gpt-6.1-sol',input_tokens=10,output_tokens=10,retry=True)
        with self.assertRaises(PermissionError):guard.authorize_request('gpt-6.1-sol',input_tokens=10,output_tokens=10,retry=True)

    def test_rating_lock_requires_complete_packet_and_prevents_mutation(self):
        packet,key=self.p.fixture_blind_packet(LAB,self.frozen)
        ratings=self.complete_ratings(packet)
        lock=self.r.lock_ratings(packet,ratings,cycle_id='cycle-1')
        self.assertTrue(self.r.verify_lock(lock,packet,ratings))
        ratings['ratings'][0]['scores']['naturalness']=1
        self.assertFalse(self.r.verify_lock(lock,packet,ratings))
        with self.assertRaises(ValueError):self.r.unblind(packet,lock,ratings,key)

    def test_locked_rating_artifact_is_write_once_and_new_cycle_is_separate(self):
        packet,_=self.p.fixture_blind_packet(LAB,self.frozen);ratings=self.complete_ratings(packet)
        lock=self.r.lock_ratings(packet,ratings,cycle_id='cycle-file')
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'ratings.json'
            self.r.persist_locked_ratings(target,packet,ratings,cycle_id='cycle-file')
            self.assertEqual(target.stat().st_mode & 0o777,0o444)
            with self.assertRaises(FileExistsError):self.r.persist_locked_ratings(target,packet,ratings,cycle_id='cycle-file')
        cycle=self.r.new_review_cycle(ratings,lock,cycle_id='cycle-file-2',owner_reason='Correct an owner scoring error')
        self.assertEqual(cycle['prior_lock_sha256'],lock['lock_sha256'])
        tampered_lock={**lock,'lock_sha256':'forged'}
        with self.assertRaises(ValueError):self.r.new_review_cycle(ratings,tampered_lock,cycle_id='cycle-file-3',owner_reason='Reject forged lock')
        misbound={**lock,'packet_sha256':'different'}
        misbound['lock_sha256']=self.r._hash({k:misbound[k] for k in ('schema_version','cycle_id','packet_sha256','ratings_sha256')})
        with self.assertRaises(ValueError):self.r.new_review_cycle(ratings,misbound,cycle_id='cycle-file-4',owner_reason='Reject packet mismatch')

    def test_unblind_after_lock_and_new_cycle_preserve_prior_lock(self):
        packet,key=self.p.fixture_blind_packet(LAB,self.frozen)
        ratings=self.complete_ratings(packet);lock=self.r.lock_ratings(packet,ratings,cycle_id='cycle-1')
        revealed=self.r.unblind(packet,lock,ratings,key)
        self.assertEqual(len(revealed['revealed']),64)
        self.assertEqual(len(revealed['pairwise_preferences']),96)
        report=self.r.aggregate_ratings(packet,lock,ratings,key)
        self.assertIsNone(report['winner'])
        with self.assertRaises(ValueError):self.r.aggregate_ratings(packet,lock,ratings,{**key,'mappings':key['mappings'][:-1]})
        altered=copy.deepcopy(key);altered['mappings'][0]['model_id']='forged'
        with self.assertRaises(ValueError):self.r.unblind(packet,lock,ratings,altered)
        next_cycle=self.r.new_review_cycle(ratings,lock,cycle_id='cycle-2',owner_reason='Owner requested correction')
        self.assertEqual(next_cycle['prior_lock_sha256'],lock['lock_sha256'])
        self.assertEqual(lock,self.r.lock_ratings(packet,ratings,cycle_id='cycle-1'))

    def test_failed_partial_outputs_remain_non_success(self):
        rows=self.p.fixture_result_rows(LAB,self.frozen)
        rows[0]['status']='refusal';rows[0]['turns']=rows[0]['turns'][:2];rows[0]['fixture_only']=False
        packet,_=self.p.package_results(LAB,self.frozen,rows,seed=42)
        self.assertEqual(packet['completion_status'],'INCOMPLETE')
        statuses=[candidate['status'] for scenario in packet['scenarios'] for candidate in scenario['candidates']]
        self.assertIn('refusal',statuses)

    def test_imported_results_require_exact_scenario_model_coverage_and_prompt_parity(self):
        rows=self.p.fixture_result_rows(LAB,self.frozen)
        self.assertEqual(self.p.validate_result_rows(LAB,self.frozen,rows)['expected_pairs'],64)
        with self.assertRaises(ValueError):self.p.validate_result_rows(LAB,self.frozen,rows[:-1])
        with self.assertRaises(ValueError):self.p.validate_result_rows(LAB,self.frozen,rows+[rows[0]])
        bad=copy.deepcopy(rows);bad[0]['turns'][0]['text']='different user prompt'
        with self.assertRaises(ValueError):self.p.validate_result_rows(LAB,self.frozen,bad)

if __name__=='__main__':unittest.main()
