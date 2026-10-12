export function filterMessageThreads(items, query) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return items;
  return items.filter((item) => {
    const haystack = [
      item.creator.displayName,
      item.creator.slug,
      item.lastMessagePreview ?? "",
    ].join(" ").toLocaleLowerCase();
    return haystack.includes(needle);
  });
}
