export type CreatorBotStatus = 'ACTIVE' | 'INACTIVE' | 'SUSPENDED';
export type AffiliateProgramStatus = 'UNKNOWN' | 'ACTIVE' | 'ENDED' | 'REVOKED';
export type AffiliateClickPlacement = 'EXPLORE_CARD' | 'CREATOR_PROFILE' | 'FREE_POST' | 'REEL' | 'VIP_CTA';

export type ExploreTab = 'trending' | 'popular' | 'new' | 'following' | 'for-you';
export type PostStatus = 'DRAFT' | 'PENDING' | 'PUBLISHED' | 'REJECTED' | 'HIDDEN';
export interface CurrentUserDTO { id: string; telegramUsername: string | null; firstName: string | null; isPremium: boolean | null; }
export interface CreatorSummaryDTO { id: string; slug: string; displayName: string; bio: string | null; avatarUrl: string | null; coverUrl: string | null; verified: boolean; vipPriceStars: number | null; vipOffer?: VipOfferDTO; }
export interface PostDTO { id: string; type: 'IMAGE' | 'VIDEO' | 'TEXT' | 'MIXED'; caption: string | null; media: { id: string; kind: 'IMAGE' | 'VIDEO'; readUrl: string }[]; publishedAt: string | null; likedByMe?: boolean; likesCount?: number; creator?: CreatorSummaryDTO; following?: boolean; }
export interface MediaDTO { id: string; kind: 'IMAGE' | 'VIDEO'; readUrl: string; }
export interface CreatorProfileDTO extends CreatorSummaryDTO { tags: string[]; posts: PostDTO[]; following: boolean; followersCount: number; vipOffer?: VipOfferDTO; }
export interface ReelDTO extends PostDTO { creator: CreatorSummaryDTO; }
export interface VipOfferDTO { id: string; title: string; description: string | null; priceStars: number | null; billingLabel: string | null; }
export interface VipClickResponseDTO { redirectUrl: string; clickRecorded: true; }
export interface PaginatedResponse<T> { items: T[]; nextCursor: string | null; }
export interface ExploreResponseDTO extends PaginatedResponse<CreatorSummaryDTO> { tab: ExploreTab; }

export type MessageRole = 'user' | 'assistant';
export interface ChatMessageDTO { id: string; clientMessageId?: string; role: MessageRole; text: string; occurredAt: string; incomingReadAt?: string | null; }
export interface MessageThreadDTO {
  creator: Pick<CreatorSummaryDTO, 'id' | 'slug' | 'displayName' | 'avatarUrl' | 'verified'>;
  lastMessagePreview: string | null;
  lastMessageRole: MessageRole | null;
  lastMessageAt: string;
  unreadCount: number;
}
export interface MessagesUnreadDTO { totalUnread: number; items: Array<{ creatorId: string; unreadCount: number }>; }
export interface MessageThreadListDTO { items: MessageThreadDTO[]; nextCursor: string | null; }
export interface ChatHistoryDTO extends PaginatedResponse<ChatMessageDTO> { creator: CreatorSummaryDTO; }
export interface MessagesStateDTO { enabled: boolean; chatHistory: boolean; longTermMemory: boolean; disclosureRequired: boolean; onboardingCompleted: boolean; disclosureVersion: number | null; onboardingCompletedAt: string | null; memoryOptIn: boolean; memoryOptInAt: string | null; ageAssuranceRequired?: boolean; aiAvailable?: boolean; }
export interface SendMessageDTO { userMessageId: string; status?: 'pending' | 'completed'; response?: ChatMessageDTO; }

export interface CreatorBot {
  id: string; creatorId: string; telegramBotId: string; telegramUsername: string;
  status: CreatorBotStatus; miniAppEnabled: boolean;
  affiliateProgramStatus: AffiliateProgramStatus; commissionPermille: number | null;
  durationMonths: number | null; gpointAffiliateUrl: string | null;
  gpointAffiliatePeerId: string | null; lastVerifiedAt: string | null;
}

export interface AffiliateClick {
  id: string; userId: string; creatorId: string; creatorBotId: string;
  affiliateLinkId: string; placement: AffiliateClickPlacement;
  source?: string; campaign?: string; clickedAt: string;
}

export interface AffiliateEvidence {
  source: 'GPOINT_CLICK' | 'TELEGRAM_AFFILIATE_REVENUE' | 'SELLER_PAYMENT';
  observedAt: string; externalReference?: string; stars?: number;
  commissionPermille?: number; notes?: string;
}

export function affiliateCommission(stars: number, commissionPermille: number): number {
  if (!Number.isInteger(stars) || stars < 0) throw new Error('stars must be a non-negative integer');
  if (!Number.isInteger(commissionPermille) || commissionPermille < 0 || commissionPermille > 1000) {
    throw new Error('commission_permille must be an integer from 0 to 1000');
  }
  return Math.floor(stars * commissionPermille / 1000);
}

export function parseAffiliateUrl(value: string): URL {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.hostname !== 't.me') throw new Error('affiliate URL must be an https://t.me link');
  if (!url.searchParams.has('ref') && !url.searchParams.has('startapp') && !url.searchParams.has('start')) {
    throw new Error('affiliate URL has no Telegram referral parameter');
  }
  return url;
}
