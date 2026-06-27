// static/js/tts-ai.js
// AI Text-to-Speech Module — supports server TTS and browser Web Speech API

class AITTSManager {
    constructor() {
        this.currentAudio = null;
        this.isPlaying = false;
        this.available = false;
        this.useBrowserTTS = false;
        this.browserVoice = '';
        this.playbackSpeed = 1;
        this._provider = 'disabled';
        this.autoPlay = false;
        this.cache = new Map(); // Client-side audio cache

        // Queue for sequential auto-play
        this._queue = [];       // Array of { text, button, resetFn, audioPromise? }
        this._processing = false;

        // Streaming sentence-by-sentence TTS state
        this._streamSentencesSent = 0;  // chars of plain text already queued
        this._streamEnqueuedText = '';  // all plain-text sentences enqueued so far (this stream)
        this._streamActive = false;
        this._streamButton = null;
        this._streamResetFn = null;
        this._streamDebounceTimer = null;

        // Check if TTS service is available
        this.checkAvailability();
    }

    async checkAvailability() {
        try {
            // Check user setting first — if TTS is disabled in settings, don't show buttons
            try {
                const settingsRes = await fetch('/api/auth/settings', { credentials: 'same-origin' });
                const settings = await settingsRes.json();
                if (settings.tts_enabled === false) {
                    this.available = false;
                    this._provider = 'disabled';
                    return;
                }
                // Load auto-play preference
                this.autoPlay = settings.tts_auto_play === true;
            } catch {}

            const response = await fetch('/api/tts/stats');
            const stats = await response.json();
            this.available = stats.available && stats.ready;
            this.playbackSpeed = stats.speed || 1;
            this._provider = stats.provider || 'disabled';

            if (stats.provider === 'browser') {
                this.useBrowserTTS = true;
                this.browserVoice = stats.voice || '';
                this.available = 'speechSynthesis' in window;
                if (!this.available) {
                    console.warn('TTS: browser mode selected but speechSynthesis not supported');
                }
            } else if (this.available) {
                this.useBrowserTTS = false;
            } else {
                console.warn('TTS: not available');
            }
        } catch (error) {
            console.error('Failed to check TTS availability:', error);
            this.available = false;
        }
    }

    extractPlainText(content) {
        // Strip <think>/<thinking> blocks (model reasoning)
        let cleaned = content.replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '');

        // Create a temporary div to parse HTML/markdown
        const temp = document.createElement('div');
        temp.innerHTML = cleaned;

        // Remove code blocks
        temp.querySelectorAll('pre, code').forEach(el => el.remove());

        // Get text content
        let text = temp.textContent || temp.innerText || '';

        // Clean up markdown syntax
        text = text
            .replace(/#{1,6}\s/g, '') // Remove headers
            .replace(/\*\*(.+?)\*\*/g, '$1') // Remove bold
            .replace(/\*(.+?)\*/g, '$1') // Remove italic
            .replace(/\[(.+?)\]\(.+?\)/g, '$1') // Remove links
            .replace(/`(.+?)`/g, '$1') // Remove inline code
            .replace(/\n{3,}/g, '\n\n') // Normalize line breaks
            .trim();

        return text;
    }

    getCacheKey(text) {
        // Simple hash function for cache key
        let hash = 0;
        for (let i = 0; i < text.length; i++) {
            const char = text.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash;
        }
        return hash.toString(36);
    }

    async synthesize(text, onProgress = null) {
        if (!this.available) {
            throw new Error('AI TTS service not available');
        }

        const plainText = this.extractPlainText(text);

        if (!plainText) {
            throw new Error('No text to synthesize');
        }

        // Browser TTS doesn't use synthesize — handled directly in play()
        if (this.useBrowserTTS) {
            return '__browser_tts__';
        }

        const cacheKey = this.getCacheKey(plainText);

        // Check cache first
        if (this.cache.has(cacheKey)) {
            return this.cache.get(cacheKey);
        }

        try {
            if (onProgress) onProgress('synthesizing');

            const response = await fetch('/api/tts/synthesize', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    text: plainText,
                    format: 'audio'
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail?.message || 'Synthesis failed');
            }

            const audioBlob = await response.blob();
            const audioUrl = URL.createObjectURL(audioBlob);

            // Cache the result
            this.cache.set(cacheKey, audioUrl);

            if (onProgress) onProgress('complete');

            return audioUrl;

        } catch (error) {
            if (onProgress) onProgress('error');
            throw error;
        }
    }

    _findBrowserVoice() {
        if (!this.browserVoice) return null;
        const voices = window.speechSynthesis.getVoices();
        const target = this.browserVoice.toLowerCase();
        // Try exact match first, then partial
        return voices.find(v => v.name.toLowerCase() === target) ||
               voices.find(v => v.name.toLowerCase().includes(target)) ||
               null;
    }

    async play(text) {
        // Stop current audio if playing
        this.stop();

        const plainText = this.extractPlainText(text);
        if (!plainText) return;

        if (this.useBrowserTTS) {
            return this._playBrowser(plainText);
        }

        try {
            const audioUrl = await this.synthesize(text);

            this.currentAudio = new Audio(audioUrl);
            await this.currentAudio.play();
            this.isPlaying = true;
            // Note: onended should be set by the caller (addAITTSButton)
            // to reset button state when audio finishes

        } catch (error) {
            console.error('Failed to play audio:', error);
            throw error;
        }
    }

    _playBrowser(plainText) {
        return new Promise((resolve, reject) => {
            const utterance = new SpeechSynthesisUtterance(plainText);
            const voice = this._findBrowserVoice();
            if (voice) utterance.voice = voice;
            utterance.rate = this.playbackSpeed;

            utterance.onend = () => {
                this.isPlaying = false;
                resolve();
            };
            utterance.onerror = (e) => {
                this.isPlaying = false;
                reject(new Error('Browser TTS error: ' + e.error));
            };

            window.speechSynthesis.speak(utterance);
            this.isPlaying = true;
        });
    }

    stop() {
        // Cancel streaming TTS
        this._streamActive = false;
        if (this._streamDebounceTimer) {
            clearTimeout(this._streamDebounceTimer);
            this._streamDebounceTimer = null;
        }
        this._streamSentencesSent = 0;
        this._streamEnqueuedText = '';

        // Clear the entire queue and reset all queued buttons
        for (const item of this._queue) {
            if (item.resetFn) item.resetFn();
        }
        this._queue = [];
        this._processing = false;

        if (this.useBrowserTTS) {
            window.speechSynthesis.cancel();
            this.isPlaying = false;
        }
        if (this.currentAudio) {
            this.currentAudio.pause();
            this.currentAudio.currentTime = 0;
            this.currentAudio = null;
            this.isPlaying = false;
        }
    }

    /**
     * Enqueue a message for auto-play. Plays sequentially — each message
     * finishes before the next starts. Stopping any message clears the queue.
     *
     * Synthesis is kicked off immediately on enqueue (not when the queue
     * runner reaches this item) so the audio is already arriving while the
     * previous sentence is still playing — eliminating the 1-2s gap between
     * sentences caused by waiting for synthesis at play time.
     */
    enqueue(text, button, resetFn) {
        // Pre-warm: start synthesis right now, store the promise so _playQueueItem
        // can await it directly instead of re-issuing a fresh fetch.
        var audioPromise = null;
        if (!this.useBrowserTTS && this.available) {
            audioPromise = this.synthesize(text).catch(function() { return null; });
        }
        this._queue.push({ text, button, resetFn, audioPromise });
        if (!this._processing) {
            this._processQueue();
        }
    }

    async _processQueue() {
        if (this._processing) return;
        this._processing = true;

        while (this._queue.length > 0) {
            const item = this._queue[0];
            try {
                await this._playQueueItem(item);
            } catch (err) {
                console.error('TTS queue item error:', err);
            }
            if (this._queue.length > 0 && this._queue[0] === item) {
                this._queue.shift();
            }
            if (!this._processing) return;
        }

        this._processing = false;
    }

    async _playQueueItem(item) {
        const { text, button, resetFn } = item;
        const ICON_LOADING = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9" stroke-dasharray="42" stroke-dashoffset="12" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg>';
        var ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';

        button.innerHTML = ICON_LOADING;
        button.classList.add('loading');
        button.style.color = '#ccc';
        button.title = 'Loading...';

        try {
            if (!this._processing) return;

            // Use the pre-warmed promise if available; otherwise fall back to a
            // fresh synthesize() call (e.g. items added via non-streaming path).
            const audioUrl = await (item.audioPromise
                ? item.audioPromise
                : this.synthesize(text));

            if (!this._processing) return;

            button.innerHTML = ICON_STOP;
            button.classList.remove('loading');
            button.classList.add('playing');
            button.title = 'Stop';

            if (this.useBrowserTTS) {
                const plainText = this.extractPlainText(text);
                await this._playBrowser(plainText);
            } else {
                if (this.currentAudio) {
                    this.currentAudio.pause();
                    this.currentAudio = null;
                }

                await new Promise((resolve, reject) => {
                    const audio = new Audio(audioUrl);
                    if (this._provider === 'local' && this.playbackSpeed !== 1) {
                        audio.playbackRate = this.playbackSpeed;
                    }
                    this.currentAudio = audio;
                    audio.onended = () => {
                        this.isPlaying = false;
                        if (this.currentAudio === audio) this.currentAudio = null;
                        // Small inter-sentence gap so sentences sound distinct
                        // rather than running together with no perceptible cut.
                        setTimeout(resolve, 60);
                    };
                    audio.onerror = (e) => {
                        this.isPlaying = false;
                        if (this.currentAudio === audio) this.currentAudio = null;
                        reject(new Error('Audio playback error'));
                    };
                    audio.onpause = () => {
                        if (this.currentAudio !== audio) {
                            resolve();
                        }
                    };
                    audio.play().then(() => {
                        this.isPlaying = true;
                    }).catch(reject);
                });
            }
        } finally {
            if (resetFn) resetFn();
        }
    }

    // ── Streaming TTS (sentence-by-sentence) ──

    streamingStart() {
        this._streamSentencesSent = 0;
        this._streamActive = true;
        this._streamButton = null;
        this._streamResetFn = null;
        // Track all plain-text already enqueued as a single concatenated string
        // so streamingEnd can find the true remainder via string search rather
        // than brittle char-offset arithmetic against re-extracted plain text.
        this._streamEnqueuedText = '';
    }

    streamingUpdate(accumulatedText) {
        if (!this._streamActive || !this.available || !this.autoPlay) return;
        // Fire immediately when a sentence-ending punctuation character appears —
        // we no longer require trailing whitespace so the first sentence dispatches
        // as soon as its terminal character arrives rather than waiting for the
        // first token of the next sentence.
        var newRegionRaw = this.extractPlainText(
            accumulatedText.replace(/```[\s\S]*?```/g, '').replace(/```[\s\S]*$/g, '')
        ).substring(this._streamSentencesSent);
        var hasSentenceBoundary = /[.!?]/.test(newRegionRaw);
        if (hasSentenceBoundary) {
            if (this._streamDebounceTimer) {
                clearTimeout(this._streamDebounceTimer);
                this._streamDebounceTimer = null;
            }
            this._processStreamingSentences(accumulatedText);
            return;
        }
        // No sentence boundary yet — trailing debounce for the final fragment.
        if (this._streamDebounceTimer) clearTimeout(this._streamDebounceTimer);
        this._streamDebounceTimer = setTimeout(() => {
            this._streamDebounceTimer = null;
            this._processStreamingSentences(accumulatedText);
        }, 80);
    }

    _processStreamingSentences(accumulatedText) {
        if (!this._streamActive) return;

        var text = accumulatedText
            .replace(/```[\s\S]*?```/g, '')
            .replace(/```[\s\S]*$/g, '');

        var plainText = this.extractPlainText(text);
        if (!plainText || plainText.length <= this._streamSentencesSent) return;

        var newRegion = plainText.substring(this._streamSentencesSent);

        // Parse sentences and track exact char positions so the cursor advances
        // by the real number of consumed characters — not a hardcoded +1 that
        // can skip the first word of the next sentence when there's no space.
        var sentences = [];   // { text, consumed } — consumed = chars used from newRegion
        var current = '';
        var consumed = 0;     // running char count through newRegion
        for (var i = 0; i < newRegion.length; i++) {
            current += newRegion[i];
            var ch = newRegion[i];
            var next = newRegion[i + 1];
            // Boundary: punctuation followed by whitespace OR at end of region
            var atBoundary = (ch === '.' || ch === '!' || ch === '?') &&
                (!next || /\s/.test(next));
            if (atBoundary) {
                var lastWord = current.trim().split(/\s/).pop() || '';
                if (/^\d+\.$/.test(lastWord)) continue;
                if (/^[A-Z][a-z]?\.$/.test(lastWord)) continue;
                // Advance consumed past the sentence AND ALL trailing whitespace
                // (there may be \n\n or other multi-char separators — skip them all
                // so the cursor never drifts and silently drops leading words).
                var sentEnd = i + 1;
                while (sentEnd < newRegion.length && /\s/.test(newRegion[sentEnd])) {
                    sentEnd++;
                }
                sentences.push({ text: current.trim(), consumed: sentEnd });
                consumed = sentEnd;
                current = '';
            }
        }

        if (sentences.length === 0) return;

        var totalConsumed = 0;
        for (var j = 0; j < sentences.length; j++) {
            var s = sentences[j];
            totalConsumed = s.consumed;
            // Only enqueue sentences long enough to be worth synthesizing
            if (s.text.length < 8) continue;
            var btn = this._streamButton || this._createPlaceholderButton();
            var resetFn = this._streamResetFn || function() {};
            this.enqueue(s.text, btn, resetFn);
            this._streamEnqueuedText += (this._streamEnqueuedText ? ' ' : '') + s.text;
            // enqueue() now kicks off synthesis immediately for every item,
            // so no separate pre-warm loop is needed here.
        }

        this._streamSentencesSent += totalConsumed;
    }

    _createPlaceholderButton() {
        var btn = document.createElement('button');
        btn.style.display = 'none';
        btn.className = 'ai-tts-button streaming-placeholder';
        return btn;
    }

    streamingAttachButton(button, resetFn) {
        this._streamButton = button;
        this._streamResetFn = resetFn;
        for (var i = 0; i < this._queue.length; i++) {
            if (this._queue[i].button && this._queue[i].button.classList.contains('streaming-placeholder')) {
                this._queue[i].button = button;
                this._queue[i].resetFn = resetFn;
            }
        }
    }

    streamingEnd(finalText) {
        if (!this._streamActive) return;
        this._streamActive = false;
        if (this._streamDebounceTimer) {
            clearTimeout(this._streamDebounceTimer);
            this._streamDebounceTimer = null;
        }

        var text = finalText
            .replace(/```[\s\S]*?```/g, '')
            .replace(/```[\s\S]*$/g, '');

        var plainText = this.extractPlainText(text);
        if (!plainText) return;

        // Find the remainder by locating where enqueued content ends in the final
        // plain text — avoids cursor drift from partial-vs-complete markdown extraction.
        var remaining;
        if (this._streamEnqueuedText && plainText.includes(this._streamEnqueuedText)) {
            var afterIdx = plainText.indexOf(this._streamEnqueuedText) + this._streamEnqueuedText.length;
            remaining = plainText.substring(afterIdx).trim();
        } else {
            // Fallback: use char-offset (handles case where nothing was enqueued yet)
            remaining = plainText.substring(this._streamSentencesSent).trim();
        }

        // Guard: if the remaining text is already queued (exact match on the last
        // queued item's text), don't re-enqueue — prevents the same fragment being
        // synthesized twice causing a "voice switch" in prosody.
        if (remaining.length >= 8) {
            var lastQueued = this._queue.length > 0
                ? this._queue[this._queue.length - 1].text
                : null;
            if (lastQueued !== remaining) {
                // enqueue() fires synthesis immediately, so no separate pre-warm needed.
                var btn = this._streamButton || this._createPlaceholderButton();
                var resetFn = this._streamResetFn || function() {};
                this.enqueue(remaining, btn, resetFn);
            }
        }
        this._streamSentencesSent = 0;
        this._streamEnqueuedText = '';
    }

    clearCache() {
        for (const url of this.cache.values()) {
            URL.revokeObjectURL(url);
        }
        this.cache.clear();
    }
}

// Create global AI TTS manager instance
window.aiTTSManager = new AITTSManager();

// Function to add AI TTS button to a message element's action bar
export function addAITTSButton(messageElement, text) {
    if (!window.aiTTSManager.available || window.aiTTSManager._provider === 'disabled') {
        return;
    }

    if (messageElement.querySelector('.ai-tts-button')) {
        return;
    }

    // Find the msg-actions container in the footer
    const actions = messageElement.querySelector('.msg-actions');
    if (!actions) return;

    var ICON_PLAY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
    var ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';
    var ICON_LOADING = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9" stroke-dasharray="42" stroke-dashoffset="12" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg>';

    const playButton = document.createElement('button');
    playButton.className = 'ai-tts-button';
    playButton.type = 'button';
    playButton.title = 'Read aloud';
    playButton.innerHTML = ICON_PLAY;
    playButton.style.cssText = 'background:none;border:none;color:#6b7280;cursor:pointer;padding:2px 6px;border-radius:4px;transition:color .15s;line-height:1;display:inline-flex;align-items:center;';

    playButton.addEventListener('mouseenter', () => { playButton.style.color = '#ccc'; });
    playButton.addEventListener('mouseleave', () => {
        if (!playButton.classList.contains('playing') && !playButton.classList.contains('loading')) playButton.style.color = '#6b7280';
    });

    function resetButton() {
        playButton.innerHTML = ICON_PLAY;
        playButton.classList.remove('playing', 'loading');
        playButton.style.color = '#6b7280';
        playButton.title = 'Read aloud';
    }

    playButton.addEventListener('click', async (e) => {
        e.stopPropagation();
        const mgr = window.aiTTSManager;

        if (mgr.isPlaying || mgr._processing) {
            mgr.stop();
            resetButton();
            return;
        }

        mgr.enqueue(text, playButton, resetButton);
    });

    actions.appendChild(playButton);
}

// Stop audio when navigating away
window.addEventListener('beforeunload', () => {
    if (window.aiTTSManager) {
        window.aiTTSManager.stop();
    }
});

export { AITTSManager };

const ttsModule = { AITTSManager, addAITTSButton };
export default ttsModule;
