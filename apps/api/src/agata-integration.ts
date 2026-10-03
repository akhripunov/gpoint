import {
  type AgataChatState,
  type AgataAgeAssuranceState,
  type AgataConversationPage,
  type AgataCharacterProfile,
  type AgataConsentSnapshot,
  type AgataHistoryPage,
  type AgataRespondOutput,
} from '@gpoint/agata-client';
import { createAgataClient } from './agata-client.js';
import type { AgataClient } from '@gpoint/agata-client';

export interface AgataTurnInput {
  userId: string;
  characterId: string;
  requestId: string;
  messageId: string;
  conversationId?: string;
  text: string;
  locale?: string;
  profile: AgataCharacterProfile;
  consent: AgataConsentSnapshot;
}

export interface AgataConsentInput {
  userId: string;
  consent: AgataConsentSnapshot;
}

/** Disabled-by-default boundary for a future GPoint character-chat flow. */
export class AgataIntegrationService {
  constructor(private readonly client: Pick<AgataClient, 'syncCharacter' | 'syncConsent' | 'respond' | 'getChatState' | 'getAgeAssurance' | 'selfAttestAge' | 'listConversations' | 'listMessages' | 'unreadMessages' | 'markMessagesRead'>) {}

  chatState(userId: string): Promise<AgataChatState> {
    return this.client.getChatState(userId);
  }

  ageAssurance(userId: string): Promise<AgataAgeAssuranceState> {
    return this.client.getAgeAssurance(userId);
  }

  selfAttestAge(userId: string): ReturnType<AgataClient['selfAttestAge']> {
    return this.client.selfAttestAge(userId);
  }

  unreadMessages(userId: string): ReturnType<AgataClient['unreadMessages']> {
    return this.client.unreadMessages(userId);
  }

  markMessagesRead(userId: string, characterId: string, messageIds: string[]): ReturnType<AgataClient['markMessagesRead']> {
    return this.client.markMessagesRead(userId, characterId, messageIds);
  }

  listConversations(userId: string, options?: { limit?: number; cursor?: string }): Promise<AgataConversationPage> {
    return this.client.listConversations(userId, options);
  }

  listMessages(userId: string, characterId: string, options?: { limit?: number; cursor?: string }): Promise<AgataHistoryPage> {
    return this.client.listMessages(userId, characterId, options);
  }

  acceptConsent(input: AgataConsentInput): ReturnType<AgataClient['syncConsent']> {
    return this.client.syncConsent(input.userId, input.consent);
  }

  async respond(input: AgataTurnInput): Promise<AgataRespondOutput> {
    await this.client.syncCharacter(input.characterId, input.profile);
    await this.client.syncConsent(input.userId, input.consent);
    return this.send(input);
  }

  async respondWithCurrentConsent(input: AgataTurnInput): Promise<AgataRespondOutput> {
    await this.client.syncCharacter(input.characterId, input.profile);
    return this.send(input);
  }

  private async send(input: AgataTurnInput): Promise<AgataRespondOutput> {
    // messageId is the durable idempotency key. Any client retry reuses it.
    return this.client.respond({
      request_id: input.requestId,
      gpoint_user_id: input.userId,
      gpoint_character_id: input.characterId,
      ...(input.conversationId ? { conversation_id: input.conversationId } : {}),
      message_id: input.messageId,
      text: input.text,
      ...(input.locale ? { locale: input.locale } : {}),
    });
  }
}

export function createAgataIntegration(
  env: NodeJS.ProcessEnv = process.env,
): AgataIntegrationService | null {
  const client = createAgataClient(env);
  return client ? new AgataIntegrationService(client) : null;
}
