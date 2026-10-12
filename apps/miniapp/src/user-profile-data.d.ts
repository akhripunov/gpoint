export function userProfileCounts(user: Record<string, unknown> | null | undefined): {
  saved: number | null;
  following: number | null;
};
