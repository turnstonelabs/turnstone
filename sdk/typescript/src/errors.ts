/** Raised when a turnstone server returns a non-2xx response. */
export class TurnstoneAPIError extends Error {
  constructor(
    public readonly statusCode: number,
    public readonly errorMessage: string,
  ) {
    super(`HTTP ${statusCode}: ${errorMessage}`);
    this.name = "TurnstoneAPIError";
  }
}
