"use strict";
/* ============================================================================
   Aksara Voice — voice studio controller
   Malay + English TTS. UI shows brand names only (Aksara HD / Local / Cloud).
   ========================================================================== */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const SAMPLES = {
  ms: "Selamat datang ke Aksara Voice. Saya boleh membaca teks Bahasa Melayu dan Bahasa Inggeris dengan suara yang natural. Cuba taip apa-apa ayat, kemudian tekan butang jana suara.",
  en: "Welcome to Aksara Voice. I can read English and Malay text with a natural voice. Type anything, then press generate voice.",
};
const PREVIEW = {
  ms: "Selamat datang ke Aksara Voice. Ini suara saya.",
  en: "Welcome to Aksara Voice. This is my voice.",
};
const ENGINE_LABEL = { edge: "Aksara Cloud", kokoro: "Aksara Local", chatterbox: "Aksara HD" };
/* White-label: never show third-party engine/vendor names in the UI (server messages included). */
const BRAND_SCRUB = [  // package names first, so "edge-tts" never becomes "Aksara Cloud-tts"
  [/chatterbox-tts|kokoro[-_]onnx|edge-tts/gi, "Aksara engine"],
  [/Chatterbox(\s+Multilingual)?(\s+V2)?/gi, "Aksara HD"],
  [/Kokoro(\s+Local)?/gi, "Aksara Local"],
  [/Edge(\s+Neural)?/gi, "Aksara Cloud"],
];
function brandSafe(text) {
  return BRAND_SCRUB.reduce((s, [re, to]) => s.replace(re, to), String(text ?? ""));
}
/* Card descriptor overrides for voices whose catalog note names an engine. */
const NOTE_OVERRIDE = {
  "chatterbox:ms-malaysia-f": "Natural · Malaysian accent",
  "chatterbox:ms-malaysia-m": "Natural · Malaysian accent",
};

const state = {
  catalog: null,
  voices: [],
  defaults: {},
  lastVoice: {},
  lang: "ms",
  voice: null,
  gender: "all",
  search: "",
  maxChars: 3000,
  generating: false,
  previewing: null,
  current: null,
  peaks: [],
  duration: 0,
  history: [],
  genAbort: null,
  genTimer: null,
  genStarted: 0,
};

/* Media elements are resolved once, up front, so every function below can use them. */
const audio = $("#audio");
const previewAudio = $("#preview-audio");
const seek = $("#seek");
const waveCanvas = $("#wave");
const PREVIEW_CACHE_MAX = 24; // all 20 voices stay cached (~200 KB each)
const previewCache = new Map(); // voice id -> blob: URL (revoked on eviction / page hide)
let seeking = false;

/* Client-side ceiling per request; the server keeps its own engine timeouts. */
/* Generous ceilings: a busy CPU can make local engines several times slower than usual.
   The real limit is max(base, expected time x GEN_TIMEOUT_MULT); the user can always press Cancel. */
const GEN_TIMEOUT_S = { chatterbox: 20 * 60, kokoro: 10 * 60, edge: 5 * 60 };
const GEN_TIMEOUT_MULT = 8;
/* Rough generation speed on this PC: seconds of compute per second of audio. */
const GEN_FACTOR = { chatterbox: { cpu: 3.6, cuda: 0.6 }, kokoro: 0.35, edge: 0.25 };
const LONG_TEXT_CHATTERBOX = 300;

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("vox." + key); return v === null ? fallback : v; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("vox." + key, String(value)); } catch { /* private mode */ }
  },
};

/* ------------------------------------------------------------------- utils */
function fmtTime(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function when(ts) {
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return "";
  const clock = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return `Today, ${clock}`;
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday, ${clock}`;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}, ${clock}`;
}
let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = brandSafe(message);
  el.classList.toggle("err", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 4200);
}
function langLabel(lang) { return lang === "ms" ? "Malay" : "English"; }
function initial(name) {
  const parts = (name || "?").trim().split(/\s+/);
  if (parts.length > 1 && parts[0].toLowerCase() === "melayu") {
    return parts[parts.length - 1].charAt(0).toUpperCase();
  }
  return (name || "?").trim().charAt(0).toUpperCase();
}
function voiceMeta(v) {
  return `${langLabel(v.lang)} · ${v.engine === "edge" ? "Cloud" : "Local"}`;
}
function voiceDetail(v) {
  const gender = { female: "Female", male: "Male", neutral: "Neutral" }[v.gender] || v.gender;
  return `${v.accent} · ${gender} · ${v.engine === "edge" ? "Cloud" : "Local"}`;
}
/* "Warm, natural Malay" -> "Warm · Natural · Malay" style descriptor line for the cards. */
function voiceTone(v) {
  if (NOTE_OVERRIDE[v.id]) return NOTE_OVERRIDE[v.id];
  const words = brandSafe(v.note || "").split(/\s*[,·]\s*/).filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1));
  return words.length ? words.join(" · ") : voiceDetail(v);
}
function voiceById(id) { return state.voices.find((v) => v.id === id) || null; }

/* --------------------------------------------------------------- portrait art */
function hashString(str) {
  let h = 0;
  const s = String(str || "vox");
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
/* Colour helpers for the shaded (pseudo-3D) portraits and the gradient waveforms. */
function hexToRgb(hex) {
  const h = String(hex).replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHex(rgb) {
  return "#" + rgb.map((x) => Math.max(0, Math.min(255, Math.round(x))).toString(16).padStart(2, "0")).join("");
}
function mixHex(a, b, t) {
  const x = hexToRgb(a), y = hexToRgb(b);
  return rgbToHex(x.map((v, i) => v + (y[i] - v) * t));
}
function shade(hex, amount) { // amount > 0 lightens, < 0 darkens
  return amount >= 0 ? mixHex(hex, "#ffffff", amount) : mixHex(hex, "#000000", -amount);
}

const AVATAR_BG = [["#173a7a", "#070f2a"], ["#2a1f6e", "#0a0a28"], ["#124a6b", "#05142a"], ["#3a1d5e", "#0d0822"], ["#1b3f8c", "#060d26"]];
const AVATAR_SKIN = ["#f2c9a6", "#e8b48e", "#d9a27c", "#c48a64", "#a8704f"];
const AVATAR_HAIR = ["#1d1512", "#2e1d15", "#14171f", "#3b2418", "#241c22"];
const AVATAR_SUIT = ["#1d2b4f", "#22262f", "#1a3346", "#2b2341", "#28324a"];
const AVATAR_TIE = ["#3c5dbd", "#8a2f3c", "#2f6f73", "#6b4bb8", "#b07a2c"];
const AVATAR_HIJAB = ["#ead7c3", "#dcb6c2", "#bfc8ea", "#cfe2d7", "#e8d0a6", "#c9b6e4"];
const AVATAR_TOP = ["#3a4f8f", "#7b3f6e", "#2d6b6b", "#5b4aa0", "#8a5a3c"];

function avatarSvg(v) {
  const h = hashString(v.id);
  const pick = (list, shift) => list[(h >> shift) % list.length];
  const k = "av" + (String(v.id).replace(/[^a-z0-9]/gi, "").slice(-14) || "voice");
  const bg = pick(AVATAR_BG, 0);
  const skin = pick(AVATAR_SKIN, 3);
  const hair = pick(AVATAR_HAIR, 6);
  const female = v.gender === "female";
  const hijab = female && v.lang === "ms";
  const cloth = female ? pick(AVATAR_TOP, 9) : pick(AVATAR_SUIT, 9);
  const veil = pick(AVATAR_HIJAB, 12);
  const tie = pick(AVATAR_TIE, 15);

  const defs = `
    <radialGradient id="${k}bg" cx=".3" cy=".22" r=".95"><stop offset="0" stop-color="${shade(bg[0], .12)}"/><stop offset=".55" stop-color="${bg[0]}"/><stop offset="1" stop-color="${bg[1]}"/></radialGradient>
    <radialGradient id="${k}sk" cx=".42" cy=".36" r=".72"><stop offset="0" stop-color="${shade(skin, .22)}"/><stop offset=".6" stop-color="${skin}"/><stop offset="1" stop-color="${shade(skin, -.28)}"/></radialGradient>
    <linearGradient id="${k}nk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${shade(skin, -.35)}"/><stop offset="1" stop-color="${shade(skin, -.12)}"/></linearGradient>
    <linearGradient id="${k}hr" x1=".2" y1="0" x2=".8" y2="1"><stop offset="0" stop-color="${shade(hair, .25)}"/><stop offset=".5" stop-color="${hair}"/><stop offset="1" stop-color="${shade(hair, -.4)}"/></linearGradient>
    <linearGradient id="${k}cl" x1=".15" y1="0" x2=".85" y2="1"><stop offset="0" stop-color="${shade(cloth, .25)}"/><stop offset=".55" stop-color="${cloth}"/><stop offset="1" stop-color="${shade(cloth, -.55)}"/></linearGradient>
    <linearGradient id="${k}vl" x1=".1" y1="0" x2=".9" y2="1"><stop offset="0" stop-color="${shade(veil, .35)}"/><stop offset=".5" stop-color="${veil}"/><stop offset="1" stop-color="${shade(veil, -.38)}"/></linearGradient>`;

  const face = `
    <ellipse cx="32" cy="28.5" rx="${hijab ? 10.4 : 11.4}" ry="${hijab ? 12.6 : 13.4}" fill="url(#${k}sk)"/>
    <path d="M24.6 24.6c1.4-.9 3-1.1 4.6-.6M34.8 24c1.6-.5 3.2-.3 4.6.6" stroke="${shade(hair, .1)}" stroke-width="${female ? 1 : 1.35}" stroke-linecap="round" fill="none"/>
    <ellipse cx="27.6" cy="28.4" rx="1.75" ry="1.25" fill="#fff" opacity=".85"/><ellipse cx="36.4" cy="28.4" rx="1.75" ry="1.25" fill="#fff" opacity=".85"/>
    <circle cx="27.7" cy="28.5" r="1.05" fill="#2a1a14"/><circle cx="36.5" cy="28.5" r="1.05" fill="#2a1a14"/>
    <circle cx="28.1" cy="28.1" r=".35" fill="#fff"/><circle cx="36.9" cy="28.1" r=".35" fill="#fff"/>
    <path d="M32.3 29.6c-.2 2-.8 3.4-1.6 4.2.9.6 2.2.6 3 .1" stroke="${shade(skin, -.38)}" stroke-width=".9" stroke-linecap="round" fill="none"/>
    <path d="M28.6 37.1c2.2 1.4 4.6 1.4 6.8 0" stroke="${female ? "#b4545e" : shade(skin, -.45)}" stroke-width="${female ? 1.4 : 1.1}" stroke-linecap="round" fill="none"/>
    <ellipse cx="26" cy="33.4" rx="2.4" ry="1.3" fill="#ff8a8a" opacity="${female ? .18 : .07}"/><ellipse cx="38" cy="33.4" rx="2.4" ry="1.3" fill="#ff8a8a" opacity="${female ? .18 : .07}"/>
    <path d="M42.6 21c1.6 4.2 1.6 10.4-.6 15" stroke="rgba(150,210,255,.35)" stroke-width="1.2" fill="none" stroke-linecap="round"/>`;

  let figure;
  if (hijab) {
    figure = `
      <path d="M9 64c1-9 5-15.5 10-19.5-2.2-4.5-3-9.2-3-13.6C16 19.6 23.2 11 32 11s16 8.6 16 19.9c0 4.4-.8 9.1-3 13.6 5 4 9 10.5 10 19.5z" fill="url(#${k}vl)"/>
      <path d="M19 45c4 5.5 8.2 8.2 13 8.2s9-2.7 13-8.2" stroke="${shade(veil, -.3)}" stroke-width="1" fill="none" opacity=".7"/>
      <path d="M14.5 58c3-4 6.5-6.5 10-8M49.5 58c-3-4-6.5-6.5-10-8" stroke="${shade(veil, -.25)}" stroke-width="1" fill="none" opacity=".55"/>
      <ellipse cx="32" cy="30" rx="12.6" ry="15.2" fill="${shade(veil, -.18)}"/>
      ${face}
      <path d="M20.6 25.4C21.8 17.6 26.4 13.4 32 13.4s10.2 4.2 11.4 12c-3-3.4-6.9-5.2-11.4-5.2s-8.4 1.8-11.4 5.2z" fill="url(#${k}vl)"/>
      <path d="M21.5 22.5c3-3.2 6.6-4.7 10.5-4.7s7.5 1.5 10.5 4.7" stroke="rgba(255,255,255,.35)" stroke-width=".8" fill="none"/>`;
  } else if (female) {
    figure = `
      <path d="M17.6 33c-1.8-13.4 4.6-21.8 14.4-21.8S48.2 19.6 46.4 33l1.8 16.5c-4.4-3.6-9.2-4.8-16.2-4.8s-11.8 1.2-16.2 4.8z" fill="url(#${k}hr)"/>
      <path d="M8 64c1.4-10 9.4-16 24-16s22.6 6 24 16z" fill="url(#${k}cl)"/>
      <path d="M26.6 38.5h10.8v8.2c0 2.6-2.4 4.4-5.4 4.4s-5.4-1.8-5.4-4.4z" fill="url(#${k}nk)"/>
      <path d="M24 48.6c2.4 3 5 4.4 8 4.4s5.6-1.4 8-4.4" stroke="${shade(cloth, .35)}" stroke-width="1" fill="none"/>
      <ellipse cx="20.6" cy="29.5" rx="1.8" ry="2.8" fill="${shade(skin, -.15)}"/><ellipse cx="43.4" cy="29.5" rx="1.8" ry="2.8" fill="${shade(skin, -.15)}"/>
      ${face}
      <path d="M20.4 27c-.2-8.6 4.8-14.2 11.6-14.2 7.2 0 12 5.4 11.7 14-2.6-5.4-6.6-7.8-12.2-7.6-4.2 1.4-7.8 4.2-11.1 7.8z" fill="url(#${k}hr)"/>
      <path d="M25 16.5c2.4-1.6 5-2.2 7.6-2" stroke="rgba(255,255,255,.28)" stroke-width="1" fill="none" stroke-linecap="round"/>`;
  } else {
    figure = `
      <path d="M6 64c1.6-11 10.4-17.2 26-17.2S56.4 53 58 64z" fill="url(#${k}cl)"/>
      <path d="M26.6 38.5h10.8v8.6c0 2.6-2.4 4.2-5.4 4.2s-5.4-1.6-5.4-4.2z" fill="url(#${k}nk)"/>
      <path d="M25 47.2 32 58l7-10.8-7 3.2z" fill="#eef2fa"/>
      <path d="M30.9 50.6h2.2l1.2 8.6-2.3 2.8-2.3-2.8z" fill="${tie}"/>
      <path d="M24.6 47.4 31 59.5 27 64H18.5c.4-6.6 2.5-12.4 6.1-16.6zM39.4 47.4 33 59.5l4 4.5h8.5c-.4-6.6-2.5-12.4-6.1-16.6z" fill="${shade(cloth, -.25)}" opacity=".9"/>
      <ellipse cx="20.6" cy="29.5" rx="2" ry="3" fill="${shade(skin, -.15)}"/><ellipse cx="43.4" cy="29.5" rx="2" ry="3" fill="${shade(skin, -.15)}"/>
      ${face}
      <path d="M20.4 27.4c-.8-9.4 4.4-15.6 11.8-15.6 7.8 0 12.8 5.8 11.6 15.4-1.1-4.4-3.4-7.2-7.4-8.4-3.4 2.6-9 3.8-13.6 3.4-1.2 1.6-2 3.4-2.4 5.2z" fill="url(#${k}hr)"/>
      <path d="M24.5 16.4c2.6-1.8 5.6-2.4 8.6-2" stroke="rgba(255,255,255,.25)" stroke-width="1" fill="none" stroke-linecap="round"/>`;
  }

  return `<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="${esc(v.name)}">
    <defs>${defs}</defs>
    <rect width="64" height="64" fill="url(#${k}bg)"/>
    <circle cx="16" cy="12" r="18" fill="rgba(140,200,255,.12)"/>
    ${figure}
    <path d="M0 0h64v20C44 27 20 27 0 20z" fill="rgba(255,255,255,.04)"/>
  </svg>`;
}

const WAVE_PALETTES = [["#3ee6ff", "#7c6bff"], ["#8c63ff", "#ec5cf2"], ["#3ee6ff", "#4aa9ff"], ["#b55cff", "#4aa9ff"]];
function miniWave(seed, bars = 30) {
  const h = hashString(seed || "aksara-voice");
  const [c1, c2] = WAVE_PALETTES[h % WAVE_PALETTES.length];
  const html = Array.from({ length: bars }, (_, i) => {
    const t = i / (bars - 1);
    const envelope = 0.35 + 0.65 * Math.sin(Math.PI * Math.min(1, t * 1.15));
    const noise = ((h >> (i % 23)) & 15) / 15;
    const height = Math.max(14, Math.min(100, (0.3 + 0.7 * noise) * envelope * 100));
    return `<i style="height:${height.toFixed(0)}%;--wc:${mixHex(c1, c2, t)}"></i>`;
  }).join("");
  return `<span class="mini-wave" aria-hidden="true">${html}</span>`;
}
function waveAccent(seed) {
  return WAVE_PALETTES[hashString(seed || "aksara-voice") % WAVE_PALETTES.length][0];
}

/* ------------------------------------------------------------------ audio ctx */
let audioCtx = null;
function getAudioCtx() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    audioCtx = new Ctx();
  }
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

/* --------------------------------------------------------------------- status */
function setPill(el, mode, title, label) {
  el.classList.remove("on", "off", "loading");
  if (mode) el.classList.add(mode);
  if (title) el.title = brandSafe(title);
  if (label) el.innerHTML = `<i aria-hidden="true"></i>${esc(brandSafe(label))}`;
  el.setAttribute("aria-label", title || el.textContent);
}
function updateStatus(engines) {
  if (!engines) return;
  const edge = engines.edge || {};
  const kokoro = engines.kokoro || {};
  const chatterbox = engines.chatterbox || {};
  setPill($("#edge-pill"), edge.ok ? "on" : "off",
    edge.ok ? "Aksara Cloud ready (internet)" : (edge.error || "Aksara Cloud unavailable"),
    edge.ok ? "Aksara Cloud" : "Aksara Cloud · off");
  if (kokoro.loading) setPill($("#kokoro-pill"), "loading", "Aksara Local sedang dimuatkan…", "Aksara Local · loading");
  else if (kokoro.ok) setPill($("#kokoro-pill"), "on", "Aksara Local running on this PC", "Aksara Local");
  else setPill($("#kokoro-pill"), "off", "Aksara Local gagal: " + (kokoro.error || "unavailable"), "Aksara Local · failed");
  if (chatterbox.loading) {
    setPill($("#chatterbox-pill"), "loading", "Aksara HD sedang dimuatkan… (first load ~30–60 s)", "Aksara HD · loading");
  } else if (chatterbox.ok) {
    const device = chatterbox.device === "cuda" ? "GPU" : "CPU";
    setPill($("#chatterbox-pill"), "on", `Aksara HD sedia (${device})`, `Aksara HD · ${device}`);
  } else {
    setPill($("#chatterbox-pill"), "off", "Aksara HD gagal: " + (chatterbox.error || "unavailable"), "Aksara HD · failed");
  }
}

/* -------------------------------------------------------------------- catalog */
async function loadCatalog(initial = false) {
  try {
    const response = await fetch("/api/catalog", { cache: "no-store" });
    if (!response.ok) throw new Error("Catalog " + response.status);
    const data = await response.json();
    state.catalog = data;
    state.voices = data.voices || [];
    state.defaults = data.defaults || {};
    state.maxChars = data.max_chars || 3000;
    $("#text").maxLength = state.maxChars;
    updateStatus(data.engines);
    if (initial) {
      setLang("ms", true);
    } else {
      if (state.voice && !voiceById(state.voice.id)) chooseVoiceForLang();
      renderVoices();
    }
    updateTextMeta();
    if (data.engines?.kokoro?.loading || data.engines?.chatterbox?.loading) {
      setTimeout(() => loadCatalog(false), 2500);
    }
  } catch (err) {
    setPill($("#edge-pill"), "off", "Server unavailable");
    setPill($("#kokoro-pill"), "off", "Server unavailable");
    setPill($("#chatterbox-pill"), "off", "Server unavailable");
    $("#voice-count").textContent = "Server not reachable";
    $("#voices").innerHTML = '<div class="voice-empty">Tidak dapat memuatkan senarai suara. Pastikan Aksara Voice berjalan.</div>';
    toast("Tidak dapat menghubungi server Aksara Voice.", true);
  }
}

/* ------------------------------------------------------------------ language */
function setLang(lang, initial = false) {
  const previousSample = SAMPLES[state.lang];
  state.lang = lang;
  document.documentElement.lang = lang;
  $("#text").lang = lang;
  $$("#lang-tabs button").forEach((b) => {
    const on = b.dataset.lang === lang;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  const text = $("#text");
  if (initial || !text.value.trim() || text.value.trim() === previousSample.trim()) {
    text.value = SAMPLES[lang];
  }
  chooseVoiceForLang(initial);
  updateTextMeta();
  renderVoices();
  $("#text").placeholder = lang === "ms"
    ? "Taip atau tampal teks Bahasa Melayu di sini…"
    : "Type or paste English text here…";
}

function chooseVoiceForLang(initial = false) {
  const available = state.voices.filter((v) => v.lang === state.lang);
  if (!available.length) {
    state.voice = null;
    updateVoiceChip();
    return;
  }
  const remembered = state.lastVoice[state.lang];
  const preferred = state.defaults[state.lang];
  // Respect an active Female/Male/Local filter so the chosen voice is visible in the list.
  const pool = available.filter(matchesFilter);
  const candidates = pool.length ? pool : available;
  state.voice = candidates.find((v) => v.id === remembered)
    || candidates.find((v) => v.id === preferred)
    || candidates[0];
  updateVoiceChip();
}

function matchesFilter(v) {
  if (state.gender === "local") return v.engine !== "edge";
  return state.gender === "all" || v.gender === state.gender;
}

/* After a filter change: if the selected voice is now hidden, switch to the first visible one. */
function ensureSelectedVisible() {
  if (!state.voice || matchesFilter(state.voice)) return;
  const visible = state.voices.filter((v) => v.lang === state.lang && matchesFilter(v));
  if (!visible.length) return; // nothing to switch to; renderVoices shows a note instead
  const preferred = visible.find((v) => v.id === state.defaults[state.lang]) || visible[0];
  const old = state.voice.name;
  selectVoice(preferred.id);
  toast(`Penapis aktif: suara ditukar dari ${old} ke ${preferred.name}.`);
}

function selectVoice(id) {
  const v = voiceById(id);
  if (!v) return;
  stopPreview();
  state.voice = v;
  state.lastVoice[state.lang] = v.id;
  updateVoiceChip();
  renderVoices();
  updateGenerate();
}

function updateVoiceChip() {
  const v = state.voice;
  const avatar = $("#sel-avatar");
  if (!v) {
    $("#sel-name").textContent = "No voice";
    $("#sel-meta").textContent = "—";
    avatar.textContent = "?";
    $("#engine-hint").textContent = "No voices available";
    return;
  }
  $("#sel-name").textContent = v.name;
  $("#sel-meta").textContent = voiceMeta(v);
  avatar.innerHTML = avatarSvg(v);
  const isChatterbox = v.engine === "chatterbox";
  $("#engine-hint").textContent = isChatterbox
    ? "Aksara HD · natural Malay, on this PC"
    : v.engine === "kokoro"
      ? "Aksara Local · offline"
      : "Aksara Cloud · needs internet";
  const pitchBox = $("#pitch-box");
  const pitchInput = $("#pitch");
  const expressBox = $("#express-box");
  const stabilityBox = $("#stability-box");
  const stabilityInput = $("#stability");
  pitchBox.hidden = isChatterbox;
  expressBox.hidden = !isChatterbox;
  pitchInput.disabled = v.engine !== "edge";
  pitchBox.classList.toggle("disabled", v.engine !== "edge");
  pitchBox.title = v.engine === "kokoro"
    ? "Pitch is fixed for Aksara Local voices"
    : "Shift the pitch of the Aksara Cloud voice";
  stabilityInput.disabled = !isChatterbox;
  stabilityBox.classList.toggle("disabled", !isChatterbox);
  stabilityBox.title = isChatterbox
    ? "Higher stability = fewer wild takes"
    : "Stability applies to Aksara HD voices";
  if (isChatterbox) {
    $("#express-value").textContent = Number($("#express").value).toFixed(2);
    $("#stability-value").textContent = Number($("#stability").value).toFixed(2);
  }
}

/* ------------------------------------------------------------------- voices UI */
function filteredVoices() {
  const q = state.search.trim().toLowerCase();
  return state.voices.filter((v) => {
    if (v.lang !== state.lang) return false;
    if (!matchesFilter(v)) return false;
    if (q) {
      const hay = `${v.name} ${v.accent} ${voiceTone(v)} ${ENGINE_LABEL[v.engine] || ""} ${v.engine === "edge" ? "cloud" : "local"} ${v.gender}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderVoices() {
  const list = filteredVoices();
  const host = $("#voices");
  const langVoices = state.voices.filter((v) => v.lang === state.lang);
  const localCount = langVoices.filter((v) => v.engine !== "edge").length;
  const hasChatterbox = langVoices.some((v) => v.engine === "chatterbox");
  const totalLang = langVoices.length;
  const countBits = [`${totalLang} ${state.lang === "ms" ? "Malay" : "English"} ${totalLang === 1 ? "voice" : "voices"}`];
  if (localCount) countBits.push(`${localCount} local`);
  $("#voice-count").textContent = totalLang ? countBits.join(" · ") : "No voices for this language";
  $("#library-note").textContent = state.lang === "ms"
    ? (hasChatterbox
      ? "Aksara HD runs on this PC and is the most natural. Aksara Cloud needs internet."
      : "Malay voices use Aksara Cloud and need internet.")
    : "Aksara Local voices run offline. Aksara Cloud voices need internet. Scroll for more.";
  const extra = $("#library-extra");
  let rows;
  if (state.lang === "ms") {
    const localChatter = ["chatterbox:ms-malaysia-f", "chatterbox:ms-malaysia-m"].map(voiceById).filter(Boolean);
    const yasmin = voiceById("edge:ms-MY-YasminNeural");
    const englishCount = state.voices.filter((v) => v.lang === "en").length;
    rows = `${localChatter.length ? `<div class="lib-guide-row"><i style="--c:#ffb44a"></i><b>Perempuan / Lelaki</b><span>Aksara HD — the most natural Malay with a Malaysian accent, runs on this PC.</span></div>` : ""}
      ${yasmin ? `<div class="lib-guide-row"><i style="--c:#4aa9ff"></i><b>Yasmin / Osman</b><span>Aksara Cloud voices — fast for long text, needs internet.</span></div>` : ""}
      <div class="lib-guide-row"><i style="--c:#8c63ff"></i><b>Tip</b><span>Short sentences with full stops sound most natural. Raise Stability for steadier takes.</span></div>
      ${englishCount ? `<button type="button" class="lib-switch" data-switch-lang="en">Explore ${englishCount} English voices →</button>` : ""}`;
  } else {
    const malayCount = state.voices.filter((v) => v.lang === "ms").length;
    rows = `<div class="lib-guide-row"><i style="--c:#4cf0b0"></i><b>Aksara Local</b><span>Runs offline on this PC — Heart is the signature voice.</span></div>
      <div class="lib-guide-row"><i style="--c:#4aa9ff"></i><b>Aksara Cloud</b><span>American, British, Australian and Indian accents; needs internet.</span></div>
      <div class="lib-guide-row"><i style="--c:#8c63ff"></i><b>Tip</b><span>Press a voice's play button to hear a preview before generating.</span></div>
      ${malayCount ? `<button type="button" class="lib-switch" data-switch-lang="ms">Explore ${malayCount} Malay voices →</button>` : ""}`;
  }
  extra.classList.toggle("open", !!state.guideOpen);
  extra.innerHTML = `<div class="guide-head">
      <span class="guide-bulb" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.8.6 1.3 1.5 1.4 2.4L10 17h4l.2-.8c.1-.9.6-1.8 1.4-2.4A6 6 0 0 0 12 3z"/></svg></span>
      <span class="guide-copy"><b>Voice Guide</b><small>Tips for the best results</small></span>
      <button type="button" class="link-btn" data-guide-toggle aria-expanded="${!!state.guideOpen}">${state.guideOpen ? "Hide guide" : "View guide"} <span aria-hidden="true">${state.guideOpen ? "↑" : "→"}</span></button>
    </div>
    <div class="guide-body">${rows}</div>`;
  // Never let the selected voice silently disappear: say so and offer a way back.
  const selectedHidden = state.voice && state.voice.lang === state.lang && !list.some((v) => v.id === state.voice.id);
  const hiddenNote = selectedHidden
    ? `<div class="voice-hidden-note" role="status">Dipilih: <b>${esc(state.voice.name)}</b> (tersembunyi oleh ${state.search.trim() ? "carian" : "penapis"})
        <button type="button" data-show-all>Tunjuk semua</button></div>`
    : "";
  if (!list.length) {
    host.innerHTML = hiddenNote + '<div class="voice-empty">Tiada suara yang sepadan. Cuba carian atau penapis lain.</div>';
    return;
  }
  host.innerHTML = hiddenNote + list.map((v) => {
    const selected = state.voice?.id === v.id;
    const popular = v.id === state.defaults[state.lang];
    return `<div class="voice-card${selected ? " selected" : ""}" data-id="${esc(v.id)}" role="button" tabindex="0" aria-pressed="${selected}">
      <span class="v-avatar" style="--c:${esc(v.color || "#5ce1ff")}">${avatarSvg(v)}</span>
      <span class="v-body">
        <span class="v-name"><b>${esc(v.name)}</b>${popular ? '<i class="v-popular">Popular</i>' : ""}</span>
        <small title="${esc(voiceDetail(v))}">${esc(voiceTone(v))}</small>
        <small>${esc(v.accent)} <span class="v-tag ${v.engine === "edge" ? "cloud" : "local"}">${v.engine === "edge" ? "Cloud" : "Local"}</span></small>
      </span>
      <button class="v-play" type="button" data-preview="${esc(v.id)}" title="Preview ${esc(v.name)}" aria-label="Preview ${esc(v.name)}">
        <svg class="ico-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
        <svg class="ico-stop" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v10H7z"/></svg>
      </button>
    </div>`;
  }).join("");
  if (state.previewing) markPreview(state.previewing, previewAudio.paused ? "loading" : "playing");
}

/* ------------------------------------------------------------------ text meta */
function updateTextMeta() {
  const text = $("#text").value;
  const count = $("#count");
  count.textContent = `${text.length.toLocaleString()} / ${state.maxChars.toLocaleString()}`;
  count.classList.toggle("warn", text.length > state.maxChars * 0.9);
  const rate = state.lang === "ms" ? 11.5 : 13.5;
  const estimate = Math.max(0, Math.round(text.trim().length / rate));
  $("#estimate").textContent = `≈ ${fmtTime(estimate)} audio`;
  updateLongWarn(estimate);
  updateGenerate();
}

/* Expected compute time for the current voice, in seconds (rough, for UX only). */
function estimateGenSeconds(audioSeconds, voice = state.voice) {
  if (!voice) return 0;
  if (voice.engine === "chatterbox") {
    const device = state.catalog?.engines?.chatterbox?.device === "cuda" ? "cuda" : "cpu";
    return Math.round(4 + audioSeconds * GEN_FACTOR.chatterbox[device]);
  }
  return Math.round(1 + audioSeconds * (GEN_FACTOR[voice.engine] || 0.5));
}

function fallbackEdgeVoice() {
  const v = state.voice;
  const edge = state.voices.filter((x) => x.lang === (v?.lang || state.lang) && x.engine === "edge");
  return edge.find((x) => x.gender === v?.gender) || edge[0] || null;
}

function updateLongWarn(audioSeconds) {
  const box = $("#long-warn");
  const v = state.voice;
  const chars = $("#text").value.trim().length;
  const onCpu = state.catalog?.engines?.chatterbox?.device !== "cuda";
  const show = !!v && v.engine === "chatterbox" && onCpu && chars > LONG_TEXT_CHATTERBOX;
  box.hidden = !show;
  if (!show) return;
  const edge = fallbackEdgeVoice();
  const secs = estimateGenSeconds(audioSeconds);
  box.innerHTML = `<span>Teks panjang untuk Aksara HD (CPU) — anggaran <b>~${fmtTime(secs)}</b> untuk jana.
    ${edge ? "Aksara Cloud jauh lebih laju (perlu internet)." : ""}</span>
    ${edge ? `<button type="button" data-use-voice="${esc(edge.id)}">Guna ${esc(edge.name)} (Cloud)</button>` : ""}`;
}

function updateGenerate() {
  $("#generate").disabled = state.generating || !state.voice || !$("#text").value.trim();
  $("#regen").disabled = state.generating;
}

/* Keep the library at the height of the left workspace so its voice list scrolls. */
let libraryHeightFrame = null;
function syncLibraryHeight() {
  if (libraryHeightFrame) return;
  libraryHeightFrame = requestAnimationFrame(() => {
    libraryHeightFrame = null;
    const library = $(".library");
    const left = $(".workspace-left");
    if (!library || !left) return;
    if (window.innerWidth <= 1180) {
      library.style.height = "";
      return;
    }
    const height = left.offsetHeight;
    if (height > 220) library.style.height = height + "px";
  });
}

/* ------------------------------------------------------------------- generate */
function setGenerating(on, expectSeconds = 0) {
  state.generating = on;
  const btn = $("#generate");
  btn.classList.toggle("loading", on);
  btn.setAttribute("aria-busy", String(on));
  btn.querySelector(".spinner").hidden = !on;
  const label = btn.querySelector(".gen-label");
  const hint = btn.querySelector(".gen-hint");
  const cancel = $("#cancel-gen");
  clearInterval(state.genTimer);
  state.genTimer = null;
  if (on) {
    state.genStarted = Date.now();
    const tick = () => {
      const secs = Math.floor((Date.now() - state.genStarted) / 1000);
      label.textContent = `Generating… ${secs}s`;
      const slow = expectSeconds > 0 && secs > Math.max(30, expectSeconds * 2.5);
      if (hint) hint.textContent = slow ? "lambat" : (expectSeconds > 8 ? `~${fmtTime(expectSeconds)}` : "");
      if (slow && !state.genSlowWarned) {
        state.genSlowWarned = true;
        toast("Penjanaan lebih lambat dari biasa — PC mungkin sibuk (penjanaan lain, model lain). Ia masih berjalan; tekan Cancel jika mahu berhenti.");
      }
    };
    tick();
    state.genTimer = setInterval(tick, 1000);
  } else {
    label.textContent = "Generate Voice";
    if (hint) hint.textContent = "Ctrl ↵";
  }
  cancel.hidden = !on;
  $("#gen-status").textContent = on ? "Menjana suara…" : "";
  updateGenerate();
}

function cancelGenerate() {
  if (state.genAbort) state.genAbort.abort("cancel");
}

async function generate() {
  if (state.generating) return; // no double submit (button, Ctrl+Enter, regenerate)
  const text = $("#text").value.trim();
  if (!text) { toast("Taip teks dahulu."); $("#text").focus(); return; }
  if (!state.voice) { toast("Pilih suara dahulu."); return; }
  if (text.length > state.maxChars) {
    toast(`Teks terlalu panjang (${text.length} / ${state.maxChars} aksara).`, true);
    return;
  }
  stopPreview();
  if (!audio.paused) audio.pause();
  const voice = state.voice;
  const audioEstimate = text.length / (state.lang === "ms" ? 11.5 : 13.5);
  const controller = new AbortController();
  const expected = estimateGenSeconds(audioEstimate, voice);
  const limit = Math.max(GEN_TIMEOUT_S[voice.engine] || 300, Math.round(expected * GEN_TIMEOUT_MULT));
  state.genSlowWarned = false;
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort("timeout"); }, limit * 1000);
  state.genAbort = controller;
  setGenerating(true, expected);
  const started = Date.now();
  try {
    const response = await fetch("/api/tts", {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        voice: voice.id,
        speed: Number($("#speed").value),
        pitch: voice.engine === "edge" ? Number($("#pitch").value) : 0,
        exaggeration: Number($("#express").value),
        stability: Number($("#stability").value),
        save: true,
      }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, "Gagal menjana suara."));
    const blob = await response.blob();
    const id = response.headers.get("X-History-Id");
    const serverUrl = response.headers.get("X-Audio-Url");
    const meta = {
      id,
      serverUrl,
      url: URL.createObjectURL(blob),
      duration: parseFloat(response.headers.get("X-Duration") || "0"),
      voiceName: response.headers.get("X-Voice-Name") || voice.name,
      engine: response.headers.get("X-Engine") || voice.engine,
      ext: response.headers.get("X-Ext") || "mp3",
      text,
      speed: Number($("#speed").value),
      pitch: Number($("#pitch").value),
      stability: Number($("#stability").value),
      voiceId: voice.id,
    };
    loadIntoPlayer(meta, true);
    if (voice.engine === "edge") setPill($("#edge-pill"), "on", "Aksara Cloud ready");
    const genSeconds = (Date.now() - started) / 1000;
    toast(`Siap · ${fmtTime(meta.duration)} audio · ${genSeconds.toFixed(1)}s`);
    loadHistory();
  } catch (err) {
    if (controller.signal.aborted) {
      if (timedOut) {
        toast(`Masa tamat selepas ${fmtTime(limit)}. Jika server masih menyiapkannya, audio akan muncul di Recent Generations.`, true);
      } else {
        toast("Penjanaan dibatalkan.");
      }
      watchLateResult(text, started); // an older server may still finish and save it
      return;
    }
    const msg = String(err.message || err);
    const offline = err instanceof TypeError; // fetch() network failure
    const hint = offline
      ? " Server Aksara Voice tidak dapat dihubungi — pastikan ia masih berjalan."
      : voice.engine === "edge" && /edge|internet|fail/i.test(msg)
        ? ` Cuba suara ${voice.lang === "ms" ? "Aksara HD" : "Aksara Local"} (local) atau semak sambungan internet.` : "";
    toast((offline ? "Gagal menjana suara." : msg) + hint, true);
    if (voice.engine === "edge" && !offline) setPill($("#edge-pill"), "off", "Last Aksara Cloud request failed");
  } finally {
    clearTimeout(timeout);
    if (state.genAbort === controller) state.genAbort = null;
    setGenerating(false);
  }
}

/* --------------------------------------------------------------------- player */
function loadIntoPlayer(meta, autoplay = false) {
  if (state.current?.url?.startsWith("blob:")) {
    try { URL.revokeObjectURL(state.current.url); } catch { /* ignore */ }
  }
  state.current = meta;
  state.peaks = [];
  state.duration = meta.duration || 0;
  audio.src = meta.url;
  audio.load();
  $("#player").hidden = false;
  $("#player-empty").hidden = true;
  $("#player-voice").textContent = `${meta.voiceName || "Voice"} · ${ENGINE_LABEL[meta.engine] || meta.engine}`;
  syncLibraryHeight();
  const dl = $("#download");
  dl.href = meta.serverUrl || meta.url;
  dl.download = downloadName(meta);
  dl.removeAttribute("aria-disabled");
  $("#seek").value = 0;
  paintRange(seek);
  $("#time").textContent = `0:00 / ${fmtTime(state.duration)}`;
  requestAnimationFrame(() => drawWave(0));
  computePeaks(meta.url);
  if (autoplay) {
    audio.play().catch((err) => {
      if (err && err.name === "NotAllowedError") toast("Tekan butang play untuk mendengar audio.");
    });
  }
}

/* Back to the empty state (e.g. the loaded generation was deleted). */
function resetPlayer() {
  if (!audio.paused) audio.pause();
  if (state.current?.url?.startsWith("blob:")) {
    try { URL.revokeObjectURL(state.current.url); } catch { /* ignore */ }
  }
  state.current = null;
  state.peaks = [];
  state.duration = 0;
  audio.removeAttribute("src");
  audio.load();
  $("#play").classList.remove("playing");
  $("#play").setAttribute("aria-label", "Play");
  $("#player").hidden = true;
  $("#player-empty").hidden = false;
  const dl = $("#download");
  dl.removeAttribute("href");
  dl.setAttribute("aria-disabled", "true");
  syncLibraryHeight();
}

function downloadName(meta) {
  const slug = String(meta.voiceName || "voice").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const date = new Date().toISOString().slice(0, 10);
  return `aksara-voice-${slug}-${date}.${meta.ext || "mp3"}`;
}

async function computePeaks(url) {
  try {
    const ctx = getAudioCtx();
    if (!ctx) throw new Error("no audio context");
    const buffer = await (await fetch(url)).arrayBuffer();
    const decoded = await ctx.decodeAudioData(buffer.slice(0));
    const channel = decoded.getChannelData(0);
    const bars = 120;
    const step = Math.max(1, Math.floor(channel.length / bars));
    const peaks = [];
    for (let i = 0; i < bars; i++) {
      let max = 0;
      const start = i * step;
      const stride = Math.max(1, Math.floor(step / 32));
      for (let j = start; j < start + step && j < channel.length; j += stride) {
        const value = Math.abs(channel[j]);
        if (value > max) max = value;
      }
      peaks.push(max);
    }
    state.peaks = peaks;
    if (decoded.duration) state.duration = decoded.duration;
    drawWave(audio.currentTime / (state.duration || 1));
  } catch {
    state.peaks = [];
    drawWave(0);
  }
}

function drawWave(progress = 0) {
  if (!waveCanvas || $("#player").hidden) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const width = waveCanvas.clientWidth;
  const height = waveCanvas.clientHeight;
  if (!width || !height) return;
  if (waveCanvas.width !== Math.round(width * dpr) || waveCanvas.height !== Math.round(height * dpr)) {
    waveCanvas.width = Math.round(width * dpr);
    waveCanvas.height = Math.round(height * dpr);
  }
  const ctx = waveCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const peaks = state.peaks;
  const mid = height / 2;
  if (!peaks.length) {
    ctx.strokeStyle = "rgba(255,255,255,.12)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(width, mid); ctx.stroke();
    return;
  }
  const gap = 2.4;
  const barWidth = Math.max(1.6, (width - gap * (peaks.length - 1)) / peaks.length);
  const progressX = Math.max(0, Math.min(1, progress)) * width;
  const gradient = ctx.createLinearGradient(0, 0, width, 0);
  gradient.addColorStop(0, "#3ee6ff");
  gradient.addColorStop(.4, "#4aa9ff");
  gradient.addColorStop(.7, "#8c63ff");
  gradient.addColorStop(1, "#ec5cf2");
  const drawBars = (alpha, glow) => {
    ctx.globalAlpha = alpha;
    ctx.shadowColor = glow ? "rgba(90,190,255,.85)" : "transparent";
    ctx.shadowBlur = glow ? 8 : 0;
    ctx.fillStyle = gradient;
    peaks.forEach((peak, i) => {
      const x = i * (barWidth + gap);
      if (glow && x > progressX) return;
      const h = Math.max(3, Math.min(1, peak * 1.4) * (height - 8));
      const y = mid - h / 2;
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, barWidth, h, barWidth / 2); ctx.fill(); }
      else ctx.fillRect(x, y, barWidth, h);
    });
  };
  drawBars(0.38, false);              // the whole clip, dimmed
  if (progressX > 0) drawBars(1, true); // played part, bright with glow
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
  ctx.fillStyle = "#eafcff";
  ctx.fillRect(progressX, 2, 1.5, height - 4);
}

function togglePlay() {
  if (audio.paused) {
    stopPreview();
    audio.play().catch(() => toast("Audio tidak dapat dimainkan.", true));
  } else {
    audio.pause();
  }
}

function setPlayButton(playing) {
  $("#play").classList.toggle("playing", playing);
  $("#play").setAttribute("aria-label", playing ? "Pause" : "Play");
}
audio.addEventListener("play", () => setPlayButton(true));
audio.addEventListener("pause", () => setPlayButton(false));
audio.addEventListener("ended", () => { setPlayButton(false); drawWave(1); });
audio.addEventListener("loadedmetadata", () => {
  if (isFinite(audio.duration) && audio.duration > 0) state.duration = audio.duration;
  $("#time").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(state.duration)}`;
});
audio.addEventListener("timeupdate", () => {
  const duration = state.duration || audio.duration || 0;
  const progress = duration ? audio.currentTime / duration : 0;
  if (!seeking) { seek.value = String(Math.round(progress * 1000)); paintRange(seek); }
  $("#time").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(duration)}`;
  drawWave(progress);
});
audio.addEventListener("error", () => {
  if (state.current) toast("Fail audio tidak dapat dimainkan.", true);
});

seek.addEventListener("pointerdown", () => { seeking = true; });
window.addEventListener("pointerup", () => { seeking = false; });
seek.addEventListener("input", () => {
  const duration = state.duration || audio.duration || 0;
  if (!duration) return;
  audio.currentTime = (Number(seek.value) / 1000) * duration;
  drawWave(Number(seek.value) / 1000);
});
waveCanvas.addEventListener("click", (e) => {
  const duration = state.duration || audio.duration || 0;
  if (!duration) return;
  const rect = waveCanvas.getBoundingClientRect();
  const progress = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  audio.currentTime = progress * duration;
  drawWave(progress);
});

new ResizeObserver(() => drawWave(audio.currentTime / (state.duration || 1))).observe(waveCanvas);

/* ------------------------------------------------------------------- preview */
/* Reflect preview state on the card's play button: "loading" | "playing" | null. */
function markPreview(id, mode) {
  $$(".v-play").forEach((b) => {
    const mine = mode && b.dataset.preview === id;
    b.classList.toggle("loading", mine && mode === "loading");
    b.classList.toggle("playing", mine && mode === "playing");
    const name = voiceById(b.dataset.preview)?.name || "";
    b.setAttribute("aria-label", mine ? `Stop preview ${name}` : `Preview ${name}`);
    b.title = mine ? `Stop preview` : `Preview ${name}`;
  });
}

function stopPreview() {
  if (!previewAudio.paused) previewAudio.pause();
  previewAudio.removeAttribute("src");
  state.previewing = null;
  markPreview(null, null);
}
previewAudio.addEventListener("ended", () => { state.previewing = null; markPreview(null, null); });
previewAudio.addEventListener("error", () => {
  if (!previewAudio.getAttribute("src")) return; // src removed by stopPreview()
  state.previewing = null;
  markPreview(null, null);
});

function cachePreview(id, url) {
  previewCache.set(id, url);
  while (previewCache.size > PREVIEW_CACHE_MAX) {
    const [oldId, oldUrl] = previewCache.entries().next().value;
    previewCache.delete(oldId);
    if (previewAudio.src !== oldUrl) URL.revokeObjectURL(oldUrl);
  }
}
function clearPreviewCache() {
  previewCache.forEach((url) => { try { URL.revokeObjectURL(url); } catch { /* ignore */ } });
  previewCache.clear();
}

async function previewVoice(id) {
  const voice = voiceById(id);
  if (!voice) return;
  if (state.previewing === id) { stopPreview(); return; }
  stopPreview();
  if (!audio.paused) audio.pause();
  state.previewing = id;
  markPreview(id, "loading");
  try {
    let url = previewCache.get(id);
    if (url) {
      previewCache.delete(id); // refresh LRU order
      previewCache.set(id, url);
    } else {
      const response = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: PREVIEW[voice.lang] || PREVIEW.en,
          voice: id,
          speed: 1,
          pitch: 0,
          exaggeration: 0.5,
          stability: 0.9,
          save: false,
        }),
      });
      if (!response.ok) throw new Error(await errorMessage(response, "Preview gagal."));
      url = URL.createObjectURL(await response.blob());
      cachePreview(id, url);
    }
    if (state.previewing !== id) return;
    previewAudio.src = url;
    await previewAudio.play();
    if (state.previewing === id) markPreview(id, "playing");
  } catch (err) {
    if (state.previewing !== id) return; // superseded or stopped meanwhile
    toast(String(err.message || err), true);
    state.previewing = null;
    markPreview(null, null);
  }
}

async function errorMessage(response, fallback) {
  try { const j = await response.json(); if (j.error) return j.error; } catch { /* not JSON */ }
  return `${fallback} (HTTP ${response.status})`;
}

/* After a timeout/cancel, an older server (or one that could not stop in time) may still save the
   audio. Check history for a few minutes and surface it instead of losing it silently. */
function watchLateResult(text, startedMs) {
  clearInterval(state.lateTimer);
  const deadline = Date.now() + 10 * 60 * 1000;
  const known = new Set(state.history.map((x) => x.id));
  state.lateTimer = setInterval(async () => {
    if (Date.now() > deadline || state.generating) { clearInterval(state.lateTimer); return; }
    await loadHistory();
    const flat = (t) => String(t || "").replace(/\s+/g, " ").trim(); // server normalises whitespace
    const late = state.history.find((x) => !known.has(x.id) && flat(x.text) === flat(text) && x.created * 1000 >= startedMs - 5000);
    if (late) {
      clearInterval(state.lateTimer);
      toast(`Audio "${late.voice}" siap juga — ada di Recent Generations.`);
    }
  }, 15000);
}

/* -------------------------------------------------------------------- history */
async function loadHistory() {
  try {
    const response = await fetch("/api/history?limit=30", { cache: "no-store" });
    if (!response.ok) throw new Error("history " + response.status);
    const data = await response.json();
    state.history = data.items || [];
    renderHistory();
  } catch { /* server may be starting */ }
}

/* How many cards fit on one row of the history grid right now (4 on desktop, 1 on phones). */
function historyColumns() {
  const cols = getComputedStyle($("#history")).gridTemplateColumns.split(" ").filter(Boolean).length;
  return Math.max(1, cols || 4);
}

function renderHistory() {
  const host = $("#history");
  const toggle = $("#history-toggle");
  const cols = historyColumns();
  state.historyCols = cols;
  if (!state.history.length) {
    toggle.hidden = true;
    host.innerHTML = Array.from({ length: cols }, (_, n) => `<div class="hist-item hist-ghost" aria-hidden="true">
      <span class="hist-ghost-orb"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></span>
      <div class="hist-visual">
        <span class="mini-wave ghost-wave">${Array.from({ length: 30 }, (_, i) => `<i style="height:${18 + ((i * 17 + n * 11) % 62)}%"></i>`).join("")}</span>
        <div class="hist-meta"><b>--:--</b><small>No audio yet</small></div>
      </div>
    </div>`).join("");
    return;
  }
  const expanded = !!state.historyExpanded;
  const shown = expanded ? state.history : state.history.slice(0, cols);
  toggle.hidden = state.history.length <= cols;
  toggle.innerHTML = expanded
    ? 'Show less <span aria-hidden="true">↑</span>'
    : `View all (${state.history.length}) <span aria-hidden="true">→</span>`;
  toggle.setAttribute("aria-expanded", String(expanded));
  host.innerHTML = shown.map((item) => {
    const snippet = String(item.text || "").replace(/\s+/g, " ").trim();
    const ext = String(item.url || "").endsWith(".wav") ? "wav" : "mp3";
    const slug = String(item.voice || "voice").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const label = `${item.voice} · ${fmtTime(item.duration)} · ${when(item.created)}`;
    return `<div class="hist-item" data-id="${esc(item.id)}" title="${esc(snippet)}" style="--hc:${waveAccent(item.id)}">
      <button class="hist-play" type="button" data-play="${esc(item.id)}" title="Play" aria-label="Play ${esc(label)}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <div class="hist-visual">
        ${miniWave(item.id)}
        <div class="hist-meta"><b>${fmtTime(item.duration)}</b><span aria-hidden="true">·</span><small>${esc(when(item.created))}</small></div>
        <span class="hist-voice">${esc(item.voice)}</span>
      </div>
      <div class="hist-actions">
        <a href="${esc(item.url)}" download="aksara-voice-${esc(slug || "voice")}-${esc(item.id)}.${ext}" title="Download" aria-label="Download ${esc(label)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 20h14"/></svg>
        </a>
        <button class="del" type="button" data-del="${esc(item.id)}" title="Delete" aria-label="Delete ${esc(label)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9.5 7V4.5h5V7M7 7l1 13h8l1-13"/></svg>
        </button>
      </div>
    </div>`;
  }).join("");
}

function playHistory(item) {
  stopPreview();
  loadIntoPlayer({
    id: item.id,
    url: item.url,
    serverUrl: item.url,
    duration: item.duration || 0,
    voiceName: item.voice,
    engine: item.engine,
    ext: item.url.endsWith(".wav") ? "wav" : "mp3",
    text: item.text,
    speed: item.speed,
    pitch: item.pitch,
    stability: item.stability,
    voiceId: item.voice_id,
  }, true);
}

async function deleteHistory(id) {
  try {
    const response = await fetch("/api/history/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    if (!response.ok && response.status !== 404) throw new Error(await errorMessage(response, "Delete failed"));
    state.history = state.history.filter((item) => item.id !== id);
    if (state.current?.id === id) resetPlayer();
    renderHistory();
    toast("Generasi dipadam.");
  } catch (err) { toast("Gagal memadam generasi. " + (err.message || ""), true); }
}

/* ------------------------------------------------------------------- volume */
function applyVolume() {
  const v = Math.max(0, Math.min(1, Number($("#volume").value)));
  const muted = $("#mute").getAttribute("aria-pressed") === "true";
  audio.volume = v;
  previewAudio.volume = v;
  audio.muted = muted;
  previewAudio.muted = muted;
  const mute = $("#mute");
  mute.classList.toggle("muted", muted || v === 0);
  mute.title = muted ? "Unmute" : "Mute";
  mute.setAttribute("aria-label", muted ? "Unmute" : "Mute");
  store.set("volume", v.toFixed(2));
  store.set("muted", muted ? "1" : "0");
}
function setMuted(muted) {
  $("#mute").setAttribute("aria-pressed", String(muted));
  applyVolume();
}

/* -------------------------------------------------------------------- paste */
function insertScript(text) {
  const box = $("#text");
  let value = String(text || "");
  let clipped = false;
  if (value.length > state.maxChars) {
    value = value.slice(0, state.maxChars);
    clipped = true;
  }
  box.value = value;
  updateTextMeta();
  box.focus();
  toast(clipped
    ? `Teks ditampal dan dipotong ke ${state.maxChars.toLocaleString()} aksara (had maksimum).`
    : "Teks ditampal.");
}
async function pasteFromClipboard() {
  const box = $("#text");
  if (!navigator.clipboard?.readText) {
    box.focus();
    toast("Pelayar ini tidak beri akses clipboard. Klik dalam kotak teks dan tekan Ctrl+V.");
    return;
  }
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) { toast("Clipboard kosong — salin teks dahulu."); return; }
    insertScript(text);
  } catch {
    box.focus();
    box.select();
    toast("Akses clipboard disekat oleh pelayar. Tekan Ctrl+V sekarang untuk tampal.");
  }
}

/* ------------------------------------------------------------------ bindings */
function bind() {
  $("#lang-tabs").addEventListener("click", (e) => {
    const button = e.target.closest("[data-lang]");
    if (!button || button.dataset.lang === state.lang) return;
    stopPreview();
    setLang(button.dataset.lang);
  });

  $("#text").addEventListener("input", updateTextMeta);
  $("#sample").addEventListener("click", () => {
    $("#text").value = SAMPLES[state.lang];
    updateTextMeta();
    $("#text").focus();
  });
  $("#paste").addEventListener("click", pasteFromClipboard);
  $("#clear").addEventListener("click", () => {
    $("#text").value = "";
    updateTextMeta();
    $("#text").focus();
  });
  $("#long-warn").addEventListener("click", (e) => {
    const button = e.target.closest("[data-use-voice]");
    if (!button) return;
    selectVoice(button.dataset.useVoice);
    updateTextMeta();
    toast(`Suara ditukar ke ${state.voice?.name} (Cloud).`);
  });

  $("#speed").addEventListener("input", (e) => {
    $("#speed-value").textContent = Number(e.target.value).toFixed(2) + "×";
  });
  $("#pitch").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#pitch-value").textContent = (v > 0 ? "+" : "") + v + " Hz";
  });
  $("#express").addEventListener("input", (e) => {
    $("#express-value").textContent = Number(e.target.value).toFixed(2);
  });
  $("#stability").addEventListener("input", (e) => {
    $("#stability-value").textContent = Number(e.target.value).toFixed(2);
  });

  $("#volume").addEventListener("input", () => {
    $("#mute").setAttribute("aria-pressed", "false"); // moving the slider unmutes
    applyVolume();
  });
  $("#mute").addEventListener("click", () => setMuted($("#mute").getAttribute("aria-pressed") !== "true"));

  $$("[data-coming]").forEach((button) => {
    button.addEventListener("click", (e) => {
      e.preventDefault();
      toast(`${button.dataset.coming} akan datang. Buat masa ini: Text to Speech.`);
    });
  });
  $$("[data-scroll]").forEach((button) => {
    button.addEventListener("click", (e) => {
      e.preventDefault();
      const target = document.getElementById(button.dataset.scroll);
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      if (button.closest(".side-nav")) {
        $$(".side-nav button").forEach((b) => {
          b.classList.toggle("active", b === button);
          if (b === button) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
        });
      }
    });
  });
  $("#side-guide").addEventListener("click", () => {
    if (state.lang !== "ms") setLang("ms");
    state.guideOpen = true;
    renderVoices();
    $("#library").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#library-extra").classList.add("flash");
    setTimeout(() => $("#library-extra").classList.remove("flash"), 1200);
  });

  $("#generate").addEventListener("click", generate);
  $("#cancel-gen").addEventListener("click", cancelGenerate);
  $("#play").addEventListener("click", togglePlay);
  $("#regen").addEventListener("click", generate);
  $("#download").addEventListener("click", (e) => {
    if (!state.current) {
      e.preventDefault();
      toast("Tiada audio untuk dimuat turun. Jana suara dahulu.");
    }
  });

  $("#voice-chip").addEventListener("click", () => {
    $(".library").scrollIntoView({ behavior: "smooth", block: "nearest" });
    const card = $(`.voice-card[data-id="${CSS.escape(state.voice?.id || "")}"]`);
    if (card) {
      card.scrollIntoView({ block: "nearest" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 900);
    }
  });

  $("#voices").addEventListener("click", (e) => {
    if (e.target.closest("[data-show-all]")) {
      state.search = "";
      $("#search").value = "";
      setFilter("all");
      return;
    }
    const preview = e.target.closest("[data-preview]");
    if (preview) {
      e.stopPropagation();
      previewVoice(preview.dataset.preview);
      return;
    }
    const card = e.target.closest(".voice-card");
    if (card) selectVoice(card.dataset.id);
  });
  $("#voices").addEventListener("keydown", (e) => {
    if (e.target.closest(".v-play")) return;
    const card = e.target.closest(".voice-card");
    if (card && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      selectVoice(card.dataset.id);
      $(`.voice-card[data-id="${CSS.escape(card.dataset.id)}"]`)?.focus(); // list re-rendered
    }
  });

  $("#filters").addEventListener("click", (e) => {
    const button = e.target.closest("[data-gender]");
    if (button) setFilter(button.dataset.gender);
  });
  $("#search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderVoices();
  });
  $("#library-extra").addEventListener("click", (e) => {
    if (e.target.closest("[data-guide-toggle]")) {
      state.guideOpen = !state.guideOpen;
      renderVoices();
      $("#library-extra [data-guide-toggle]")?.focus();
      return;
    }
    const button = e.target.closest("[data-switch-lang]");
    if (button) setLang(button.dataset.switchLang);
  });
  $("#history-toggle").addEventListener("click", () => {
    state.historyExpanded = !state.historyExpanded;
    renderHistory();
  });
  // Colour-filled slider tracks: keep --p in sync with every range input.
  $$('input[type="range"]').forEach((input) => {
    input.addEventListener("input", () => paintRange(input));
    paintRange(input);
  });

  $("#history").addEventListener("click", (e) => {
    const play = e.target.closest("[data-play]");
    if (play) {
      const item = state.history.find((x) => x.id === play.dataset.play);
      if (item) playHistory(item);
      return;
    }
    const del = e.target.closest("[data-del]");
    if (del) deleteHistory(del.dataset.del);
  });

  $("#clear-history").addEventListener("click", async () => {
    if (!state.history.length) { toast("Tiada generasi untuk dipadam."); return; }
    if (!confirm(`Padam semua ${state.history.length} generasi? Fail audio juga akan dipadam.`)) return;
    try {
      const response = await fetch("/api/history/clear", { method: "POST" });
      if (!response.ok) throw new Error(await errorMessage(response, "Clear failed"));
      state.history = [];
      resetPlayer();
      renderHistory();
      toast("Sejarah dibersihkan.");
    } catch (err) { toast("Gagal membersihkan sejarah. " + (err.message || ""), true); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    const target = e.target instanceof Element ? e.target : document.activeElement;
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!e.repeat) generate();
      return;
    }
    if (e.key === " " && !e.defaultPrevented && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Space toggles playback only when focus is not on something Space already activates.
      const interactive = target?.closest?.(
        "button, a[href], input, textarea, select, summary, [contenteditable='true'], [role='button'], [role='tab'], [tabindex]:not([tabindex='-1'])");
      if (!interactive && !$("#player").hidden) {
        e.preventDefault();
        togglePlay();
      }
      return;
    }
    if (e.key === "Escape") {
      if (state.previewing || !previewAudio.paused) stopPreview();
      if (!audio.paused) audio.pause();
    }
  });

  window.addEventListener("pagehide", () => {
    clearPreviewCache();
    if (state.current?.url?.startsWith("blob:")) URL.revokeObjectURL(state.current.url);
  });
}

function setFilter(gender) {
  state.gender = gender;
  $$("#filters button").forEach((b) => {
    const on = b.dataset.gender === gender;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  ensureSelectedVisible();
  renderVoices();
}

/* ----------------------------------------------------------------------- boot */
async function boot() {
  bind();
  $("#speed-value").textContent = "1.00×";
  $("#pitch-value").textContent = "0 Hz";
  $("#stability-value").textContent = "0.90";
  const savedVolume = Number(store.get("volume", $("#volume").value));
  if (isFinite(savedVolume)) $("#volume").value = String(Math.max(0, Math.min(1, savedVolume)));
  setMuted(store.get("muted", "0") === "1");
  paintRange($("#volume"));
  resetPlayer();
  await loadCatalog(true);
  await loadHistory();
  syncLibraryHeight();
  new ResizeObserver(() => syncLibraryHeight()).observe($(".workspace-left"));
  window.addEventListener("resize", () => {
    syncLibraryHeight();
    if (!state.historyExpanded && historyColumns() !== state.historyCols) renderHistory();
  });
}

function paintRange(input) {
  if (!input) return;
  const min = Number(input.min || 0), max = Number(input.max || 100);
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--p", `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`);
}

boot();
