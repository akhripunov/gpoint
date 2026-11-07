export interface GpointChatPolicy {
  id: string;
  sourceTenantId: string;
  version: number;
  termsVersion: string;
  privacyVersion: string;
  status: string;
  effectiveFrom: Date;
  effectiveTo?: Date | null;
  rules: unknown;
}

export interface GpointConsent {
  id?: string;
  purpose: string;
  grantedAt?: Date;
  revokedAt?: Date | null;
  policyVersion?: string;
}

export interface GpointAgeAssurance {
  id?: string;
  ageBand: string;
  status: string;
  method: string;
  verifiedAt?: Date | null;
  assertedAt?: Date | null;
  expiresAt?: Date | null;
  version: number;
}

export interface GpointDisclosureReceipt {
  id?: string;
  policyProfileId: string;
  policyVersion: number;
  displayedAt: Date;
  memoryOptIn?: boolean;
  memoryOptInAt?: Date | null;
}

export interface GpointMessagesState {
  enabled: boolean;
  chatHistory: boolean;
  longTermMemory: boolean;
  termsAccepted: boolean;
  privacyAccepted: boolean;
  termsVersion: string | null;
  privacyVersion: string | null;
  memoryConsentRequired: boolean;
  disclosureRequired: boolean;
  onboardingCompleted: boolean;
  disclosureVersion: number | null;
  onboardingCompletedAt: string | null;
  memoryOptIn: boolean;
  memoryOptInAt: string | null;
  ageAssuranceRequired: boolean;
  ageBand: string;
  ageStatus: string;
  ageMethod: string;
  verifiedAt: string | null;
  assertedAt: string | null;
  expiresAt: string | null;
  aiAvailable: boolean;
}

export function buildGpointMessagesState(input: {
  policies: GpointChatPolicy[];
  consents: GpointConsent[];
  ages: GpointAgeAssurance[];
  receipts: GpointDisclosureReceipt[];
  now?: Date;
  erosEnabled?: boolean;
}): GpointMessagesState;

export function isGpointChatPermitted(state: GpointMessagesState | null | undefined): boolean;
export function gpointAgeAssuranceState(state: GpointMessagesState | null | undefined, policySelfAttestationAllowed?: boolean): {
  required: boolean;
  eligible: boolean;
  status: string;
  method: string;
  assertedAt: string | null;
  expiresAt: string | null;
  selfAttestationAllowed: boolean;
};
