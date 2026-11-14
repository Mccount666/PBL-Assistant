/* PCM16 采集处理器：Float32 麦克风数据 → Int16 帧 → 主线程 */
class Pcm16Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._frameCount = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;
    const f32 = input[0];
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this._frameCount++;
    // 每 ~40ms 发一帧（24kHz 下 1024 样本 ≈ 42.7ms）
    if (this._frameCount % 1 === 0) {
      this.port.postMessage(i16.buffer, [i16.buffer]);
    }
    return true;
  }
}

registerProcessor('pcm16-capture', Pcm16Capture);