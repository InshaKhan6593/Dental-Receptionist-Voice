// AudioWorklet that plays back streamed PCM from the model. The main thread
// pushes Float32 chunks via port.postMessage; we queue and drain them sample by
// sample so playback stays gapless. A "clear" message flushes the queue, which
// is how we implement barge-in (stop talking the instant the user interrupts).
class PcmPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];      // array of Float32Array chunks
    this.readIndex = 0;   // read offset into queue[0]
    this.port.onmessage = (event) => {
      if (event.data === "clear") {
        this.queue = [];
        this.readIndex = 0;
        return;
      }
      this.queue.push(event.data);
    };
  }

  process(_inputs, outputs) {
    const channel = outputs[0][0];
    for (let i = 0; i < channel.length; i++) {
      if (this.queue.length === 0) {
        channel[i] = 0; // underrun -> silence
        continue;
      }
      const chunk = this.queue[0];
      channel[i] = chunk[this.readIndex++];
      if (this.readIndex >= chunk.length) {
        this.queue.shift();
        this.readIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-player-processor", PcmPlayerProcessor);
