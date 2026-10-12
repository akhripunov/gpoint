function validCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function userProfileCounts(user) {
  return {
    saved: validCount(user?.savedCount),
    following: validCount(user?.followingCount),
  };
}
