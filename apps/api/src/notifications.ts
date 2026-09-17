export type NotificationEvent = 'USER_FOLLOWED_CREATOR' | 'POST_LIKED';
export interface NotificationProvider { publish(input: { event: NotificationEvent; subscriberId: string; actorUserId: string; entityId: string; idempotencyKey: string }): Promise<{ published: boolean; deduped: boolean }> }
export class DeferredNotificationProvider implements NotificationProvider {
  private readonly seen = new Set<string>();
  async publish(input: { event: NotificationEvent; subscriberId: string; actorUserId: string; entityId: string; idempotencyKey: string }) { if (this.seen.has(input.idempotencyKey)) return { published: false, deduped: true }; this.seen.add(input.idempotencyKey); return { published: false, deduped: false }; }
}
export class NovuNotificationProvider implements NotificationProvider {
  constructor(private readonly endpoint: string, private readonly apiKey: string) {}
  async publish(input: { event: NotificationEvent; subscriberId: string; actorUserId: string; entityId: string; idempotencyKey: string }) { const response = await fetch(`${this.endpoint.replace(/\/$/, '')}/v1/events/trigger`, { method: 'POST', headers: { 'content-type': 'application/json', authorization: `ApiKey ${this.apiKey}`, 'x-idempotency-key': input.idempotencyKey }, body: JSON.stringify({ name: input.event, to: input.subscriberId, payload: { actorUserId: input.actorUserId, entityId: input.entityId } }) }); if (!response.ok) throw new Error(`notification provider ${response.status}`); return { published: true, deduped: false }; }
}
export const notificationProvider: NotificationProvider = process.env.NOTIFICATIONS_PROVIDER === 'novu' && process.env.NOVU_URL && process.env.NOVU_API_KEY ? new NovuNotificationProvider(process.env.NOVU_URL, process.env.NOVU_API_KEY) : new DeferredNotificationProvider();
