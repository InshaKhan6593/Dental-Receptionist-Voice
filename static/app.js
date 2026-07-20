// Browser test client: one button. Click to connect + capture mic at 16 kHz and
// stream PCM to the server; the agent's 24 kHz PCM reply plays back. Click again
// to stop. Same protocol as the Twilio bridge — this just skips the phone.

const INPUT_SAMPLE_RATE = 16000;
const OUTPUT_SAMPLE_RATE = 24000;

const talkBtn = document.getElementById("talk");
const talkLabel = document.getElementById("talk-label");
const statusEl = document.getElementById("status");
const voiceAgentOrigin = globalThis.VOICE_AGENT_ORIGIN || location.origin;

if (!talkBtn || !talkLabel || !statusEl) {
  console.error(
    "Test client: expected controls are missing — you're likely on a stale " +
      "cached page. Hard-reload (Cmd/Ctrl+Shift+R)."
  );
}

let ws = null;
let micContext = null;
let playbackContext = null;
let recorderNode = null;
let playerNode = null;
let mediaStream = null;
let active = false;

function setStatus(text, live) {
  statusEl.textContent = text;
  talkBtn.classList.toggle("live", !!live);
}

// --- Float32 <-> Int16 PCM conversions ---
function floatToPcm16(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function pcm16ToFloat(int16) {
  const out = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) out[i] = int16[i] / 0x8000;
  return out;
}

function base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

async function start() {
  active = true;
  talkLabel.textContent = "Stop";
  setStatus("preparing microphone…", false);

  // Build the audio graph and get mic permission BEFORE opening the socket.
  // The server sends its greeting the instant the socket connects, so the
  // playback node must already exist — otherwise the first chunks arrive with
  // no player and get dropped, and you hear the greeting from the middle.
  try {
    await setupAudio();
  } catch (err) {
    console.error("audio setup failed", err);
    setStatus("Microphone access is needed — allow it, then click again.", false);
    teardown();
    return;
  }
  if (!active) return; // user clicked Stop while the mic prompt was open

  setStatus("connecting…", false);
  const userId = Math.random().toString(36).slice(2);
  const backendUrl = new URL(voiceAgentOrigin);
  const proto = backendUrl.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${backendUrl.host}/ws/${userId}`);

  ws.onopen = () => {
    setStatus("Listening — try: “I'd like a check-up, I'm John Smith.”", true);
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "audio") {
      const pcm = new Int16Array(base64ToArrayBuffer(msg.data));
      playerNode?.port.postMessage(pcm16ToFloat(pcm));
    } else if (msg.type === "interrupted") {
      // Barge-in: drop any audio still queued for playback.
      playerNode?.port.postMessage("clear");
    }
  };

  ws.onclose = () => {
    if (active) setStatus("disconnected", false);
    teardown();
  };
  ws.onerror = () => setStatus("Connection error — please try again.", false);
}

async function setupAudio() {
  // Playback graph (24 kHz). Created inside the click gesture so the context
  // starts "running"; resume() covers browsers that still open it suspended
  // (a suspended context would silently swallow the greeting audio).
  playbackContext = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
  if (playbackContext.state === "suspended") await playbackContext.resume();
  await playbackContext.audioWorklet.addModule("/pcm-player-processor.js");
  playerNode = new AudioWorkletNode(playbackContext, "pcm-player-processor");
  playerNode.connect(playbackContext.destination);

  // Capture graph (16 kHz).
  micContext = new AudioContext({ sampleRate: INPUT_SAMPLE_RATE });
  await micContext.audioWorklet.addModule("/pcm-recorder-processor.js");
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const source = micContext.createMediaStreamSource(mediaStream);
  recorderNode = new AudioWorkletNode(micContext, "pcm-recorder-processor");
  recorderNode.port.onmessage = (event) => {
    if (ws?.readyState !== WebSocket.OPEN) return;
    const pcm16 = floatToPcm16(event.data);
    ws.send(
      JSON.stringify({ type: "audio", data: arrayBufferToBase64(pcm16.buffer) })
    );
  };
  source.connect(recorderNode);
}

function teardown() {
  mediaStream?.getTracks().forEach((t) => t.stop());
  recorderNode?.disconnect();
  playerNode?.disconnect();
  micContext?.close();
  playbackContext?.close();
  recorderNode = playerNode = micContext = playbackContext = mediaStream = null;
  active = false;
  talkLabel.textContent = "Begin";
}

function stop() {
  ws?.close();
  setStatus("Click to connect and allow microphone access.", false);
}

talkBtn.addEventListener("click", () => (active ? stop() : start()));
