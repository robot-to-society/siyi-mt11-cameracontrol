// Minimal WHEP (WebRTC-HTTP Egress Protocol) receive-only client with auto-reconnect.

const RETRY_MS = 3000;
const DISCONNECT_GRACE_MS = 5000;
const ICE_GATHER_TIMEOUT_MS = 2000;
const CONNECT_TIMEOUT_MS = 15000; // don't sit in "connecting" when 8189 is unreachable

function waitIceGathering(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === "complete") done(); };
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, ICE_GATHER_TIMEOUT_MS);
  });
}

export class WhepPlayer {
  constructor(url, video, onState) {
    this.url = url;
    this.video = video;
    this.onState = onState;
    this.pc = null;
    this.retryTimer = null;
    this.graceTimer = null;
    this.connectTimer = null;
    this.stopped = true;
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    clearTimeout(this.graceTimer);
    clearTimeout(this.connectTimer);
    this.close();
  }

  close() {
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
  }

  scheduleRetry() {
    if (this.stopped) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.connect(), RETRY_MS);
  }

  handleConnectionState(pc) {
    if (this.pc !== pc) return; // event from a replaced connection
    const st = pc.connectionState;
    this.onState(st);
    clearTimeout(this.graceTimer);
    if (st === "connected") clearTimeout(this.connectTimer);
    if (st === "failed" || st === "closed") {
      this.scheduleRetry();
    } else if (st === "disconnected") {
      // "disconnected" often recovers by itself (e.g. LTE hiccup); retry only if it persists
      this.graceTimer = setTimeout(() => {
        if (this.pc === pc && pc.connectionState !== "connected") this.scheduleRetry();
      }, DISCONNECT_GRACE_MS);
    }
  }

  async connect() {
    this.close();
    const pc = new RTCPeerConnection();
    this.pc = pc;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = (ev) => {
      this.video.srcObject = ev.streams[0] ?? new MediaStream([ev.track]);
    };
    pc.onconnectionstatechange = () => this.handleConnectionState(pc);
    this.onState("connecting");
    clearTimeout(this.connectTimer);
    this.connectTimer = setTimeout(() => {
      if (this.pc === pc && pc.connectionState !== "connected") {
        this.onState("error: timeout (UDP/TCP 8189 unreachable?)");
        this.scheduleRetry();
      }
    }, CONNECT_TIMEOUT_MS);
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await waitIceGathering(pc);
      const res = await fetch(this.url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription.sdp,
      });
      if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 120)}`);
      await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
    } catch (e) {
      if (this.pc !== pc) return;
      this.onState(`error: ${e.message}`);
      this.scheduleRetry();
    }
  }

  /** Receive-side delay estimate in ms (jitter buffer + half RTT), or null. */
  async latencyMs() {
    if (!this.pc) return null;
    const stats = await this.pc.getStats();
    let jitterMs = null;
    let rttMs = null;
    stats.forEach((s) => {
      if (s.type === "inbound-rtp" && s.kind === "video" && s.jitterBufferEmittedCount > 0) {
        jitterMs = (s.jitterBufferDelay / s.jitterBufferEmittedCount) * 1000;
      }
      if (s.type === "candidate-pair" && s.nominated && s.currentRoundTripTime !== undefined) {
        rttMs = s.currentRoundTripTime * 1000;
      }
    });
    if (jitterMs === null && rttMs === null) return null;
    return Math.round((jitterMs ?? 0) + (rttMs ?? 0) / 2);
  }
}
