import { describe, expect, it } from "vitest";
import { MAX_ETA_SECONDS, formatEta } from "../format";

describe("formatEta", () => {
  it("formats seconds, minutes and hours", () => {
    expect(formatEta(5)).toBe("5s");
    expect(formatEta(59.2)).toBe("1m 0s"); // ceil(59.2) = 60 -> minutes branch
    expect(formatEta(125)).toBe("2m 5s");
    expect(formatEta(3 * 3600 + 7 * 60)).toBe("3h 7m");
  });

  it("returns empty for null / undefined / NaN / Infinity / non-positive", () => {
    for (const v of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, 0, -3]) {
      expect(formatEta(v as number | null | undefined)).toBe("");
    }
  });

  it("keeps up to exactly 24 h", () => {
    expect(MAX_ETA_SECONDS).toBe(86_400);
    expect(formatEta(MAX_ETA_SECONDS)).toBe("24h 0m");
  });

  it("shows nothing beyond 24 h instead of an absurd number", () => {
    // the real incident: a stalled transfer produced "2947935474527h 58m"
    expect(formatEta(MAX_ETA_SECONDS + 1)).toBe("");
    expect(formatEta(2_947_935_474_527 * 3600)).toBe("");
    expect(formatEta(Number.MAX_SAFE_INTEGER)).toBe("");
  });
});
