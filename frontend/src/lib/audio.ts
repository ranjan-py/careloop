/**
 * Microphone capture per the pinned audio architecture (spec §8A):
 * AudioWorklet capture, downsampled in-browser to 16 kHz mono Int16 PCM.
 * NO MediaRecorder container formats — raw header-less PCM only.
 *
 * Chunks (~200 ms of audio) are handed to the caller as ArrayBuffers, ready to
 * send as binary WebSocket frames.
 *
 * Requires a secure context: Chrome on localhost for this demo (README).
 */

const TARGET_SAMPLE_RATE = 16000;
const CHUNK_SAMPLES = 3200; // 200 ms at 16 kHz

/** AudioWorkletProcessor source, loaded via a Blob URL so no static asset is
 * needed. Runs in the AudioWorkletGlobalScope (where `sampleRate` is global). */
const WORKLET_SOURCE = `
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.chunkSamples = opts.chunkSamples || 3200;
    this.ratio = sampleRate / this.targetRate;
    this.pos = 0; // fractional read position carried across blocks
    this.out = new Int16Array(this.chunkSamples);
    this.outLen = 0;
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    const n = channel.length;
    let pos = this.pos;
    while (pos < n) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const s0 = channel[i];
      const s1 = i + 1 < n ? channel[i + 1] : s0;
      let v = s0 + (s1 - s0) * frac;
      if (v > 1) v = 1;
      if (v < -1) v = -1;
      this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.outLen === this.chunkSamples) {
        const copy = this.out.slice();
        this.port.postMessage(copy.buffer, [copy.buffer]);
        this.outLen = 0;
      }
      pos += this.ratio;
    }
    this.pos = pos - n;
    return true;
  }
}
registerProcessor("pcm16-downsampler", Pcm16Downsampler);
`;

export class MicCaptureError extends Error {
  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "MicCaptureError";
    this.cause = cause;
  }
}

export class MicCapture {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;

  get running(): boolean {
    return this.ctx !== null;
  }

  /** Starts capture; `onChunk` receives 16 kHz mono Int16 PCM ArrayBuffers. */
  async start(onChunk: (pcm: ArrayBuffer) => void): Promise<void> {
    if (this.running) return;
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      throw new MicCaptureError(
        "Microphone unavailable — getUserMedia requires a secure context. " +
          "Use Chrome on localhost (see README).",
      );
    }

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (err) {
      throw new MicCaptureError(
        "Microphone permission denied or no input device found.",
        err,
      );
    }

    try {
      this.ctx = new AudioContext();
      const blobUrl = URL.createObjectURL(
        new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
      );
      try {
        await this.ctx.audioWorklet.addModule(blobUrl);
      } finally {
        URL.revokeObjectURL(blobUrl);
      }

      const source = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "pcm16-downsampler", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: {
          targetRate: TARGET_SAMPLE_RATE,
          chunkSamples: CHUNK_SAMPLES,
        },
      });
      this.node.port.onmessage = (event: MessageEvent) => {
        onChunk(event.data as ArrayBuffer);
      };
      source.connect(this.node);
      // Deliberately not connected to ctx.destination — no local playback echo.
    } catch (err) {
      await this.stop();
      throw new MicCaptureError(
        "Failed to start AudioWorklet capture pipeline.",
        err,
      );
    }
  }

  async stop(): Promise<void> {
    this.node?.port.close();
    this.node?.disconnect();
    this.node = null;
    if (this.ctx) {
      try {
        await this.ctx.close();
      } catch {
        /* already closed */
      }
      this.ctx = null;
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }
}
