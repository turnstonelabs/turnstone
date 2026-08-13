import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { normalizeMemoryDescription } from "../src/memory_description.js";

interface DescriptionParityCorpus {
  whitespace_code_points: number[];
  preserved_code_points: number[];
  empty_inputs: string[];
  non_string_inputs: unknown[];
  boundaries: Array<{
    label: string;
    character: string;
    count: number;
    valid: boolean;
  }>;
}

const CORPUS = JSON.parse(
  readFileSync(
    new URL("../../../tests/data/memory_description_parity.json", import.meta.url),
    "utf8",
  ),
) as DescriptionParityCorpus;

describe("memory description normalization", () => {
  it.each(CORPUS.whitespace_code_points)("folds U+%s", (codePoint) => {
    const space = String.fromCodePoint(codePoint);
    expect(normalizeMemoryDescription(`${space}alpha${space}${space}beta${space}`))
      .toBe("alpha beta");
  });

  it("preserves characters outside the explicit whitespace set", () => {
    const preserved = String.fromCodePoint(...CORPUS.preserved_code_points);
    expect(normalizeMemoryDescription(`${preserved}alpha${preserved}`)).toBe(
      `${preserved}alpha${preserved}`,
    );
  });

  it.each(CORPUS.empty_inputs)(
    "rejects empty-after-normalization input",
    (description) => {
      expect(() => normalizeMemoryDescription(description)).toThrow(
        "description is required",
      );
    },
  );

  it.each([...CORPUS.non_string_inputs, undefined])(
    "rejects non-string input %#",
    (description) => {
      expect(() => normalizeMemoryDescription(description)).toThrow(TypeError);
    },
  );

  it.each(CORPUS.boundaries)("enforces the code-point cap for $label", (boundary) => {
    const value = boundary.character.repeat(boundary.count);
    const valid = boundary.valid;
    if (valid) {
      expect(normalizeMemoryDescription(value)).toBe(value);
    } else {
      expect(() => normalizeMemoryDescription(value)).toThrow("512");
    }
  });
});
