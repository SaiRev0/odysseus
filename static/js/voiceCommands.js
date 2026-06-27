// static/js/voiceCommands.js
/**
 * Voice Command Engine
 *
 * Intercepts a transcribed utterance and checks it against a set of
 * intent patterns.  If a command matches it executes the action and
 * returns true so the caller knows NOT to insert the text into the chat
 * input.  Otherwise returns false and the caller inserts the text normally.
 *
 * Supported voice commands (case-insensitive, leading/trailing noise stripped):
 *
 *   SEND / Navigation
 *   ─────────────────
 *   "send"                        → submit the current chat input
 *   "send message"                → submit the current chat input
 *   "send it"                     → submit the current chat input
 *   "new chat"                    → start a new normal session
 *   "new session"                 → start a new normal session
 *   "new incognito chat"          → start a new incognito session
 *   "incognito mode"              → toggle incognito on
 *   "disable incognito"           → toggle incognito off
 *   "cancel"  / "stop"            → abort current streaming request
 *   "go to chat <name/number>"    → switch to a session by partial name or list index
 *   "open chat <name/number>"     → same as above
 *   "switch to chat <name>"       → same as above
 *
 *   TTS
 *   ───
 *   "read aloud"  / "read it"     → play TTS on the latest AI message
 *   "stop reading" / "stop audio" → stop TTS playback
 *
 * Usage:
 *   import voiceCommands from './voiceCommands.js';
 *   voiceCommands.init({ sessionModule, chatModule, uiModule });
 *   const handled = voiceCommands.dispatch(transcript);
 */

// ── Keyword-presence helpers ─────────────────────────────────────────────────
// These check whether the normalised text *contains* a command phrase rather
// than requiring an exact match.  This handles natural speech like
// "please send this message", "go ahead and send", "start a new chat", etc.

function _has(text, ...words) {
  return words.some(w => {
    const re = new RegExp('\\b' + w.replace(/\s+/g, '\\s+') + '\\b', 'i');
    return re.test(text);
  });
}

const _INTENTS = [
  // ── Send ─────────────────────────────────────────────────────────────────
  // Matches any utterance that contains a send-intent keyword and does NOT
  // look like a normal question/statement (i.e. is short enough to be a
  // command, or starts with a command word).
  {
    id: 'send',
    test: (t) => _has(t, 'send this', 'send it', 'send message', 'send the message',
                         'submit this', 'submit message', 'go ahead and send',
                         'please send', 'ok send', 'okay send') ||
                 /^\s*send\s*[.!]?\s*$/i.test(t),
    action: (_m, ctx) => {
      const input = document.getElementById('message');
      const text = input ? input.value.trim() : '';

      if (!text) {
        if (ctx.uiModule) ctx.uiModule.showToast('Nothing to send — type or dictate a message first');
        return;
      }

      // Ensure the send button is not in mic/recording mode before submitting
      const sendBtn = document.querySelector('.send-btn');
      if (sendBtn && (sendBtn.dataset.mode === 'recording' || sendBtn.dataset.mode === 'mic')) {
        sendBtn.dataset.mode = 'send';
        sendBtn.classList.remove('recording', 'mic-mode');
      }

      // Pass a mock event — handleChatSubmit calls e.preventDefault() unconditionally
      const mockEvent = { preventDefault: () => {} };
      if (ctx.handleSubmit) {
        ctx.handleSubmit(mockEvent);
      } else {
        const form = document.getElementById('chat-form');
        if (form) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
      if (ctx.uiModule) ctx.uiModule.showToast('Sending…');
    },
  },

  // ── New incognito chat (must come before generic "new chat") ──────────────
  {
    id: 'new_incognito',
    test: (t) => _has(t, 'new incognito', 'new private chat', 'new secret chat',
                         'start incognito', 'open incognito', 'incognito chat',
                         'incognito session', 'incognito mode'),
    action: (_m, ctx) => {
      // Enable the incognito toggle then create a new chat
      const chk = document.getElementById('incognito-toggle');
      const btn = document.getElementById('incognito-btn');
      if (chk && !chk.checked) {
        chk.checked = true;
        chk.dispatchEvent(new Event('change', { bubbles: true }));
      }
      if (btn) btn.click();
      // Start a new session via the rail button
      const railNew = document.getElementById('rail-new-session');
      if (railNew) railNew.click();
      if (ctx.uiModule) ctx.uiModule.showToast('New incognito chat');
    },
  },

  // ── Toggle incognito on ───────────────────────────────────────────────────
  {
    id: 'incognito_on',
    test: (t) => _has(t, 'enable incognito', 'activate incognito', 'turn on incognito',
                         'switch to incognito'),
    action: (_m, ctx) => {
      const chk = document.getElementById('incognito-toggle');
      if (chk && !chk.checked) {
        chk.checked = true;
        chk.dispatchEvent(new Event('change', { bubbles: true }));
      }
      const btn = document.getElementById('incognito-btn');
      if (btn) btn.click();
      if (ctx.uiModule) ctx.uiModule.showToast('Incognito mode on');
    },
  },

  // ── Toggle incognito off ──────────────────────────────────────────────────
  {
    id: 'incognito_off',
    test: (t) => _has(t, 'disable incognito', 'deactivate incognito', 'turn off incognito',
                         'exit incognito', 'leave incognito'),
    action: (_m, ctx) => {
      const chk = document.getElementById('incognito-toggle');
      if (chk && chk.checked) {
        chk.checked = false;
        chk.dispatchEvent(new Event('change', { bubbles: true }));
      }
      const btn = document.getElementById('incognito-btn');
      if (btn && btn.classList.contains('active')) btn.click();
      if (ctx.uiModule) ctx.uiModule.showToast('Incognito mode off');
    },
  },

  // ── New normal chat ───────────────────────────────────────────────────────
  {
    id: 'new_chat',
    test: (t) => _has(t, 'new chat', 'new session', 'new conversation',
                         'start a new chat', 'start a new session',
                         'open a new chat', 'create a new chat',
                         'fresh chat', 'blank chat'),
    action: (_m, ctx) => {
      const railNew = document.getElementById('rail-new-session');
      if (railNew) railNew.click();
      if (ctx.uiModule) ctx.uiModule.showToast('New chat');
    },
  },

  // ── Go to / switch to chat ────────────────────────────────────────────────
  {
    id: 'switch_chat',
    test: (t) => /\b(go to|open|switch to|navigate to|load)\s+(chat|session)\s+\S/i.test(t),
    action: (_m, ctx, rawText) => {
      if (!ctx.sessionModule) return;
      // Extract the target after the command verb
      const m = rawText.match(/\b(?:go to|open|switch to|navigate to|load)\s+(?:chat|session)\s+(.+)/i);
      const query = m ? m[1].trim().toLowerCase().replace(/[.,!?]+$/, '') : '';
      if (!query) return;

      const sessions = ctx.sessionModule.getSessions().filter(s => !s.archived);

      // Try numeric index first (1-based, matching sidebar order)
      const num = parseInt(query, 10);
      if (!isNaN(num) && num >= 1 && num <= sessions.length) {
        ctx.sessionModule.selectSession(sessions[num - 1].id);
        if (ctx.uiModule) ctx.uiModule.showToast(`Switched to chat ${num}`);
        return;
      }

      // Fuzzy name match
      const hit = sessions.find(s => (s.name || '').toLowerCase().includes(query));
      if (hit) {
        ctx.sessionModule.selectSession(hit.id);
        if (ctx.uiModule) ctx.uiModule.showToast(`Switched to "${hit.name}"`);
      } else {
        if (ctx.uiModule) ctx.uiModule.showToast(`No chat found matching "${query}"`);
      }
    },
  },

  // ── Cancel / abort streaming ──────────────────────────────────────────────
  {
    id: 'cancel',
    test: (t) => /^\s*(cancel|abort|stop generating|stop the response)\s*[.!]?\s*$/i.test(t),
    action: (_m, ctx) => {
      if (ctx.chatModule && ctx.chatModule.abortCurrentRequest) {
        ctx.chatModule.abortCurrentRequest();
      }
      if (ctx.uiModule) ctx.uiModule.showToast('Cancelled');
    },
  },

  // ── TTS: read aloud ───────────────────────────────────────────────────────
  {
    id: 'tts_play',
    test: (t) => _has(t, 'read aloud', 'read it', 'read that', 'read the response',
                         'read the message', 'play audio', 'play tts',
                         'speak it', 'speak that', 'read last message'),
    action: (_m, ctx) => {
      const mgr = window.aiTTSManager;
      if (!mgr || !mgr.available) {
        if (ctx.uiModule) ctx.uiModule.showToast('TTS not available');
        return;
      }
      const allAI = document.querySelectorAll('#chat-history .msg-ai');
      for (let i = allAI.length - 1; i >= 0; i--) {
        const btn = allAI[i].querySelector('.ai-tts-button');
        if (btn) { btn.click(); return; }
      }
    },
  },

  // ── TTS: stop ─────────────────────────────────────────────────────────────
  {
    id: 'tts_stop',
    test: (t) => _has(t, 'stop reading', 'stop audio', 'stop tts', 'stop playback') ||
                 /^\s*(mute|silence|quiet)\s*[.!]?\s*$/i.test(t),
    action: (_m, _ctx) => {
      const mgr = window.aiTTSManager;
      if (mgr) mgr.stop();
    },
  },
];

// ── Module state ─────────────────────────────────────────────────────────────

let _ctx = {};

/**
 * Initialise with references to app modules.
 * Call once from app.js after all modules are ready.
 */
function init({ sessionModule, chatModule, uiModule, handleSubmit } = {}) {
  _ctx = { sessionModule, chatModule, uiModule, handleSubmit };
}

/**
 * Try to match `transcript` against all known command intents.
 * Returns true if a command was matched and executed.
 * Returns false if no command matched (caller inserts text normally).
 */
function dispatch(transcript) {
  if (!transcript) return false;

  // Normalise: collapse whitespace, strip leading filler
  const text = transcript
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/^(um+|uh+|hey|hi|okay|ok|please|alright|right|so)\s+/i, '')
    .replace(/[.,!?]+$/, '')
    .trim();

  for (const intent of _INTENTS) {
    let matched = false;
    if (typeof intent.test === 'function') {
      matched = intent.test(text);
    } else if (Array.isArray(intent.patterns)) {
      matched = intent.patterns.some(p => p.test(text));
    }

    if (matched) {
      try {
        intent.action(null, _ctx, text);
      } catch (e) {
        console.error(`[voiceCommands] action "${intent.id}" threw:`, e);
      }
      return true;
    }
  }

  return false;
}

const voiceCommandsModule = { init, dispatch };

// Expose globally so it can be tested from the browser console:
// window.voiceCommands.dispatch("send this message")
window.voiceCommands = voiceCommandsModule;

export default voiceCommandsModule;
export { init, dispatch };
