import { pathToFileURL } from 'node:url';

const REQUIRED_FACTS = [
  'city_durable_memory_section_present',
  'old_meeting_durable_memory_section_present',
  'current_meeting_durable_memory_section_present',
  'city_absent_from_recent_history',
  'old_meeting_absent_from_recent_history',
  'current_meeting_absent_from_recent_history',
];

export function readProviderContextEvidence(log) {
  const records = String(log)
    .split(/\r?\n/)
    .filter((line) => line.includes('LOCAL_TEST_PROVIDER_CONTEXT'));

  if (records.length === 0) throw new Error('provider_context_record_missing');
  const parsed = records.map((line) => {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      throw new Error('provider_context_record_malformed');
    }
    if (record?.event !== 'LOCAL_TEST_PROVIDER_CONTEXT') {
      throw new Error('provider_context_record_malformed');
    }
    if (!Number.isInteger(record.message_count) || record.message_count < 1) {
      throw new Error('provider_context_message_count_invalid');
    }
    if (!Number.isInteger(record.recent_history_count) || record.recent_history_count < 0) {
      throw new Error('provider_context_recent_history_count_invalid');
    }
    if (!Number.isInteger(record.user_message_count) || record.user_message_count < 1) {
      throw new Error('provider_context_user_message_count_invalid');
    }
    if (typeof record.last_user_is_recall_probe !== 'boolean') {
      throw new Error('provider_context_probe_flag_invalid');
    }
    for (const fact of REQUIRED_FACTS) {
      if (typeof record[fact] !== 'boolean') {
        throw new Error(`provider_context_fact_invalid:${fact}`);
      }
    }
    return record;
  });
  // The direct dialogue completion carries the recent conversation window.
  // A one-message system task can repeat the recall probe but is not the
  // user-facing response context; it has fewer than ten history turns.
  const relevant = parsed.filter((record) => (
    record.last_user_is_recall_probe && record.recent_history_count >= 10
  ));
  if (relevant.length === 0) throw new Error('provider_context_probe_record_missing');
  const latest = relevant.reduce((current, record) => (
    record.message_count > current.message_count ? record : current
  ));
  const missingFacts = REQUIRED_FACTS.filter((fact) => relevant.some((record) => record[fact] !== true));

  return {
    status: missingFacts.length === 0 ? 'PASS' : 'BLOCKED',
    record_count: relevant.length,
    ignored_record_count: parsed.length - relevant.length,
    message_count: latest.message_count,
    recent_history_count: latest.recent_history_count,
    missing_facts: missingFacts,
  };
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  try {
    const evidence = readProviderContextEvidence(input);
    const facts = evidence.missing_facts.length === 0
      ? 'durable_memory_context=present recent_history=excluded'
      : `missing_evidence=${evidence.missing_facts.join(',')}`;
    console.log(`LOCAL_E2E_PROVIDER_CONTEXT=${evidence.status} records=${evidence.record_count} latest_message_count=${evidence.message_count} recent_history_count=${evidence.recent_history_count} ${facts}`);
  } catch (error) {
    console.error(`LOCAL_E2E_PROVIDER_CONTEXT=FAIL reason=${error.message}`);
    process.exitCode = 1;
  }
}
