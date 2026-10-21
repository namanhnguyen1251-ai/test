/* Page batch console. It calls only Meta Graph API and never stores an access token. */
"use strict";

const PAGE_SIZE = 100;
const MAX_RETRIES = 3;
const GRAPH_ORIGIN = "https://graph.facebook.com";

function detectDelimiter(input) {
  let quoted = false, commas = 0, semicolons = 0;
  for (let i = 0; i < input.length; i++) {
    const ch = input[i];
    if (ch === '"') {
      if (quoted && input[i + 1] === '"') i++;
      else quoted = !quoted;
    } else if (!quoted && (ch === '\n' || ch === '\r')) break;
    else if (!quoted && ch === ',') commas++;
    else if (!quoted && ch === ';') semicolons++;
  }
  return semicolons > commas ? ';' : ',';
}

function parseCsv(input) {
  if (typeof input !== 'string') throw new Error('CSV phải là văn bản.');
  const text = input.replace(/^\uFEFF/, '');
  const delimiter = detectDelimiter(text);
  const rows = [];
  let row = [], cell = '', quoted = false, line = 1, startLine = 1;
  function finishRow() {
    row.push(cell);
    if (row.some(value => value.trim() !== '')) rows.push({ line: startLine, cells: row });
    row = [];
    cell = '';
    startLine = line + 1;
  }
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else if (ch === '\r' && text[i + 1] === '\n') { cell += '\n'; i++; line++; }
      else { cell += ch; if (ch === '\n' || ch === '\r') line++; }
    } else if (ch === '"') {
      if (cell.trim() !== '') throw new Error(`Dấu ngoặc kép không hợp lệ ở dòng ${line}.`);
      cell = '';
      quoted = true;
    } else if (ch === delimiter) {
      row.push(cell); cell = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      finishRow(); line++;
    } else cell += ch;
  }
  if (quoted) throw new Error(`CSV thiếu dấu ngoặc kép đóng ở dòng ${startLine}.`);
  if (cell !== '' || row.length) finishRow();
  return rows;
}

function parseJobs(text) {
  const rows = parseCsv(text);
  if (!rows.length) throw new Error('CSV trống.');
  const header = rows[0].cells.map(value => value.trim().toLowerCase());
  const nameAt = header.indexOf('name');
  const categoryAt = header.indexOf('category_enum');
  const aboutAt = header.indexOf('about');
  if (nameAt < 0 || categoryAt < 0) throw new Error('CSV cần cột name và category_enum.');
  if (new Set(header).size !== header.length) throw new Error('CSV có tên cột bị trùng.');
  const jobs = [], errors = [], seen = new Set();
  for (const item of rows.slice(1)) {
    const values = item.cells;
    if (values.length > header.length && values.slice(header.length).some(v => v.trim())) {
      errors.push(`Dòng ${item.line}: thừa cột; kiểm tra dấu phẩy và dấu ngoặc kép.`);
      continue;
    }
    const name = (values[nameAt] || '').trim();
    const category = (values[categoryAt] || '').trim();
    const about = aboutAt >= 0 ? (values[aboutAt] || '').trim() : '';
    if (!name || name.length > 255) errors.push(`Dòng ${item.line}: name phải có 1–255 ký tự.`);
    else if (!/^[A-Za-z][A-Za-z0-9_]{0,99}$/.test(category)) errors.push(`Dòng ${item.line}: category_enum phải là mã api_enum.`);
    else {
      const key = name.toLocaleLowerCase();
      if (seen.has(key)) errors.push(`Dòng ${item.line}: Page bị trùng trong CSV.`);
      else {
        seen.add(key);
        jobs.push({ line: item.line, name, category, about, status: 'pending', pageId: '', detail: '' });
      }
    }
    if (errors.length >= 10) break;
  }
  if (errors.length) throw new Error(errors.join(' '));
  if (!jobs.length) throw new Error('Không có Page hợp lệ trong CSV.');
  return jobs;
}

function normalizeVersion(value) {
  const version = String(value || '').trim();
  if (!/^v?\d+\.\d+$/.test(version)) throw new Error('Graph API version phải có dạng v26.0.');
  return version.startsWith('v') ? version : `v${version}`;
}

function makePageParams(job) {
  const params = new URLSearchParams();
  params.set('name', job.name);
  params.set('category_enum', job.category);
  if (job.about) params.set('about', job.about);
  return params;
}

function parseRetryAfter(value, now = Date.now()) {
  if (!value) return 0;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - now) : 0;
}

function usageDelay(headers) {
  let highest = 0, regainMinutes = 0;
  function inspect(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, entry] of Object.entries(value)) {
      if (entry && typeof entry === 'object') inspect(entry);
      else if (['call_count', 'total_time', 'total_cputime'].includes(key)) highest = Math.max(highest, Number(entry) || 0);
      else if (key === 'estimated_time_to_regain_access') regainMinutes = Math.max(regainMinutes, Number(entry) || 0);
    }
  }
  for (const name of ['x-app-usage', 'x-business-use-case-usage']) {
    try { inspect(JSON.parse(headers.get(name) || 'null')); } catch (_) { /* absent or malformed */ }
  }
  return Math.max(regainMinutes * 60000, highest >= 95 ? 300000 : highest >= 85 ? 60000 : 0);
}

function classifyGraphError(payload, status) {
  const source = payload && typeof payload.error === 'object' ? payload.error : {};
  const code = Number(source.code) || 0;
  const fatal = [10, 100, 102, 190, 200, 368, 80004].includes(code);
  const retryable = status === 429 || [4, 17, 32, 613, 80001].includes(code);
  return { code, fatal, retryable, message: String(source.message || `Meta trả HTTP ${status}.`) };
}

function csvCell(value) {
  let text = String(value == null ? '' : value);
  if (/^\s*[=+\-@]/.test(text) || /^[\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

function exportRows(jobs) {
  const header = ['row', 'name', 'category_enum', 'about', 'status', 'page_id', 'detail'];
  const lines = [header.map(csvCell).join(',')];
  for (const job of jobs) lines.push([job.line, job.name, job.category, job.about, job.status, job.pageId, job.detail].map(csvCell).join(','));
  return '\uFEFF' + lines.join('\r\n') + '\r\n';
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { parseCsv, parseJobs, normalizeVersion, makePageParams, parseRetryAfter, usageDelay, classifyGraphError, exportRows };
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const el = {
    csvFile: $('csvFile'), csvText: $('csvText'), loadBtn: $('loadBtn'),
    token: $('token'), version: $('version'), delay: $('delay'),
    startBtn: $('startBtn'), pauseBtn: $('pauseBtn'), cancelBtn: $('cancelBtn'), exportBtn: $('exportBtn'),
    message: $('message'), summary: $('summary'), resultsBody: $('resultsBody'),
    prevBtn: $('prevBtn'), nextBtn: $('nextBtn'), pageLabel: $('pageLabel'),
  };
  const state = { jobs: [], page: 0, running: false, paused: false, cancelled: false, activeController: null, inPost: false, resume: null, checkpointError: false };
  const CHECKPOINT_SOURCE = 'xmeta-page-batch-source-v1';
  const CHECKPOINT_STATUS_PREFIX = 'xmeta-page-batch-status-v1-';
  const statusNames = { pending: 'Chờ', sending: 'Đang gửi', done: 'Đã tạo', failed: 'Lỗi', unknown: 'Chưa rõ' };

  function clearCheckpoint() {
    try {
      localStorage.removeItem(CHECKPOINT_SOURCE);
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (key && key.startsWith(CHECKPOINT_STATUS_PREFIX)) localStorage.removeItem(key);
      }
      state.checkpointError = false;
    } catch (_) { state.checkpointError = true; }
  }
  function checkpointSource() {
    clearCheckpoint();
    try {
      const source = state.jobs.map(({ line, name, category, about }) => ({ line, name, category, about }));
      localStorage.setItem(CHECKPOINT_SOURCE, JSON.stringify(source));
    } catch (_) { state.checkpointError = true; }
  }
  function checkpointStatus(job) {
    if (state.checkpointError) return;
    try {
      localStorage.setItem(`${CHECKPOINT_STATUS_PREFIX}${job.index}`, JSON.stringify({ status: job.status, pageId: job.pageId, detail: job.detail, notBefore: job.notBefore || 0 }));
    } catch (_) { state.checkpointError = true; }
  }
  function restoreCheckpoint() {
    try {
      const raw = localStorage.getItem(CHECKPOINT_SOURCE);
      if (!raw) return false;
      const source = JSON.parse(raw);
      if (!Array.isArray(source)) throw new Error('invalid checkpoint');
      state.jobs = source.map((item, index) => {
        if (!item || typeof item.name !== 'string' || typeof item.category !== 'string') throw new Error('invalid row');
        let saved = {};
        try { saved = JSON.parse(localStorage.getItem(`${CHECKPOINT_STATUS_PREFIX}${index}`) || '{}'); } catch (_) { /* use pending */ }
        const status = ['pending', 'sending', 'done', 'failed', 'unknown'].includes(saved.status) ? saved.status : 'pending';
        const job = {
          index, line: Number(item.line) || index + 2, name: item.name, category: item.category,
          about: String(item.about || ''), status: status === 'sending' ? 'unknown' : status,
          pageId: String(saved.pageId || ''),
          notBefore: Number.isFinite(Number(saved.notBefore)) ? Math.max(0, Number(saved.notBefore)) : 0,
          detail: status === 'sending' ? 'Trang điều khiển đã đóng khi đang gửi. Kiểm tra Meta trước khi chạy lại.' : String(saved.detail || ''),
        };
        if (status === 'sending') checkpointStatus(job);
        return job;
      });
      return state.jobs.length > 0;
    } catch (_) {
      state.jobs = [];
      clearCheckpoint();
      return false;
    }
  }

  function message(text, error = false) {
    el.message.textContent = text;
    el.message.classList.toggle('error', error);
  }
  function scrub(text, token) {
    const value = String(text || '');
    return token ? value.split(token).join('[token đã ẩn]') : value;
  }
  function setBusy(busy) {
    el.csvFile.disabled = busy;
    el.csvText.disabled = busy;
    el.loadBtn.disabled = busy;
    el.token.disabled = busy;
    el.version.disabled = busy;
    el.delay.disabled = busy;
    el.startBtn.disabled = busy || !state.jobs.some(job => job.status === 'pending');
    el.pauseBtn.disabled = !busy;
    el.cancelBtn.disabled = !busy;
    el.exportBtn.disabled = !state.jobs.length;
  }
  function render() {
    const jobs = state.jobs;
    const counts = { pending: 0, sending: 0, done: 0, failed: 0, unknown: 0 };
    for (const job of jobs) counts[job.status]++;
    el.summary.textContent = `${jobs.length} Page · Đã tạo ${counts.done} · Chờ ${counts.pending} · Lỗi ${counts.failed} · Chưa rõ ${counts.unknown}${state.checkpointError ? ' · Không lưu được tiến trình trên máy' : ''}`;
    const pages = Math.ceil(jobs.length / PAGE_SIZE);
    state.page = Math.max(0, Math.min(state.page, Math.max(0, pages - 1)));
    el.pageLabel.textContent = `Trang ${pages ? state.page + 1 : 0}/${pages}`;
    el.prevBtn.disabled = state.page === 0;
    el.nextBtn.disabled = state.page + 1 >= pages;
    const fragment = document.createDocumentFragment();
    for (const job of jobs.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE)) {
      const tr = document.createElement('tr');
      const cells = [job.line, job.name, job.category, statusNames[job.status], job.pageId, job.detail];
      for (let i = 0; i < cells.length; i++) {
        const td = document.createElement('td');
        if (i === 3) {
          const badge = document.createElement('span');
          badge.className = `status ${job.status}`;
          badge.textContent = String(cells[i]);
          td.appendChild(badge);
        } else td.textContent = String(cells[i]);
        if (i === 5) td.className = 'detail';
        tr.appendChild(td);
      }
      fragment.appendChild(tr);
    }
    el.resultsBody.replaceChildren(fragment);
    if (!state.running) setBusy(false);
  }
  function wake() { if (state.resume) { const resume = state.resume; state.resume = null; resume(); } }
  async function waitIfPaused() {
    while (state.paused && !state.cancelled) await new Promise(resolve => { state.resume = resolve; });
    return !state.cancelled;
  }
  async function waitMs(ms) {
    const until = Date.now() + Math.max(0, ms);
    while (Date.now() < until && !state.cancelled) {
      if (!await waitIfPaused()) break;
      await new Promise(resolve => setTimeout(resolve, Math.min(250, until - Date.now())));
    }
    return !state.cancelled;
  }
  async function requestGraph(url, options, token, post = false) {
    const controller = new AbortController();
    state.activeController = controller;
    state.inPost = post;
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(url, {
        ...options,
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json', ...(options.headers || {}) },
        signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
      const raw = await response.text();
      let payload;
      try { payload = raw ? JSON.parse(raw) : {}; }
      catch (_) { throw new Error('Meta trả dữ liệu không phải JSON; cần kiểm tra kết quả trên Meta.'); }
      return { response, payload };
    } finally {
      clearTimeout(timer);
      state.activeController = null;
      state.inPost = false;
    }
  }
  async function submitPage(job, base, token, minimumDelay) {
    let retries = 0;
    while (!state.cancelled) {
      if (!await waitIfPaused()) return { stopped: true };
      job.status = 'sending'; job.detail = retries ? `Thử lại lần ${retries}.` : '';
      job.notBefore = 0;
      checkpointStatus(job);
      render();
      let result;
      try {
        result = await requestGraph(`${base}/accounts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: makePageParams(job),
        }, token, true);
      } catch (_) {
        job.status = 'unknown';
        job.detail = 'Không rõ Page đã được tạo hay chưa. Kiểm tra Meta trước khi chạy lại.';
        checkpointStatus(job);
        render();
        return { stopped: true, reason: `Dừng ở dòng ${job.line}: không xác định được kết quả tạo Page.` };
      }
      const { response, payload } = result;
      if (response.status >= 500) {
        job.status = 'unknown';
        job.detail = `Meta trả HTTP ${response.status}; không rõ Page đã được tạo hay chưa. Kiểm tra trên Meta.`;
        checkpointStatus(job);
        render();
        return { stopped: true, reason: `Dừng ở dòng ${job.line}: ${job.detail}` };
      }
      if (response.ok && payload && !payload.error) {
        const id = payload.id == null ? payload.page_id : payload.id;
        if (id == null || !/^\d+$/.test(String(id))) {
          job.status = 'unknown';
          job.detail = 'Meta trả HTTP thành công nhưng không có Page ID. Kiểm tra trên Meta trước khi chạy lại.';
          checkpointStatus(job);
          render();
          return { stopped: true, reason: `Dừng ở dòng ${job.line}: ${job.detail}` };
        }
        job.status = 'done';
        job.pageId = String(id);
        job.detail = '';
        job.notBefore = 0;
        checkpointStatus(job);
        render();
        return { delay: Math.max(minimumDelay, usageDelay(response.headers)) };
      }
      const error = classifyGraphError(payload, response.status);
      if (error.fatal) {
        job.status = 'failed';
        job.detail = scrub(`Mã ${error.code || response.status}: ${error.message}`, token);
        checkpointStatus(job);
        render();
        return { stopped: true, reason: `Dừng ở dòng ${job.line}: ${job.detail}` };
      }
      if (error.retryable && retries < MAX_RETRIES) {
        retries++;
        const retryAfter = parseRetryAfter(response.headers.get('retry-after'));
        const baseBackoff = error.code === 80001 ? 900000 : 10000 * 2 ** (retries - 1);
        const delay = Math.max(minimumDelay, retryAfter, usageDelay(response.headers), baseBackoff);
        job.status = 'pending';
        job.notBefore = Date.now() + delay;
        if (delay > 3600000) {
          job.detail = `Meta yêu cầu chờ ${Math.ceil(delay / 60000)} phút. Đã dừng; chỉ chạy tiếp sau thời gian này.`;
          checkpointStatus(job);
          render();
          return { stopped: true, reason: `Dừng ở dòng ${job.line}: ${job.detail}` };
        }
        job.detail = `Meta giới hạn yêu cầu; chờ ${Math.ceil(delay / 1000)} giây rồi thử lại (${retries}/${MAX_RETRIES}).`;
        checkpointStatus(job);
        render();
        message(job.detail);
        if (!await waitMs(delay)) return { stopped: true };
        continue;
      }
      job.status = 'failed';
      job.detail = scrub(`Mã ${error.code || response.status}: ${error.message}`, token);
      checkpointStatus(job);
      render();
      if (error.fatal || error.retryable) return { stopped: true, reason: `Dừng ở dòng ${job.line}: ${job.detail}` };
      return { delay: Math.max(minimumDelay, usageDelay(response.headers)) };
    }
    return { stopped: true };
  }

  async function runBatch() {
    const token = el.token.value.trim();
    if (!token) { message('Cần User access token của ứng dụng Meta được phép tạo Page.', true); return; }
    let version, minimumDelay;
    try {
      version = normalizeVersion(el.version.value);
      const seconds = Number(el.delay.value);
      if (!Number.isInteger(seconds) || seconds < 5 || seconds > 3600) throw new Error('Khoảng nghỉ phải từ 5 đến 3600 giây.');
      minimumDelay = seconds * 1000;
    } catch (error) { message(error.message, true); return; }
    state.running = true; state.paused = false; state.cancelled = false;
    el.pauseBtn.textContent = 'Tạm dừng';
    setBusy(true);
    let finalMessage = 'Đã hoàn tất danh sách.', isError = false;
    try {
      message('Đang kiểm tra User access token với Meta…');
      const preflight = await requestGraph(`${GRAPH_ORIGIN}/${version}/me?fields=id`, { method: 'GET' }, token);
      if (!preflight.response.ok || !preflight.payload || !preflight.payload.id) {
        const error = classifyGraphError(preflight.payload, preflight.response.status);
        throw new Error(scrub(`Không xác thực được token: ${error.message}`, token));
      }
      const userId = String(preflight.payload.id);
      if (!/^\d+$/.test(userId)) throw new Error('Meta trả User ID không hợp lệ.');
      const base = `${GRAPH_ORIGIN}/${version}/${userId}`;
      let consecutiveFailures = 0;
      for (const job of state.jobs) {
        if (state.cancelled) { finalMessage = 'Đã dừng. Các dòng đang chờ có thể chạy lại.'; break; }
        if (job.status !== 'pending') continue;
        if (!await waitIfPaused()) { finalMessage = 'Đã dừng.'; break; }
        if (job.notBefore && job.notBefore > Date.now()) {
          const remaining = job.notBefore - Date.now();
          if (remaining > 3600000) {
            finalMessage = `Dòng ${job.line} cần chờ thêm ${Math.ceil(remaining / 60000)} phút theo giới hạn Meta.`;
            isError = true;
            break;
          }
          message(`Đang chờ giới hạn Meta: ${Math.ceil(remaining / 1000)} giây.`);
          if (!await waitMs(remaining)) { finalMessage = 'Đã dừng.'; break; }
        }
        message(`Đang xử lý dòng ${job.line}: ${job.name}`);
        const result = await submitPage(job, base, token, minimumDelay);
        if (result.stopped) {
          finalMessage = result.reason || 'Đã dừng. Kiểm tra trạng thái trước khi chạy lại.';
          isError = !!result.reason;
          break;
        }
        consecutiveFailures = job.status === 'failed' ? consecutiveFailures + 1 : 0;
        if (consecutiveFailures >= 3) {
          finalMessage = 'Ba Page liên tiếp bị lỗi. Đã dừng để kiểm tra cấu hình và quyền Meta.';
          isError = true;
          break;
        }
        const next = state.jobs.find(candidate => candidate.status === 'pending');
        if (next) {
          next.notBefore = Math.max(next.notBefore || 0, Date.now() + result.delay);
          checkpointStatus(next);
          message(`Đã xử lý dòng ${job.line}. Chờ ${Math.ceil(result.delay / 1000)} giây trước Page tiếp theo.`);
          if (!await waitMs(result.delay)) { finalMessage = 'Đã dừng.'; break; }
        }
      }
    } catch (error) {
      finalMessage = state.cancelled ? 'Đã dừng.' : scrub(error.message || 'Không thể kết nối Meta.', token);
      isError = !state.cancelled;
    } finally {
      state.running = false; state.paused = false; state.activeController = null; state.inPost = false;
      el.token.value = '';
      el.pauseBtn.textContent = 'Tạm dừng';
      render();
      message(finalMessage, isError);
    }
  }

  el.csvFile.addEventListener('change', async () => {
    const file = el.csvFile.files && el.csvFile.files[0];
    if (!file) return;
    try { el.csvText.value = await file.text(); message(`Đã đọc ${file.name}. Bấm “Nạp và kiểm tra danh sách”.`); }
    catch (_) { message('Không đọc được tệp CSV.', true); }
  });
  el.loadBtn.addEventListener('click', () => {
    if (state.running) return;
    try {
      const jobs = parseJobs(el.csvText.value);
      state.jobs = jobs.map((job, index) => ({ ...job, index, notBefore: 0 }));
      state.page = 0;
      checkpointSource();
      render();
      message(state.checkpointError
        ? `Đã kiểm tra ${state.jobs.length} Page, nhưng trình duyệt không lưu được tiến trình. Giữ trang này mở và xuất kết quả trước khi đóng.`
        : `Đã kiểm tra ${state.jobs.length} Page. Hãy xác nhận quyền Meta trước khi bắt đầu.`, state.checkpointError);
    } catch (error) {
      state.jobs = [];
      state.page = 0;
      clearCheckpoint();
      render();
      message(error.message, true);
    }
  });
  el.startBtn.addEventListener('click', () => { if (!state.running) void runBatch(); });
  el.pauseBtn.addEventListener('click', () => {
    if (!state.running) return;
    state.paused = !state.paused;
    el.pauseBtn.textContent = state.paused ? 'Tiếp tục' : 'Tạm dừng';
    if (state.paused) message('Đã yêu cầu tạm dừng sau yêu cầu hiện tại.');
    else { message('Đã tiếp tục.'); wake(); }
  });
  el.cancelBtn.addEventListener('click', () => {
    if (!state.running) return;
    state.cancelled = true;
    wake();
    if (state.activeController) state.activeController.abort();
    message('Đang dừng. Nếu đã gửi yêu cầu, hãy kiểm tra Meta trước khi chạy lại.');
  });
  el.exportBtn.addEventListener('click', () => {
    if (!state.jobs.length) return;
    const blob = new Blob([exportRows(state.jobs)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `xmeta-page-results-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  });
  el.prevBtn.addEventListener('click', () => { state.page--; render(); });
  el.nextBtn.addEventListener('click', () => { state.page++; render(); });
  const recovered = restoreCheckpoint();
  render();
  if (recovered) message('Đã khôi phục tiến trình trước đó. Các yêu cầu đang gửi khi trang đóng được đánh dấu “chưa rõ”.');
});
