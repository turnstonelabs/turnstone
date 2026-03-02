"""Edit helpers for precise string replacement in files."""


def find_occurrences(content: str, old_string: str) -> list[int]:
    """Return 1-based line numbers where each occurrence of old_string starts."""
    if not old_string:
        return []
    # Build a prefix-sum of line starts for O(1) line-number lookup.
    line_starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_starts.append(i + 1)
    results = []
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        # bisect: find the line containing idx
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= idx:
                lo = mid
            else:
                hi = mid - 1
        results.append(lo + 1)  # 1-based
        start = idx + 1
    return results


def pick_nearest(content: str, old_string: str, near_line: int) -> int:
    """Return the char index of the occurrence of old_string nearest to near_line."""
    line_starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_starts.append(i + 1)

    best_idx = -1
    best_dist = float("inf")
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        # Find line number for this occurrence
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= idx:
                lo = mid
            else:
                hi = mid - 1
        line_num = lo + 1
        dist = abs(line_num - near_line)
        if dist < best_dist:
            best_dist = dist
            best_idx = idx
        start = idx + 1
    return best_idx
