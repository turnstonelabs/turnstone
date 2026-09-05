/* composer_paste_text.js — shared large-paste attachment policy.
 *
 * DOM-free so the decision can be exercised directly under Node. Paste event
 * handlers stay surface-owned: they must preserve each composer's existing
 * file-first staging path and call preventDefault() only after this helper
 * returns a File.
 */

export const PASTE_ATTACHMENT_CHARS = 2000;
// Keep the byte ceiling aligned with core/attachments.py:TEXT_DOC_SIZE_CAP.
export const TEXT_ATTACHMENT_MAX_BYTES = 512 * 1024;

const PASTED_TEXT_FILENAME = "pasted-text.txt";
const PASTED_TEXT_MIME = "text/plain";
const pastedTextSources = new WeakMap();

function hasMoreThanCharacters(text, thresholdChars) {
  let count = 0;
  // String iteration counts Unicode code points, matching Python's len()
  // more closely than UTF-16 String.length (which counts emoji twice).
  for (const _character of text) {
    count += 1;
    if (count > thresholdChars) return true;
  }
  return false;
}

export function pasteTextToFile(text) {
  if (typeof text !== "string" || text.length === 0) return null;
  if (!hasMoreThanCharacters(text, PASTE_ATTACHMENT_CHARS)) return null;

  // Blob.size is the UTF-8 byte count browsers use for the synthesized File.
  // Check it before the caller suppresses the native paste: over-cap text must
  // remain inline instead of becoming a guaranteed 413 with no textarea copy.
  if (new Blob([text]).size > TEXT_ATTACHMENT_MAX_BYTES) return null;

  const file = new File([text], PASTED_TEXT_FILENAME, {
    type: PASTED_TEXT_MIME,
  });
  pastedTextSources.set(file, text);
  return file;
}

// True for a File this module synthesized from a paste (a consumer that
// cannot take files returns false for it silently, keeping the text inline;
// a real drop or picked file still deserves its refusal message).
export function isPastedTextFile(file) {
  return pastedTextSources.has(file);
}

export function isDuplicatePastedTextFile(file, existingFiles) {
  const source = pastedTextSources.get(file);
  if (source === undefined) return false;
  for (const existing of existingFiles || []) {
    if (pastedTextSources.get(existing) === source) return true;
  }
  return false;
}

// Classic node/console bundles consume the same module at boot/event time.
if (typeof window !== "undefined") {
  window.TurnstonePasteText = {
    isDuplicatePastedTextFile,
    isPastedTextFile,
    pasteTextToFile,
  };
}
