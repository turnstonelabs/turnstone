const DESCRIPTION_WHITESPACE =
  /[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/g;

/** Internal wire-boundary normalizer shared by both SDK clients. */
export function normalizeMemoryDescription(description: unknown): string {
  if (typeof description !== "string") {
    throw new TypeError(
      "memory description is required and must be non-empty",
    );
  }
  const normalized = description
    .replace(DESCRIPTION_WHITESPACE, " ")
    .replace(/^ +| +$/g, "");
  if (!normalized) {
    throw new TypeError(
      "memory description is required and must be non-empty",
    );
  }
  if (Array.from(normalized).length > 512) {
    throw new TypeError("memory description exceeds 512 characters");
  }
  return normalized;
}
