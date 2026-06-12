'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let ws       = null;
let pc       = null;
let txChan   = null;
let audioCtx = null;
let mediaStream = null;
let connected = false;
let pttActive = false;
let callStartTs = null;
let callTimerInterval = null;
let lastHeard = [];

const $ = id => document.getElementById(id);

// ── DOM refs ───────────────────────────────────────────────────────────────
const dotEl      = $('status-dot');
const labelEl    = $('status-label');
const tgBadge    = $('tg-badge');
const tgInput    = $('tg-input');
const pttBtn     = $('ptt-btn');
const pttHint    = $('ptt-hint');
const callBanner = $('call-banner');
const callCs     = $('call-callsign');
const callName   = $('call-name');
const callTimer  = $('call-timer');
const lhBody     = $('lh-body');
const themeBtn   = $('theme-btn');

// ── Theme ──────────────────────────────────────────────────────────────────
const THEME_KEY = 'keyup-theme';

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  themeBtn.textContent = theme === 'dark' ? '☀' : '☾';
  localStorage.setItem(THEME_KEY, theme);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
themeBtn.addEventListener('click', toggleTheme);

// ── TG badge — click to edit ───────────────────────────────────────────────
function setTgBadge(tg) {
  tgBadge.textContent = tg ? `TG ${tg}` : 'TG —';
}

function openTgEdit() {
  tgBadge.classList.add('hidden');
  tgInput.classList.remove('hidden');
  const current = tgBadge.textContent.replace('TG ', '').trim();
  tgInput.value = current === '—' ? '' : current;
  tgInput.focus();
  tgInput.select();
}

async function commitTg() {
  const val = parseInt(tgInput.value, 10);
  tgInput.classList.add('hidden');
  tgBadge.classList.remove('hidden');
  if (val && val > 0) {
    try {
      await fetch('/api/talkgroup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ talkgroup: val }),
      });
      setTgBadge(val);
    } catch (e) { console.warn('TG change failed', e); }
  }
}

tgBadge.addEventListener('click', openTgEdit);
tgInput.addEventListener('keydown', e => {
  e.stopPropagation();  // prevent Space from reaching the PTT document handler
  if (e.key === 'Enter')  tgInput.blur();
  if (e.key === 'Escape') { tgInput.classList.add('hidden'); tgBadge.classList.remove('hidden'); }
});
tgInput.addEventListener('blur', commitTg);

// ── WebSocket ──────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen  = () => console.log('[WS] connected');
  ws.onclose = () => { setTimeout(connectWS, 3000); };
  ws.onerror = e  => console.warn('[WS] error', e);

  ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data);
    switch (msg.type) {
      case 'state':      handleState(msg.state);       break;
      case 'talkgroup':  setTgBadge(msg.talkgroup);    break;
      case 'call_start': handleCallStart(msg);          break;
      case 'call_end':   handleCallEnd(msg);            break;
    }
  };

  setInterval(() => ws && ws.readyState === WebSocket.OPEN && ws.send('ping'), 20000);
}

function handleState(state) {
  dotEl.className  = `dot ${state}`;
  labelEl.textContent = state.charAt(0).toUpperCase() + state.slice(1);

  connected = state === 'connected';
  pttBtn.disabled = !connected;
  pttHint.textContent = connected
    ? 'Hold to Talk  ·  Space or tap'
    : 'Connect to BrandMeister to transmit';
}

// ── Fetch initial status ───────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    handleState(d.state);
    setTgBadge(d.talkgroup);
    lastHeard = d.last_heard || [];
    renderLastHeard();
  } catch (e) { console.warn('status fetch failed', e); }
}

// ── WebRTC setup ───────────────────────────────────────────────────────────
async function setupWebRTC() {
  if (pc) return;

  const r = await fetch('/api/offer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdp: '', type: '' }),
  });
  const offer = await r.json();
  if (!offer || !offer.sdp) throw new Error('Empty offer from server');

  pc = new RTCPeerConnection({ iceServers: [] });
  pc.oniceconnectionstatechange = () => console.log('[ICE]', pc.iceConnectionState);

  // Server created the data channel — browser receives it via ondatachannel
  const channelReady = new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('DataChannel timeout')), 8000);
    pc.ondatachannel = ({ channel }) => {
      txChan = channel;
      channel.onmessage = ({ data }) => playPcm(data);
      if (channel.readyState === 'open') {
        clearTimeout(t); resolve();
      } else {
        channel.onopen = () => { clearTimeout(t); resolve(); };
      }
    };
  });

  await pc.setRemoteDescription({ type: offer.type, sdp: offer.sdp });
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);

  await fetch('/api/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdp: answer.sdp, type: answer.type }),
  });

  await channelReady;   // wait until SCTP+DTLS handshake completes
}

// ── Audio playback ─────────────────────────────────────────────────────────
function getAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
  return audioCtx;
}

function playPcm(buffer) {
  const ctx    = getAudioCtx();
  const view   = new Int16Array(buffer);
  const float  = new Float32Array(view.length);
  for (let i = 0; i < view.length; i++) float[i] = view[i] / 32768;
  const ab     = ctx.createBuffer(1, float.length, 8000);
  ab.copyToChannel(float, 0);
  const src = ctx.createBufferSource();
  src.buffer = ab;
  src.connect(ctx.destination);
  src.start();
}

// ── PTT ────────────────────────────────────────────────────────────────────
async function pttStart() {
  if (!connected || pttActive) return;

  getAudioCtx().resume();

  if (!mediaStream) {
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 8000, echoCancellation: true, noiseSuppression: true }, video: false });
    } catch (e) {
      alert('Microphone access denied. Please allow microphone access.');
      return;
    }
  }

  if (!pc) {
    try {
      await setupWebRTC();
    } catch (e) {
      console.error('[PTT] WebRTC setup failed:', e);
      return;
    }
  }

  if (!txChan || txChan.readyState !== 'open') {
    console.warn('[PTT] DataChannel not open:', txChan?.readyState);
    pc = null; txChan = null;   // reset so next press retries
    return;
  }

  pttActive = true;
  pttBtn.classList.add('tx');
  document.querySelector('.ptt-label').textContent = 'Transmitting…';

  await fetch('/api/ptt', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ active: true }) });

  if (txChan && txChan.readyState === 'open') {
    const src  = getAudioCtx().createMediaStreamSource(mediaStream);
    const proc = getAudioCtx().createScriptProcessor(160, 1, 1);
    proc.onaudioprocess = e => {
      if (!pttActive) { proc.disconnect(); src.disconnect(); return; }
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
      if (txChan.readyState === 'open') txChan.send(i16.buffer);
    };
    src.connect(proc);
    proc.connect(getAudioCtx().destination);
  }
}

async function pttStop() {
  if (!pttActive) return;
  pttActive = false;
  pttBtn.classList.remove('tx');
  document.querySelector('.ptt-label').textContent = 'Hold to Talk';
  await fetch('/api/ptt', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ active: false }) });
}

// ── PTT button events ──────────────────────────────────────────────────────
pttBtn.addEventListener('mousedown',  pttStart);
pttBtn.addEventListener('mouseup',    pttStop);
pttBtn.addEventListener('mouseleave', pttStop);
pttBtn.addEventListener('touchstart', e => { e.preventDefault(); pttStart(); }, { passive: false });
pttBtn.addEventListener('touchend',   e => { e.preventDefault(); pttStop();  }, { passive: false });

document.addEventListener('keydown', e => { if (e.code === 'Space' && !e.repeat && document.activeElement !== tgInput) { e.preventDefault(); pttStart(); } });
document.addEventListener('keyup',   e => { if (e.code === 'Space' && document.activeElement !== tgInput) { e.preventDefault(); pttStop(); } });

// ── Incoming call display ──────────────────────────────────────────────────
function handleCallStart(msg) {
  callCs.textContent   = msg.callsign;
  callName.textContent = msg.name || '';
  callBanner.classList.remove('hidden');
  callStartTs = Date.now();
  clearInterval(callTimerInterval);
  callTimerInterval = setInterval(() => {
    const s = Math.floor((Date.now() - callStartTs) / 1000);
    callTimer.textContent = `${s}s`;
  }, 500);
}

function handleCallEnd(msg) {
  clearInterval(callTimerInterval);
  callBanner.classList.add('hidden');
  callTimer.textContent = '';

  lastHeard.unshift({
    ts:       msg.ts,
    src_id:   msg.src_id,
    dst_id:   msg.dst_id,
    callsign: msg.callsign,
    name:     msg.name,
    duration: msg.duration,
  });
  if (lastHeard.length > 20) lastHeard.length = 20;
  renderLastHeard();
}

// ── Last heard table ───────────────────────────────────────────────────────
function renderLastHeard() {
  if (!lastHeard.length) {
    lhBody.innerHTML = '<tr><td colspan="5" class="empty">No contacts yet</td></tr>';
    return;
  }
  lhBody.innerHTML = lastHeard.map(r => {
    const t = new Date(r.ts * 1000).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' });
    const dur = r.duration != null ? `${r.duration}s` : '—';
    return `<tr>
      <td>${t}</td>
      <td class="cs">${esc(r.callsign)}</td>
      <td>${esc(r.name || '')}</td>
      <td>${r.dst_id}</td>
      <td>${dur}</td>
    </tr>`;
  }).join('');
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init ───────────────────────────────────────────────────────────────────
loadStatus();
connectWS();
