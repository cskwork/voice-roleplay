import { toDb } from './pcm'

/**
 * Energy-based speech onset detector on 20 ms frames, used only for the local barge-in
 * trigger (the gateway's Silero VAD decides turns). Tracks a noise floor so steady
 * background noise does not trigger it.
 */
export class LevelVad {
  private floorDb = -60
  private run = 0

  constructor(
    private readonly minDb = -40,
    private readonly marginDb = 14,
    private readonly onsetFrames = 8, // 160 ms
  ) {}

  /** Returns true exactly once per detected onset. */
  update(rmsLevel: number): boolean {
    const db = toDb(rmsLevel)
    const voiced = db > Math.max(this.minDb, this.floorDb + this.marginDb)
    if (voiced) {
      this.run++
      return this.run === this.onsetFrames
    }
    this.run = 0
    // Fast attack downwards, slow rise upwards.
    this.floorDb = db < this.floorDb ? db : this.floorDb + 0.02 * (db - this.floorDb)
    return false
  }

  reset(): void {
    this.run = 0
  }
}
