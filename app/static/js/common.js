/* 通用工具：API 封装、WebSocket 自动重连、倒计时、DOM 辅助、本地凭证存储。 */
'use strict';

const App = (() => {
  const LS_HOST = (rid) => `fdr:host:${rid}`;
  const LS_MEMBER = (rid) => `fdr:member:${rid}`;
  const LS_SECRET = (rid, idx) => `fdr:secret:${rid}:${idx}`;

  // ---- 时间：服务器返回 UTC ISO（Z 结尾）；表单用浏览器本地时间 ----
  function localToIso(localValue) {
    // <input type="datetime-local"> value: "YYYY-MM-DDTHH:mm"
    const d = new Date(localValue);
    if (isNaN(d.getTime())) throw new Error('时间格式无效');
    return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
  }

  function isoToLocalInput(iso) {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleString();
  }

  function fmtCountdown(ms) {
    if (ms <= 0) return '已超时';
    const total = Math.floor(ms / 1000);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function deadlineIn(iso, serverNowIso) {
    const now = serverNowIso
      ? new Date(serverNowIso).getTime() + (Date.now() - App._serverClockAt)
      : Date.now();
    return new Date(iso).getTime() - now;
  }

  // ---- HTTP API ----
  async function api(method, path, body, headers = {}) {
    const opts = { method, headers: { ...headers } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch { /* 非 JSON */ }
    if (!res.ok) {
      const err = new Error(data?.error?.message || `请求失败 (${res.status})`);
      err.code = data?.error?.code || 'http_error';
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const authHeaders = (hostToken, sessionToken) => {
    const h = {};
    if (hostToken) h['X-Host-Token'] = hostToken;
    if (sessionToken) h['X-Session-Token'] = sessionToken;
    return h;
  };

  // ---- WebSocket：自动重连 + 快照驱动重绘 ----
  class RoomSocket {
    constructor(roundId, hostToken, sessionToken, onSnapshot, onStatus) {
      this.roundId = roundId;
      this.hostToken = hostToken;
      this.sessionToken = sessionToken;
      this.onSnapshot = onSnapshot;
      this.onStatus = onStatus || (() => {});
      this.ws = null;
      this.closed = false;
      this.retry = 0;
    }

    _url() {
      const p = new URLSearchParams({ round_id: this.roundId });
      if (this.hostToken) p.set('host_token', this.hostToken);
      if (this.sessionToken) p.set('session_token', this.sessionToken);
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      return `${proto}://${location.host}/api/ws/${encodeURIComponent(this.roundId)}?${p}`;
    }

    connect() {
      this.closed = false;
      this.onStatus('connecting');
      this.ws = new WebSocket(this._url());
      this.ws.onopen = () => { this.retry = 0; this.onStatus('live'); };
      this.ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === 'snapshot' && msg.snapshot) {
          this.onSnapshot(msg.snapshot);
        }
      };
      this.ws.onclose = () => {
        if (this.closed) return;
        this.onStatus('reconnecting');
        this.retry = Math.min(this.retry + 1, 6);
        setTimeout(() => this.connect(), 400 * this.retry);
      };
      this.ws.onerror = () => { try { this.ws.close(); } catch {} };
    }

    close() { this.closed = true; try { this.ws.close(); } catch {} }
  }

  function noteServerTime(iso) {
    App._serverClockAt = Date.now();
    App._serverTime = iso;
  }

  return {
    LS_HOST, LS_MEMBER, LS_SECRET,
    localToIso, isoToLocalInput, fmtTime, fmtCountdown, esc, deadlineIn,
    api, authHeaders, RoomSocket, noteServerTime,
    _serverClockAt: Date.now(),
    _serverTime: null,
  };
})();
