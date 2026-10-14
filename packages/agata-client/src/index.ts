export interface AgataCharacterProfile {
  name: string;
  personality?: Record<string, unknown>;
  speech_style?: Record<string, unknown>;
  backstory?: Record<string, unknown>;
  boundaries?: Record<string, unknown>;
}

export interface AgataConsentSnapshot {
  chat_history: boolean;
  long_term_memory?: boolean;
  age_band: 'unknown' | 'minor' | 'adult';
  age_status: 'pending' | 'verified' | 'self_attested' | 'expired' | 'rejected';
  age_method: string;
  jurisdiction: string;
  verified_at?: string | null;
  expires_at?: string | null;
  disclosure_accepted: boolean;
  disclosure_receipt_id: string;
}

export interface AgataRespondInput {
  request_id: string;
  gpoint_user_id: string;
  gpoint_character_id: string;
  conversation_id?: string;
  message_id: string;
  text: string;
  locale?: string;
}

export interface AgataRespondOutput {
  status: 'completed' | 'pending';
  request_id: string;
  conversation_id: string;
  message_id: string | null;
  character_id: string;
  text: string | null;
}

export interface AgataChatState {
  chatHistory: boolean;
  longTermMemory: boolean;
  disclosureRequired: boolean;
  onboardingCompleted: boolean;
  disclosureVersion: number | null;
  onboardingCompletedAt: string | null;
  memoryOptIn: boolean;
  memoryOptInAt: string | null;
  ageAssuranceRequired: boolean;
  ageBand: 'unknown' | 'minor' | 'adult';
  ageStatus: 'pending' | 'verified' | 'self_attested' | 'expired' | 'rejected';
  ageMethod: string;
  verifiedAt: string | null;
  expiresAt: string | null;
  jurisdiction: string;
}

export interface AgataAgeAssuranceState {
  required: boolean;
  eligible: boolean;
  status: 'pending' | 'verified' | 'self_attested' | 'expired' | 'rejected';
  method: string;
  jurisdiction: string;
  assertedAt: string | null;
  expiresAt: string | null;
  selfAttestationAllowed: boolean;
}

export interface AgataInferenceJob {
  id: string;
  leaseToken: string;
  attempt: number;
  request: Record<string, unknown>;
}

export interface AgataConversationItem {
  gpointCharacterId: string;
  externalConversationId: string;
  lastMessagePreview: string;
  lastMessageRole: 'user' | 'assistant';
  lastMessageAt: string;
  unreadCount: number;
}

export interface AgataConversationPage {
  items: AgataConversationItem[];
  nextCursor: string | null;
}

export interface AgataHistoryMessage {
  id: string;
  clientMessageId?: string;
  role: 'user' | 'assistant';
  text: string;
  occurredAt: string;
  incomingReadAt: string | null;
}

export interface AgataHistoryPage {
  items: AgataHistoryMessage[];
  nextCursor: string | null;
}

export class AgataServiceError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(
    message: string,
    status: number,
    code: string,
  ) {
    super(message);
    this.name = 'AgataServiceError';
    this.status = status;
    this.code = code;
  }
}

export interface AgataClientOptions {
  baseUrl: string;
  token: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export interface AgataHealth {
  status: 'ok' | 'ready';
}

export type AgataWorkerQueueLane = 'GPOINT' | 'GSALES';

export interface AgataWorkerQueueStatus {
  source_id: string;
  lane: AgataWorkerQueueLane;
  pending_jobs: number;
  running_jobs: number;
  snapshot_at: string;
}

function parseWorkerQueueStatus(value: unknown, lane: AgataWorkerQueueLane): AgataWorkerQueueStatus {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AgataServiceError('AGATA queue status invalid', 502, 'worker_queue_status_invalid');
  }
  const record = value as Record<string, unknown>;
  const expectedSource = `agata-worker-queue-status-v1:${lane.toLowerCase()}`;
  const timestamp = typeof record.snapshot_at === 'string' ? Date.parse(record.snapshot_at) : Number.NaN;
  const keys = Object.keys(record).sort();
  if (
    keys.join(',') !== 'lane,pending_jobs,running_jobs,snapshot_at,source_id' ||
    record.source_id !== expectedSource || record.lane !== lane ||
    !Number.isSafeInteger(record.pending_jobs) || (record.pending_jobs as number) < 0 ||
    !Number.isSafeInteger(record.running_jobs) || (record.running_jobs as number) < 0 ||
    !Number.isFinite(timestamp) || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(record.snapshot_at as string)
  ) {
    throw new AgataServiceError('AGATA queue status invalid', 502, 'worker_queue_status_invalid');
  }
  return {
    source_id: expectedSource,
    lane,
    pending_jobs: record.pending_jobs as number,
    running_jobs: record.running_jobs as number,
    snapshot_at: record.snapshot_at as string,
  };
}

/** Internal service-to-service client. No user credential is forwarded. */
export class AgataClient {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: AgataClientOptions) {
    const parsed = new URL(options.baseUrl);
    if (!['http:', 'https:'].includes(parsed.protocol) || !options.token.trim()) {
      throw new TypeError('AGATA internal URL and service token are required');
    }
    this.baseUrl = parsed.toString().replace(/\/$/, '');
    this.token = options.token;
    this.timeoutMs = options.timeoutMs ?? 15_000;
    if (!Number.isInteger(this.timeoutMs) || this.timeoutMs < 100 || this.timeoutMs > 60_000) {
      throw new RangeError('AGATA request timeout must be between 100 and 60000 ms');
    }
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async syncCharacter(id: string, profile: AgataCharacterProfile): Promise<{ character_id: string; profile_version: number }> {
    return this.request(`/internal/v1/characters/${encodeURIComponent(id)}`, 'PUT', profile);
  }

  async syncConsent(id: string, consent: AgataConsentSnapshot): Promise<{ user_id: string; age_version: number; disclosure: string }> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/consent`, 'PUT', consent);
  }

  async getChatState(id: string): Promise<AgataChatState> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/chat-state`, 'GET', undefined);
  }

  async getAgeAssurance(id: string): Promise<AgataAgeAssuranceState> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/age-assurance`, 'GET', undefined);
  }

  async selfAttestAge(id: string): Promise<{ eligible: boolean; status: string; method: string }> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/age-assurance/self-attest`, 'POST', { confirmed_18_plus: true });
  }

  async claimInferenceJob(): Promise<{ job: AgataInferenceJob | null }> {
    return this.request('/internal/v1/worker/claim', 'POST', {});
  }

  async completeInferenceJob(input: { job_id: string; lease_token: string; text: string; model: string }): Promise<{ completed: boolean; duplicate: boolean }> {
    return this.request('/internal/v1/worker/complete', 'POST', input);
  }

  async failInferenceJob(input: { job_id: string; lease_token: string; error_code: string }): Promise<{ accepted: boolean; state: string }> {
    return this.request('/internal/v1/worker/fail', 'POST', input);
  }

  async getWorkerQueueStatus(lane: AgataWorkerQueueLane): Promise<AgataWorkerQueueStatus> {
    const result = await this.request<unknown>('/internal/v1/worker/queue-status', 'GET', undefined);
    return parseWorkerQueueStatus(result, lane);
  }

  async unreadMessages(id: string): Promise<{ items: Array<{ gpointCharacterId: string; unreadCount: number }> }> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/unread`, 'GET', undefined);
  }

  async markMessagesRead(id: string, characterId: string, messageIds: string[]): Promise<{ acknowledgedMessageIds: string[] }> {
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/characters/${encodeURIComponent(characterId)}/messages/read`, 'POST', { messageIds });
  }

  async listConversations(id: string, options: { limit?: number; cursor?: string } = {}): Promise<AgataConversationPage> {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.cursor) query.set('cursor', options.cursor);
    const suffix = query.size ? `?${query.toString()}` : '';
    return this.request(`/internal/v1/users/${encodeURIComponent(id)}/conversations${suffix}`, 'GET', undefined);
  }

  async listMessages(id: string, characterId: string, options: { limit?: number; cursor?: string } = {}): Promise<AgataHistoryPage> {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.cursor) query.set('cursor', options.cursor);
    const suffix = query.size ? `?${query.toString()}` : '';
    return this.request(
      `/internal/v1/users/${encodeURIComponent(id)}/characters/${encodeURIComponent(characterId)}/messages${suffix}`,
      'GET', undefined,
    );
  }

  /** Retries are safe because message_id is AGATA's durable idempotency key. */
  async respond(input: AgataRespondInput): Promise<AgataRespondOutput> {
    return this.request('/internal/v1/respond', 'POST', input, 2);
  }

  async live(): Promise<AgataHealth> {
    return this.request('/health/live', 'GET', undefined);
  }

  async ready(): Promise<AgataHealth> {
    return this.request('/health/ready', 'GET', undefined);
  }

  private async request<T>(path: string, method: 'GET' | 'PUT' | 'POST', body: unknown, attempts = 1): Promise<T> {
    let lastError: unknown;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      let response: Response;
      try {
        response = await this.fetchImpl(`${this.baseUrl}${path}`, {
          method,
          headers: {
            authorization: `Bearer ${this.token}`,
            'content-type': 'application/json',
            accept: 'application/json',
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
          signal: AbortSignal.timeout(this.timeoutMs),
        });
      } catch (error) {
        lastError = error;
        if (attempt < attempts) {
          await new Promise((resolve) => setTimeout(resolve, 100 * attempt));
          continue;
        }
        throw new AgataServiceError('AGATA service unavailable', 0, 'transport_error');
      }
      if (response.ok) return (await response.json()) as T;
      const payload = await response.json().catch(() => ({})) as { detail?: { code?: string }; code?: string };
      const code = payload.detail?.code ?? payload.code ?? 'service_error';
      if (attempt < attempts && (response.status === 429 || response.status >= 500)) {
        await new Promise((resolve) => setTimeout(resolve, 100 * attempt));
        continue;
      }
      throw new AgataServiceError('AGATA request failed', response.status, code);
    }
    throw lastError;
  }
}
