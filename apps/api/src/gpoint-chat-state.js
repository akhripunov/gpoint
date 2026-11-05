const VALID_AGE_BANDS = new Set(['unknown', 'minor', 'adult']);
const VALID_AGE_STATUSES = new Set(['pending', 'verified', 'self_attested', 'expired', 'rejected']);
const ALLOWED_POLICY_RULES = new Set([
  'minimum_age', 'adult_only_features', 'ai_disclosure_required', 'self_attestation_allowed',
  'disclosure_repeat_hours', 'break_interval_minutes',
]);

function validDate(value) {
  return value instanceof Date && Number.isFinite(value.getTime());
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function validPolicyRules(rules) {
  if (!rules || typeof rules !== 'object' || Array.isArray(rules)) return false;
  if (Object.keys(rules).some((key) => !ALLOWED_POLICY_RULES.has(key))) return false;
  if (rules.minimum_age !== 18) return false;
  if (rules.adult_only_features !== undefined &&
      (!Array.isArray(rules.adult_only_features) || rules.adult_only_features.some((value) => typeof value !== 'string'))) return false;
  for (const key of ['ai_disclosure_required', 'self_attestation_allowed']) {
    if (rules[key] !== undefined && typeof rules[key] !== 'boolean') return false;
  }
  for (const key of ['disclosure_repeat_hours', 'break_interval_minutes']) {
    if (rules[key] !== undefined && (!Number.isSafeInteger(rules[key]) || rules[key] <= 0)) return false;
  }
  return true;
}

function activePolicy(profiles, now) {
  if (!Array.isArray(profiles) || !validDate(now)) return null;
  const tenants = new Set(profiles.map((row) => row?.sourceTenantId).filter((value) => typeof value === 'string' && value));
  if (tenants.size !== 1 || profiles.some((row) => !row || row.sourceTenantId !== [...tenants][0])) return null;
  const matches = profiles.filter((row) => row && row.status === 'active' &&
    validDate(row.effectiveFrom) && row.effectiveFrom <= now &&
    (row.effectiveTo === null || row.effectiveTo === undefined || (validDate(row.effectiveTo) && row.effectiveTo > now)));
  if (!matches.length) return null;
  matches.sort((a, b) => b.version - a.version || b.effectiveFrom - a.effectiveFrom || String(a.id).localeCompare(String(b.id)));
  const selected = matches[0];
  if (!selected || typeof selected.id !== 'string' || !Number.isSafeInteger(selected.version) ||
      selected.version < 1 || typeof selected.sourceTenantId !== 'string' ||
      typeof selected.termsVersion !== 'string' || !selected.termsVersion ||
      typeof selected.privacyVersion !== 'string' || !selected.privacyVersion || !validPolicyRules(selected.rules)) return null;
  const sameVersion = matches.filter((row) => row.version === selected.version);
  if (sameVersion.some((row) => !validPolicyRules(row.rules) || stableJson(row.rules) !== stableJson(selected.rules))) return null;
  return selected;
}

function latestAge(rows) {
  if (!Array.isArray(rows) || rows.some((row) => !row || !Number.isSafeInteger(row.version) || row.version < 1)) return null;
  if (!rows.length) return null;
  return [...rows].sort((a, b) => b.version - a.version || String(b.id).localeCompare(String(a.id)))[0];
}

function activePurposeConsent(rows, purpose, expectedVersion) {
  if (!Array.isArray(rows)) return { active: false, lastChangedAt: null };
  const matching = rows.filter((row) => row?.purpose === purpose);
  const active = matching.filter((row) => row.revokedAt === null || row.revokedAt === undefined);
  const lastChanged = matching.map((row) => row.revokedAt ?? row.grantedAt)
    .filter(validDate).sort((a, b) => b - a)[0] ?? null;
  return { active: active.length === 1 && (!expectedVersion || active[0]?.policyVersion === expectedVersion), lastChangedAt: lastChanged };
}

function ageEligible(age, policy, now) {
  if (!age || !policy || !validDate(now) || !VALID_AGE_BANDS.has(age.ageBand) ||
      !VALID_AGE_STATUSES.has(age.status)) return false;
  if (policy.rules.minimum_age !== 18 || age.ageBand !== 'adult' ||
      (age.expiresAt && (!validDate(age.expiresAt) || age.expiresAt <= now))) return false;
  if (age.status === 'verified') return validDate(age.verifiedAt) && age.verifiedAt <= now;
  return age.status === 'self_attested' && age.method === 'self_attested' &&
    validDate(age.assertedAt) && age.assertedAt <= now &&
    policy.rules.self_attestation_allowed === true;
}

/** Build the same public consent contract from GPoint-owned persisted state. */
export function buildGpointMessagesState({ policies, consents, ages, receipts, now = new Date(), erosEnabled = process.env.EROS_ENABLED === 'true' }) {
  const policy = activePolicy(policies, now);
  if (!policy) {
    return {
      enabled: false, chatHistory: false, longTermMemory: false, termsAccepted: false, privacyAccepted: false,
      termsVersion: null, privacyVersion: null, memoryConsentRequired: true, disclosureRequired: true,
      onboardingCompleted: false, disclosureVersion: null, onboardingCompletedAt: null,
      memoryOptIn: false, memoryOptInAt: null, ageAssuranceRequired: true,
      ageBand: 'unknown', ageStatus: 'pending', ageMethod: 'unknown', verifiedAt: null,
      assertedAt: null, expiresAt: null, aiAvailable: false,
    };
  }
  const chatConsent = activePurposeConsent(consents, 'chat_history');
  const memoryConsent = activePurposeConsent(consents, 'long_term_memory');
  const termsConsent = activePurposeConsent(consents, 'terms_of_use', policy.termsVersion);
  const privacyConsent = activePurposeConsent(consents, 'privacy_policy', policy.privacyVersion);
  const age = latestAge(ages);
  const disclosureRequiredByPolicy = policy.rules.ai_disclosure_required === true;
  const receipt = Array.isArray(receipts) ? receipts
    .filter((row) => row?.policyProfileId === policy.id && row.policyVersion === policy.version && validDate(row.displayedAt))
    .sort((a, b) => b.displayedAt - a.displayedAt || String(b.id).localeCompare(String(a.id)))[0] : null;
  const onboardingCompleted = termsConsent.active && privacyConsent.active &&
    (!disclosureRequiredByPolicy || Boolean(receipt));
  const memoryOptInAt = memoryConsent.lastChangedAt ?? (receipt?.memoryOptIn === true ? receipt.memoryOptInAt ?? null : null);
  return {
    enabled: true,
    chatHistory: chatConsent.active,
    longTermMemory: memoryConsent.active,
    termsAccepted: termsConsent.active,
    privacyAccepted: privacyConsent.active,
    termsVersion: policy.termsVersion,
    privacyVersion: policy.privacyVersion,
    memoryConsentRequired: true,
    disclosureRequired: disclosureRequiredByPolicy && !onboardingCompleted,
    onboardingCompleted,
    disclosureVersion: disclosureRequiredByPolicy ? policy.version : null,
    onboardingCompletedAt: onboardingCompleted && receipt ? receipt.displayedAt.toISOString() : null,
    memoryOptIn: memoryConsent.active,
    memoryOptInAt: validDate(memoryOptInAt) ? memoryOptInAt.toISOString() : null,
    ageAssuranceRequired: !ageEligible(age, policy, now),
    ageBand: age?.ageBand ?? 'unknown',
    ageStatus: age?.status ?? 'pending',
    ageMethod: age?.method ?? 'unknown',
    verifiedAt: validDate(age?.verifiedAt) ? age.verifiedAt.toISOString() : null,
    assertedAt: validDate(age?.assertedAt) ? age.assertedAt.toISOString() : null,
    expiresAt: validDate(age?.expiresAt) ? age.expiresAt.toISOString() : null,
    aiAvailable: erosEnabled === true,
  };
}

/** Stock Eros extracts memory unconditionally, so both explicit permissions are required. */
export function isGpointChatPermitted(state) {
  return Boolean(state && state.enabled === true && state.aiAvailable === true &&
    state.chatHistory === true && state.longTermMemory === true &&
    state.termsAccepted === true && state.privacyAccepted === true &&
    state.disclosureRequired === false && state.ageAssuranceRequired === false);
}

export function gpointAgeAssuranceState(state, policySelfAttestationAllowed = false) {
  const eligible = Boolean(state && state.ageAssuranceRequired === false);
  return {
    required: !eligible,
    eligible,
    status: state?.ageStatus ?? 'pending',
    method: state?.ageMethod ?? 'unknown',
    assertedAt: state?.ageMethod === 'self_attested' ? state?.assertedAt ?? null : null,
    expiresAt: state?.expiresAt ?? null,
    selfAttestationAllowed: policySelfAttestationAllowed === true,
  };
}
