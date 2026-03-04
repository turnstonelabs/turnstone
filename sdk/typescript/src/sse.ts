/**
 * SSE stream parser for fetch ReadableStream.
 *
 * Parses standard Server-Sent Events from a `Response.body` stream.
 * Works in browsers and Node.js 18+ natively (no dependencies).
 */

/**
 * Parse an SSE stream and yield JSON-parsed data payloads.
 *
 * Handles the standard SSE format including multi-line `data:` fields
 * (joined with `\n` per the SSE spec) and CRLF line endings.
 */
export async function* parseSSEStream<T = Record<string, unknown>>(
  response: Response,
): AsyncIterableIterator<T> {
  const body = response.body;
  if (!body) return;

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Normalize CRLF to LF
      buffer = buffer.replace(/\r\n/g, "\n");

      // Process complete SSE frames (separated by double newlines)
      const frames = buffer.split("\n\n");
      // Keep the last (possibly incomplete) frame in the buffer
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        if (!frame.trim()) continue;

        // Extract data lines from the frame, joining with \n per SSE spec
        const dataLines: string[] = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("data: ")) {
            dataLines.push(line.slice(6));
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5));
          }
        }

        if (dataLines.length === 0) continue;
        const data = dataLines.join("\n");
        if (!data.trim()) continue;

        try {
          yield JSON.parse(data) as T;
        } catch {
          // Skip malformed JSON
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
