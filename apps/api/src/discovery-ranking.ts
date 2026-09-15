export const DISCOVERY_RANKING_CONFIG = {
  trendingWindowHours: 24,
  popularWindowDays: 30,
  defaultLimit: 24,
  maxLimit: 50,
  smoothingPrior: 5,
};

/** Wilson-style low-sample guard used by future rate-based surfaces. */
export function smoothedRate(successes: number, opportunities: number, prior = DISCOVERY_RANKING_CONFIG.smoothingPrior): number {
  return (Math.max(0, successes) + prior * 0.1) / (Math.max(0, opportunities) + prior);
}

export function coldStartScore(ageHours: number): number {
  return Math.max(0, 1 - Math.max(0, ageHours) / (7 * 24));
}
