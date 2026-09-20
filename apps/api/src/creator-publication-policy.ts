type CreatorProfileTransaction = any;

export function canAutoPublishVerifiedCreatorProfile(status: string | null | undefined) {
  return status === 'DRAFT' || status === 'PENDING';
}

export function verifiedCreatorProfileDecision(input: {
  status: string | null | undefined;
  channelVerified: boolean;
  userAdministratorVerified: boolean;
  botAdministratorVerified: boolean;
}) {
  if (!input.channelVerified || !input.userAdministratorVerified || !input.botAdministratorVerified) {
    return { publish: false, reason: 'VERIFIED_CHANNEL_EVIDENCE_INCOMPLETE' as const };
  }
  if (!canAutoPublishVerifiedCreatorProfile(input.status)) {
    return { publish: false, reason: 'PROFILE_ENFORCEMENT_STATE' as const };
  }
  return { publish: true, reason: 'VERIFIED_CHANNEL' as const };
}

/**
 * Verified channel ownership is the canonical initial creator-publication
 * decision. This is deliberately not a ModerationAction: no administrator
 * performed this transition. Existing REJECTED/SUSPENDED profiles remain
 * blocked and must still be handled by administrative enforcement.
 */
export async function autoPublishVerifiedCreatorProfile(
  tx: CreatorProfileTransaction,
  input: {
    creatorId: string;
    userId: string;
    telegramChannelId: string;
    telegramChatId: string;
    verifiedAt: Date;
  },
) {
  const profile = await tx.creatorProfile.findUnique({ where: { creatorId: input.creatorId } });
  const decision = verifiedCreatorProfileDecision({ status: profile?.status, channelVerified: true, userAdministratorVerified: true, botAdministratorVerified: true });
  if (!profile || !decision.publish) return profile;

  const next = await tx.creatorProfile.update({
    where: { creatorId: input.creatorId },
    data: { status: 'APPROVED', moderationReason: null, moderatedByUserId: null, moderatedAt: null },
  });

  await tx.productEvent.create({
    data: {
      userId: input.userId,
      creatorId: input.creatorId,
      eventType: 'CREATOR_AUTO_APPROVED',
      entityType: 'TELEGRAM_CHANNEL',
      entityId: input.telegramChannelId,
      metadataJson: {
        source: 'VERIFIED_TELEGRAM_CHANNEL',
        telegramChatId: input.telegramChatId,
        userAdministratorVerified: true,
        botAdministratorVerified: true,
        verifiedAt: input.verifiedAt.toISOString(),
      },
    },
  });

  return next;
}
