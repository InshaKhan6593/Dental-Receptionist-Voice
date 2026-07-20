// AudioWorklet that captures mic input and ships raw Float32 frames to the
// main thread. The main thread converts them to 16-bit PCM before sending.
class PcmRecorderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      // Copy: the underlying buffer is reused by the engine after process().
      this.port.postMessage(input[0].slice(0));
    }
    return true; // keep the processor alive
  }
}

registerProcessor("pcm-recorder-processor", PcmRecorderProcessor);
