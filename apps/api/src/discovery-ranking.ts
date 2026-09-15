export const DISCOVERY_RANKING_CONFIG = {
  trendingWindowHours: 24,
  popularWindowDays: 30,
  defaultLimit: 24,
  maxLimit: 50,
  smoothingPrior: 5,
  personalizationMax: 12,
  explorationShare: 0.2,
  decayHalfLifeDays: 14,
  seenWindowDays: 7,
};

/** Wilson-style low-sample guard used by future rate-based surfaces. */
export function smoothedRate(successes: number, opportunities: number, prior = DISCOVERY_RANKING_CONFIG.smoothingPrior): number {
  return (Math.max(0, successes) + prior * 0.1) / (Math.max(0, opportunities) + prior);
}

export function coldStartScore(ageHours: number): number {
  return Math.max(0, 1 - Math.max(0, ageHours) / (7 * 24));
}

export const PERSONALIZATION_EVENT_WEIGHTS: Record<string, number> = {
  VIP_CTA_CLICK: 5,
  FOLLOW_CREATOR: 4,
  REEL_COMPLETE: 3,
  CREATOR_PROFILE_VIEW: 2,
  POST_OPEN: 2,
  REEL_START: 1,
  CREATOR_IMPRESSION: 0,
  POST_IMPRESSION: 0,
  REEL_IMPRESSION: 0,
};

export function recencyDecay(ageDays: number): number {
  return Math.pow(0.5, Math.max(0, ageDays) / DISCOVERY_RANKING_CONFIG.decayHalfLifeDays);
}

export function boundedAffinity(weightedSignal: number): number {
  return Math.min(DISCOVERY_RANKING_CONFIG.personalizationMax, Math.sign(weightedSignal) * Math.log1p(Math.abs(weightedSignal)) * 4);
}

export function personalizedScore(globalScore: number, affinity: number, tagAffinity: number, seenPenalty: number, explorationBoost: number): number {
  return globalScore + Math.max(-DISCOVERY_RANKING_CONFIG.personalizationMax, Math.min(DISCOVERY_RANKING_CONFIG.personalizationMax, affinity + tagAffinity + explorationBoost)) - Math.max(0, Math.min(4, seenPenalty));
}

export type DiversityCandidate = { id: string; creatorId: string; tags: string[]; score: number };
export function diversifyCandidates<T extends DiversityCandidate>(input: T[], window = 5): T[] {
  const remaining = [...input]; const output: T[] = []; let previousCreator = ''; let previousTag = '';
  while (remaining.length) {
    const index = remaining.findIndex((candidate, position) => position >= Math.min(window, remaining.length) || (candidate.creatorId !== previousCreator && !candidate.tags.includes(previousTag)));
    const selected = remaining.splice(index < 0 ? 0 : index, 1)[0]; output.push(selected); previousCreator = selected.creatorId; previousTag = selected.tags[0] ?? '';
  }
  return output;
}
