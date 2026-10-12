'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const kindNames = {text: 'Văn bản', photos: 'Ảnh', video: 'Video', reel: 'Reels', batch: 'Nhiều bài'};
  const kindIcons = {text: 'text', photos: 'photo', video: 'video', reel: 'reel'};
  const finalStates = new Set(['published', 'scheduled', 'processing', 'failed', 'uncertain', 'cancelled']);
  const stateLabels = {queued: 'Trong hàng đợi', running: 'Đang đăng', published: 'Đã đăng', scheduled: 'Meta đã nhận lịch', processing: 'Meta đang xử lý', failed: 'Thất bại', uncertain: 'Cần kiểm tra', cancelled: 'Đã hủy'};
  const stateClasses = {queued: 'neutral', running: 'running', published: 'success', scheduled: 'connected', processing: 'processing', failed: 'error', uncertain: 'processing', cancelled: 'neutral'};
  const state = {csrf: '', connected: false, demo: false, graphVersion: '', sessionRevision: null, pages: [], selected: new Set(), media: [], pickedMedia: new Set(), previews: new Map(), kind: 'text', composerId: crypto.randomUUID(), job: null, viewJob: null, history: [], plans: [], drafts: [], editingId: null, uploading: false, busy: false, pending: null, pendingBatch: false, reviewIdentity: null, uncertainSubmission: null, pendingPosts: null, pollError: false};
  let toastTimer;
  let lastDispatchedSessionRevision = null;
  const commentLabels = {waiting_post: 'Chờ bài được đăng', waiting_delay: 'Chờ đến giờ bình luận', waiting_connection: 'Chờ kết nối Fanpage', sending: 'Đang gửi bình luận', commented: 'Đã bình luận', failed: 'Lỗi bình luận', uncertain: 'Cần kiểm tra bình luận', cancelled: 'Đã hủy bình luận'};
  const pendingComments = new Set(['waiting_post', 'waiting_delay', 'waiting_connection']);
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'giờ trên máy tính';

  function localInputDate(date) {
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function displayTime(value) {
    if (value === null || value === undefined || value === '') return 'Đăng ngay';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('vi-VN', {day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }

  function scheduledTime() {
    if (!$('scheduleLater').checked) return null;
    const time = new Date($('scheduledAt').value).getTime();
    return Number.isNaN(time) ? NaN : Math.floor(time / 1000);
  }

  function scheduleError(time) {
    if (time === null) return '';
    const earliest = Date.now() / 1000 + (state.demo ? 5 : 20 * 60);
    if (!Number.isFinite(time)) return 'Chọn ngày và giờ hẹn đăng bài.';
    if (time < earliest) return state.demo ? 'Giờ chạy thử phải cách hiện tại ít nhất 5 giây.' : 'Meta hẹn giờ cần cách hiện tại ít nhất 20 phút.';
    if (time > Date.now() / 1000 + 28 * 86400) return 'Giờ hẹn phải nằm trong 28 ngày tới.';
    return '';
  }

  function renderSchedule() {
    const later = $('scheduleLater').checked;
    $('scheduleField').hidden = !later; $('scheduleNotice').hidden = !later;
    $('scheduleTimezone').textContent = 'Múi giờ: ' + browserTimezone + ' · theo máy tính của bạn';
    $('scheduleHint').textContent = state.demo ? 'Dùng thử: chọn giờ cách hiện tại ít nhất 5 giây, trong 28 ngày.' : 'Chọn giờ cách hiện tại ít nhất 20 phút, trong 28 ngày.';
    $('scheduledAt').min = localInputDate(new Date(Date.now() + (state.demo ? 5000 : 20 * 60000)));
    $('scheduledAt').max = localInputDate(new Date(Date.now() + 28 * 86400000));
    if (later && !$('scheduledAt').value) $('scheduledAt').value = localInputDate(new Date(Date.now() + (state.demo ? 60000 : 30 * 60000)));
    if (state.demo) $('scheduleNotice').lastElementChild.textContent = 'Chế độ dùng thử mô phỏng lịch hẹn. Không tạo lịch hoặc đăng bài trên Facebook.';
    else $('scheduleNotice').lastElementChild.textContent = 'Lịch được gửi cho Meta ngay khi xác nhận. Meta sẽ đăng vào giờ đã hẹn, kể cả khi máy tính tắt.';
  }

  function snapshot() {
    const pages = selectedPages(), media = chosenMedia();
    return {id: state.editingId || state.composerId, page_ids: pages.map(page => page.id), page_names: pages.map(page => page.name), kind: state.kind, message: $('postMessage').value.trim(), title: state.kind === 'video' ? $('videoTitle').value.trim() : '', media_ids: media.map(item => item.id), media_names: media.map(item => item.name), scheduled_at: scheduledTime(), comment_message: $('commentMessage').value.trim(), comment_delay_seconds: Number($('commentDelaySeconds').value)};
  }

  function postPayload(post) {
    const {id, page_ids, kind, message, title, media_ids, scheduled_at, comment_message, comment_delay_seconds} = post;
    return {id, page_ids, kind, message, title, media_ids, scheduled_at, comment_message, comment_delay_seconds};
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
  }

  function notify(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {$('toast').hidden = true;}, 4800);
  }

  function error(message) {
    $('globalError').textContent = message || '';
    $('globalError').hidden = !message;
  }

  async function request(path, {method = 'GET', data, body, headers = {}} = {}) {
    const options = {method, cache: 'no-store', credentials: 'same-origin', headers: {...headers}};
    if (method !== 'GET') options.headers['X-CSRF-Token'] = state.csrf;
    if (data !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(data);
    } else if (body !== undefined) options.body = body;
    let response;
    try { response = await fetch(path, options); }
    catch (_) { const err = new Error('Không kết nối được với ứng dụng trên máy. Kiểm tra cửa sổ chạy PagePilot.'); err.ambiguous = true; throw err; }
    let result;
    try { result = await response.json(); }
    catch (_) { const err = new Error('Máy chủ trả về phản hồi không hợp lệ.'); err.ambiguous = true; throw err; }
    if (!response.ok) { const err = new Error(result.error || 'Thao tác chưa thành công. Vui lòng thử lại.'); err.ambiguous = response.status >= 500; throw err; }
    return result;
  }

  const active = () => state.connected || state.demo;
  const running = () => state.job && state.job.state === 'running';
  const locked = () => running() || state.busy || !!state.uncertainSubmission;
  const publishable = page => page.can_publish !== false;
  const selectedPages = () => state.pages.filter(page => state.selected.has(page.id) && publishable(page));
  const compatible = media => state.kind === 'photos' ? ['image/jpeg', 'image/png'].includes(media.content_type) : ['video', 'reel'].includes(state.kind) && media.content_type === 'video/mp4';
  const chosenMedia = () => state.media.filter(media => state.pickedMedia.has(media.id) && compatible(media));

  function initials(name) {
    return String(name || 'P').trim().split(/\s+/).filter(Boolean).slice(0, 2).map(part => [...part][0]).join('').toUpperCase();
  }

  function avatar(name, id) {
    const node = element('span', 'page-avatar', initials(name));
    let hash = 0;
    for (const char of String(id)) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
    const hues = [169, 211, 264, 31, 337, 194];
    const hue = hues[Math.abs(hash) % hues.length];
    node.style.background = `hsl(${hue} 25% 20%)`;
    node.style.color = `hsl(${hue} 47% 75%)`;
    return node;
  }

  function visiblePages() {
    const search = $('pageSearch').value.trim().toLocaleLowerCase('vi');
    return state.pages.filter(page => !search || page.name.toLocaleLowerCase('vi').includes(search) || String(page.id).includes(search));
  }

  function renderPages() {
    const list = $('pageList');
    list.replaceChildren();
    const pages = visiblePages();
    if (!pages.length) {
      const empty = element('div', 'empty-pages');
      const graphic = element('span', 'empty-icon');
      graphic.append(icon(state.pages.length ? 'search' : 'stack'));
      empty.append(graphic, element('strong', '', state.pages.length ? 'Không tìm thấy Fanpage' : active() ? 'Chưa có Fanpage khả dụng' : 'Kết nối các Fanpage của bạn'), element('p', '', state.pages.length ? 'Thử tìm bằng tên khác hoặc ID trang.' : active() ? 'Kiểm tra quyền và quyền truy cập trang trong Meta App.' : 'Kết nối Meta App để tải danh sách trang, hoặc khám phá với 50 trang mẫu.'));
      list.append(empty);
    }
    for (const page of pages) {
      const label = element('label', 'page-row' + (state.selected.has(page.id) ? ' selected' : '') + (!publishable(page) ? ' unavailable' : ''));
      const box = element('input');
      box.type = 'checkbox';
      box.checked = state.selected.has(page.id);
      box.disabled = locked() || !publishable(page);
      box.setAttribute('aria-label', 'Chọn ' + page.name);
      const info = element('div', 'page-info');
      info.append(element('div', 'page-name', page.name), element('small', 'page-id', publishable(page) ? 'ID · ' + page.id : 'Chưa có quyền đăng nội dung'));
      label.title = page.name + ' · ' + page.id;
      label.append(avatar(page.name, page.id), info, box);
      box.addEventListener('change', () => {
        if (box.checked) state.selected.add(page.id); else state.selected.delete(page.id);
        renderPages(); renderSummary(); renderPreview();
      });
      list.append(label);
    }
    const selectable = pages.filter(publishable);
    const checked = selectable.filter(page => state.selected.has(page.id)).length;
    $('selectAll').checked = selectable.length > 0 && checked === selectable.length;
    $('selectAll').indeterminate = checked > 0 && checked < selectable.length;
    $('selectAll').disabled = locked() || !selectable.length;
    $('pageCount').textContent = state.pages.length;
    $('totalPages').textContent = String(state.pages.length).padStart(2, '0');
    $('selectedCount').textContent = selectedPages().length + ' đã chọn';
  }

  function commonValidation() {
    if (state.uncertainSubmission) return 'Chưa xác định lượt gửi trước. Dùng nút kiểm tra và tiếp tục lượt đó trước khi tạo lượt mới.';
    if (!active()) return 'Kết nối Meta App hoặc dùng thử để bắt đầu.';
    if (running()) return 'Đang gửi một lượt bài. Đợi hoàn tất hoặc dừng hàng đợi.';
    if (state.uploading) return 'Đang tải tệp. Vui lòng đợi tải lên hoàn tất.';
    const delay = Number($('delaySeconds').value);
    if (!Number.isInteger(delay) || delay < 1 || delay > 60) return 'Khoảng nghỉ cần là số nguyên từ 1 đến 60 giây.';
    return '';
  }

  function validation() {
    const common = commonValidation(); if (common) return common;
    if (!selectedPages().length) return 'Chọn ít nhất một Fanpage cho bài này.';
    if (state.kind === 'text' && !$('postMessage').value.trim()) return 'Nhập nội dung cho bài viết của bạn.';
    const count = chosenMedia().length;
    if (state.kind === 'photos' && (!count || count > 10)) return 'Chọn từ 1 đến 10 ảnh cho bài viết.';
    if (['video', 'reel'].includes(state.kind) && count !== 1) return 'Chọn đúng một tệp video MP4.';
    const commentDelay = Number($('commentDelaySeconds').value);
    if (!Number.isInteger(commentDelay) || commentDelay < 0 || commentDelay > 3600) return 'Thời gian chờ bình luận cần từ 0 đến 3600 giây.';
    return scheduleError(scheduledTime());
  }

  function reviewValidation() {
    if (!state.drafts.length) return validation();
    const common = commonValidation(); if (common) return common;
    if (state.editingId) return 'Lưu thay đổi hoặc hủy sửa bài trước khi xác nhận danh sách.';
    const tasks = state.drafts.reduce((sum, post) => sum + post.page_ids.length, 0);
    if (state.drafts.length > 100 || tasks > 1000) return 'Mỗi lượt gửi tối đa 100 bài và 1000 lượt bài × Fanpage.';
    for (let i = 0; i < state.drafts.length; i++) {
      const post = state.drafts[i];
      if (post.page_ids.some(id => !state.pages.some(page => page.id === id && publishable(page)))) return `Bài ${i+1} có Fanpage không thuộc kết nối hiện tại. Sửa bài để chọn lại trang.`;
      if (post.media_ids.some(id => !state.media.some(media => media.id === id))) return `Bài ${i+1} thiếu tệp đính kèm. Sửa bài và tải lại tệp.`;
      const problem = scheduleError(post.scheduled_at); if (problem) return `Bài ${i+1}: ${problem}`;
    }
    return '';
  }

  function renderSummary() {
    $('uncertainNotice').hidden = !state.uncertainSubmission;
    const batch = state.drafts.length > 0;
    const tasks = state.drafts.reduce((sum, post) => sum + post.page_ids.length, 0);
    $('summarySelected').textContent = batch ? tasks : selectedPages().length;
    $('summaryMainLabel').textContent = batch ? 'lượt bài × Fanpage' : 'Fanpage đã chọn';
    $('summarySubtitle').textContent = batch ? state.drafts.length + ' bài trong danh sách' : 'Cho bài đang soạn';
    $('summaryKind').textContent = batch ? state.drafts.length + ' bài riêng biệt' : kindNames[state.kind];
    const count = chosenMedia().length;
    const attachments = batch ? state.drafts.reduce((sum, post) => sum + post.media_ids.length, 0) : count;
    $('summaryMedia').textContent = attachments ? attachments + ' tệp' : '—';
    $('mediaCount').textContent = count + ' tệp đã chọn';
    const hint = reviewValidation();
    $('reviewButton').disabled = !!hint || state.busy;
    $('readyHint').textContent = hint || (batch ? 'Chỉ gửi các bài trong danh sách. Bài đang soạn chưa thêm sẽ không được gửi.' : state.demo ? 'Chạy thử để xem quy trình. Không đăng lên Facebook.' : 'Bạn sẽ xem lại và xác nhận trước khi gửi bài.');
    $('reviewButton').replaceChildren(document.createTextNode(state.demo ? 'Xem lại & chạy thử' : batch ? 'Xem lại danh sách bài' : 'Xem lại & gửi bài'), icon('arrow'));
    for (const id of ['postMessage', 'videoTitle', 'delaySeconds', 'scheduleNow', 'scheduleLater', 'scheduledAt', 'commentMessage', 'commentDelaySeconds']) $(id).disabled = locked();
    $('addDraftButton').disabled = !!validation() || state.busy || (!state.editingId && state.drafts.length >= 100);
    $('addDraftButton').replaceChildren(icon(state.editingId ? 'check' : 'plus'), document.createTextNode(state.editingId ? 'Lưu thay đổi bài' : 'Thêm vào danh sách bài'));
    $('cancelEditButton').hidden = !state.editingId; $('cancelEditButton').disabled = locked();
    $('mediaInput').disabled = locked() || !active() || state.uploading;
    $('uploadDropzone').classList.toggle('disabled', $('mediaInput').disabled);
    document.querySelectorAll('.type-button').forEach(button => {button.disabled = locked() || state.uploading;});
    $('disconnectButton').disabled = locked() || state.uploading;
    $('connectButton').disabled = locked() || state.uploading || !state.csrf;
    $('demoButton').disabled = locked() || state.uploading || !state.csrf;
    $('demoConnectButton').disabled = locked() || state.uploading || !state.csrf;
  }

  function clearComposer() {
    $('postMessage').value = ''; $('videoTitle').value = ''; $('commentMessage').value = ''; $('commentDelaySeconds').value = '0';
    state.pickedMedia.clear(); state.editingId = null; state.composerId = crypto.randomUUID();
    renderDrafts(); renderSummary(); renderMedia(); renderPreview();
  }

  function renderDrafts() {
    $('draftSection').hidden = !state.drafts.length; $('draftCount').textContent = state.drafts.length;
    const list = $('draftList'); list.replaceChildren();
    state.drafts.forEach((post, index) => {
      const row = element('article', 'draft-item' + (post.id === state.editingId ? ' editing' : ''));
      const heading = element('div', 'draft-item-heading');
      const actions = element('div', 'draft-item-actions');
      const edit = element('button', 'button button-ghost button-small', 'Sửa'), remove = element('button', 'button button-ghost button-small', 'Xóa');
      edit.disabled = remove.disabled = locked();
      edit.type = remove.type = 'button';
      edit.setAttribute('aria-label', 'Sửa bài ' + (index+1)); remove.setAttribute('aria-label', 'Xóa bài ' + (index+1));
      edit.addEventListener('click', () => {
        state.editingId = post.id; setKind(post.kind); state.selected = new Set(post.page_ids); state.pickedMedia = new Set(post.media_ids);
        $('postMessage').value = post.message; $('videoTitle').value = post.title; $('commentMessage').value = post.comment_message; $('commentDelaySeconds').value = post.comment_delay_seconds;
        $('scheduleNow').checked = post.scheduled_at === null; $('scheduleLater').checked = post.scheduled_at !== null;
        $('scheduledAt').value = post.scheduled_at === null ? '' : localInputDate(new Date(post.scheduled_at * 1000));
        renderSchedule(); renderDrafts(); renderPages(); renderSummary(); renderMedia(); renderPreview();
        $('composeHeading').scrollIntoView({behavior: 'smooth', block: 'start'}); $('postMessage').focus();
      });
      remove.addEventListener('click', () => {const wasEditing = state.editingId === post.id; state.drafts = state.drafts.filter(item => item.id !== post.id); if (wasEditing) clearComposer(); else {renderDrafts(); renderSummary(); renderMedia();}});
      actions.append(edit, remove); heading.append(element('strong', '', `Bài ${index+1} · ${kindNames[post.kind]} · ${post.page_ids.length} Fanpage`), actions);
      row.append(heading, element('p', '', post.message || post.title || '(Nội dung đính kèm)'), element('div', 'draft-meta' + (post.scheduled_at !== null ? ' scheduled' : ''), post.scheduled_at === null ? 'Đăng ngay khi xác nhận' : 'Meta hẹn giờ · ' + displayTime(post.scheduled_at)));
      if (post.media_ids.length) row.append(element('div', 'draft-meta', post.media_ids.length + ' tệp · ' + post.media_names.join(', ')));
      if (post.comment_message) row.append(element('div', 'draft-meta', 'Có bình luận · chờ ' + post.comment_delay_seconds + ' giây sau khi bài đăng'));
      list.append(row);
    });
  }

  function renderSession() {
    if (state.sessionRevision !== null && state.sessionRevision !== lastDispatchedSessionRevision) {
      lastDispatchedSessionRevision = state.sessionRevision;
      window.dispatchEvent(new CustomEvent('pagepilot:session', {detail: {connected: state.connected, demo: state.demo, revision: state.sessionRevision, pages: state.pages.map(page => ({id: page.id, name: page.name}))}}));
    }
    const badge = $('sessionBadge');
    badge.textContent = state.demo ? 'DÙNG THỬ' : state.connected ? 'Đã kết nối' : 'Chưa kết nối';
    badge.className = 'badge ' + (state.demo ? 'demo' : state.connected ? 'connected' : 'neutral');
    $('demoNotice').hidden = !state.demo;
    $('disconnectButton').hidden = !active();
    $('connectButton').hidden = false;
    $('connectButton').replaceChildren(icon('link'), document.createTextNode(state.connected && !state.demo ? 'Cập nhật kết nối' : 'Kết nối Meta App'));
    $('demoButton').hidden = active();
    $('connectionDetail').textContent = state.demo ? 'Phiên dùng thử · 50 Fanpage mẫu' : state.connected ? 'Meta Graph API ' + state.graphVersion : 'Chưa kết nối';
    if (/^v[1-9]\d?\.0$/.test(state.graphVersion)) $('graphVersion').value = state.graphVersion;
    renderSchedule(); renderDrafts(); renderPages(); renderSummary(); renderPreview(); renderMedia();
  }

  function setKind(kind) {
    if (locked() || state.uploading) return;
    state.kind = kind;
    document.querySelectorAll('.type-button').forEach(button => {
      const selected = button.dataset.kind === kind;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    $('videoTitleField').hidden = kind !== 'video';
    $('mediaField').hidden = kind === 'text';
    $('messageRequirement').textContent = kind === 'text' ? '· bắt buộc' : '· tùy chọn';
    $('mediaInput').accept = kind === 'photos' ? 'image/jpeg,image/png' : 'video/mp4';
    $('mediaInput').multiple = kind === 'photos';
    $('uploadLabel').textContent = kind === 'photos' ? 'Chọn ảnh từ máy tính' : 'Chọn video từ máy tính';
    $('uploadHint').textContent = kind === 'photos' ? 'JPEG, PNG · tối đa 10 ảnh · 20 MB/ảnh' : 'MP4 · một video · tối đa 1 GB';
    $('uploadStatus').textContent = '';
    renderMedia(); renderSummary(); renderPreview();
  }

  function formatSize(size) {
    if (size >= 1024 * 1024 * 1024) return (size / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    if (size >= 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + ' MB';
    return Math.max(1, Math.round(size / 1024)) + ' KB';
  }

  function renderMedia() {
    $('mediaList').replaceChildren();
    for (const media of state.media.filter(compatible)) {
      const row = element('div', 'media-item');
      const box = element('input');
      box.type = 'checkbox'; box.checked = state.pickedMedia.has(media.id); box.disabled = locked() || state.uploading;
      box.setAttribute('aria-label', 'Đính kèm ' + media.name);
      box.addEventListener('change', () => {
        if (box.checked) {
          if (state.kind === 'photos' && chosenMedia().length >= 10) {box.checked = false; notify('Mỗi bài viết đính kèm tối đa 10 ảnh.'); return;}
          if (state.kind !== 'photos') state.media.filter(compatible).forEach(item => state.pickedMedia.delete(item.id));
          state.pickedMedia.add(media.id);
        } else state.pickedMedia.delete(media.id);
        renderMedia(); renderSummary(); renderPreview();
      });
      let thumb;
      if (state.previews.has(media.id) && media.content_type.startsWith('image/')) {
        thumb = element('img', 'media-thumb'); thumb.src = state.previews.get(media.id); thumb.alt = '';
      } else {thumb = element('span', 'media-thumb'); thumb.append(icon(media.content_type.startsWith('image/') ? 'photo' : 'video'));}
      const info = element('div', 'media-info');
      info.append(element('strong', '', media.name), element('small', '', formatSize(media.size)));
      const remove = element('button', 'icon-button');
      remove.type = 'button'; remove.title = 'Xóa tệp ' + media.name; remove.setAttribute('aria-label', remove.title); remove.disabled = locked() || state.uploading || state.drafts.some(post => post.media_ids.includes(media.id)); if (state.drafts.some(post => post.media_ids.includes(media.id))) remove.title = 'Tệp đang dùng trong danh sách bài. Xóa hoặc sửa các bài đó trước khi xóa tệp.'; remove.append(icon('x'));
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        try {
          await request('/api/media/' + encodeURIComponent(media.id), {method: 'DELETE'});
          state.media = state.media.filter(item => item.id !== media.id); state.pickedMedia.delete(media.id);
          if (state.previews.has(media.id)) {URL.revokeObjectURL(state.previews.get(media.id)); state.previews.delete(media.id);}
          renderMedia(); renderSummary(); renderPreview();
        } catch (err) {error(err.message); remove.disabled = false;}
      });
      row.append(box, thumb, info, remove); $('mediaList').append(row);
    }
  }

  async function uploadFiles(files) {
    if (locked() || state.uploading || !active()) return;
    const selected = Array.from(files);
    if (!selected.length) return;
    const photos = state.kind === 'photos';
    if ((photos && chosenMedia().length + selected.length > 10) || (!photos && selected.length !== 1)) {
      error(photos ? 'Mỗi bài viết chọn tối đa 10 ảnh. Bỏ chọn một số ảnh trước khi thêm.' : 'Chỉ chọn một video MP4 cho mỗi bài đăng.'); return;
    }
    const typed = selected.map(file => ({file, type: file.type || (/\.jpe?g$/i.test(file.name) ? 'image/jpeg' : /\.png$/i.test(file.name) ? 'image/png' : /\.mp4$/i.test(file.name) ? 'video/mp4' : '')}));
    for (const {file, type} of typed) {
      if (photos ? !['image/jpeg', 'image/png'].includes(type) : type !== 'video/mp4') {error('Định dạng không phù hợp: ' + file.name + '. ' + (photos ? 'Chọn JPEG hoặc PNG.' : 'Chọn video MP4.')); return;}
      if (file.size > (photos ? 20 * 1024 * 1024 : 1024 * 1024 * 1024)) {error('Tệp vượt quá dung lượng cho phép: ' + file.name); return;}
      if (file.size === 0) {error('Tệp rỗng: ' + file.name); return;}
    }
    error(''); state.uploading = true; renderSummary(); renderMedia();
    let uploaded = 0;
    try {
      for (const {file, type} of typed) {
        $('uploadStatus').textContent = `Đang tải tệp ${uploaded + 1}/${typed.length}: ${file.name}`;
        const media = await request('/api/media', {method: 'POST', body: file, headers: {'Content-Type': type, 'X-Filename': encodeURIComponent(file.name)}});
        state.media.push(media);
        if (!photos) state.media.filter(item => item.content_type === 'video/mp4').forEach(item => state.pickedMedia.delete(item.id));
        state.pickedMedia.add(media.id);
        if (photos) state.previews.set(media.id, URL.createObjectURL(file));
        uploaded++; renderMedia(); renderPreview();
      }
      $('uploadStatus').textContent = 'Đã tải lên ' + uploaded + ' tệp. Chọn tệp đính kèm bên dưới.';
    } catch (err) {error(err.message); $('uploadStatus').textContent = uploaded ? 'Đã tải được ' + uploaded + ' tệp trước khi gặp lỗi.' : '';}
    finally {state.uploading = false; $('mediaInput').value = ''; renderMedia(); renderSummary(); renderPreview();}
  }

  function renderPreview() {
    const pages = selectedPages();
    const name = pages.length ? pages[0].name : 'Fanpage đã chọn';
    $('previewName').textContent = name;
    $('previewAvatar').textContent = initials(name);
    const message = $('postMessage').value;
    $('previewMessage').textContent = message || 'Nội dung của bạn sẽ xuất hiện ở đây…';
    $('previewMessage').classList.toggle('placeholder-text', !message);
    $('characterCount').textContent = new Intl.NumberFormat('vi-VN').format([...message].length) + ' ký tự';
    const media = chosenMedia();
    const preview = $('previewMedia');
    preview.replaceChildren(); preview.hidden = !media.length; preview.classList.toggle('single', media.length === 1 || state.kind !== 'photos');
    if (state.kind === 'photos') {
      for (const item of media.slice(0, 4)) {
        if (state.previews.has(item.id)) {const img = element('img'); img.src = state.previews.get(item.id); img.alt = item.name; preview.append(img);}
        else {const placeholder = element('div', 'preview-video'); placeholder.append(icon('photo'), element('span', '', item.name)); preview.append(placeholder);}
      }
    } else if (media.length) {
      const placeholder = element('div', 'preview-video');
      placeholder.append(icon(state.kind === 'reel' ? 'reel' : 'video'), element('span', '', $('videoTitle').value && state.kind === 'video' ? $('videoTitle').value : media[0].name));
      preview.append(placeholder);
    }
  }

  function postUrl(job, result) {
    if (result.permalink_url && result.permalink_url !== 'https://www.facebook.com/' + String(result.post_id)) {
      try {
        const url = new URL(result.permalink_url);
        if (url.protocol === 'https:' && ['facebook.com', 'www.facebook.com', 'm.facebook.com', 'fb.com', 'www.fb.com'].includes(url.hostname) && !url.username && !url.password) return url.href;
      } catch (_) { /* Use the post ID when a permalink is unavailable. */ }
    }
    const id = String(result.post_id);
    if ((result.kind || job.kind) === 'reel') return 'https://www.facebook.com/reel/' + encodeURIComponent(id);
    if (/^\d+_\d+$/.test(id)) {const [page, post] = id.split('_'); return 'https://www.facebook.com/' + page + '/posts/' + post;}
    return 'https://www.facebook.com/' + encodeURIComponent(id);
  }

  function renderJob() {
    const job = state.viewJob || state.job;
    $('jobPanel').hidden = !job;
    if (!job) {renderSummary(); return;}
    const results = job.results || [];
    const completed = results.filter(result => finalStates.has(result.state)).length;
    const count = type => results.filter(result => result.state === type).length;
    const published = count('published'), scheduled = count('scheduled'), failed = count('failed'), processing = count('processing'), uncertain = count('uncertain'), cancelled = count('cancelled');
    const total = job.total || results.length;
    const badge = $('jobStateBadge');
    badge.textContent = job.state === 'running' ? 'Đang gửi' : job.state === 'stopped' ? 'Đã dừng' : 'Đã xử lý';
    badge.className = 'badge ' + (job.state === 'running' ? 'running' : job.state === 'stopped' ? 'neutral' : 'success');
    $('jobProgressText').textContent = completed + ' / ' + total + ' lượt bài × Fanpage đã xử lý';
    const details = [published && published + ' đã đăng', scheduled && scheduled + ' Meta nhận lịch', processing && processing + ' Meta xử lý', failed && failed + ' lỗi', uncertain && uncertain + ' cần kiểm tra', cancelled && cancelled + ' hủy'].filter(Boolean);
    $('jobSummaryText').textContent = details.join(' · ') || 'Đang chuẩn bị hàng đợi';
    const progress = total ? Math.round(completed / total * 100) : 0;
    $('jobProgressFill').style.width = progress + '%'; $('jobProgress').setAttribute('aria-valuenow', String(progress));
    $('jobHeading').textContent = state.viewJob ? 'Chi tiết lượt bài đã lưu' : 'Tiến trình gửi bài & lịch';
    $('currentJobButton').hidden = !state.viewJob || !state.job;
    $('stopButton').hidden = !!state.viewJob || job.state !== 'running';
    let note = job.demo ? 'Kết quả mô phỏng. Không gửi bài, lịch hoặc bình luận lên Facebook.' : uncertain ? 'Có kết quả chưa xác định. Kiểm tra trên Facebook trước khi gửi lại để tránh trùng bài.' : scheduled ? 'Meta đã nhận lịch sẽ tự đăng vào giờ hẹn, kể cả khi máy tắt. Trạng thái chưa có nghĩa bài đã xuất hiện.' : processing ? 'Meta đang xử lý tệp. Chờ bài thực sự được đăng trước khi gửi bình luận.' : job.state === 'running' ? 'Dừng hàng đợi chỉ hủy các lượt chưa gửi; không hủy lịch Meta đã nhận.' : 'Kết quả được ghi riêng cho từng bài và Fanpage. Không tự động gửi lại thao tác gặp lỗi.';
    if (results.some(result => pendingComments.has(result.comment_state))) note += ' Bình luận đang chờ cần tool mở và Fanpage được kết nối.';
    $('jobNote').textContent = note;
    $('jobResults').replaceChildren();
    results.forEach((result, resultIndex) => {
      const row = element('tr');
      const name = element('td'); name.append(element('span', 'result-name', result.page_name || result.page_id), element('small', 'result-id', result.page_id));
      const postNumber = Number.isInteger(result.post_index) ? result.post_index + 1 : 1;
      name.append(element('span', 'result-post-title', 'Bài ' + postNumber + ' · ' + (result.post_title || kindNames[result.kind || job.kind] || 'Bài viết')));
      const time = element('td', 'result-schedule', result.scheduled_at == null ? 'Đăng ngay' : displayTime(result.scheduled_at));
      const status = element('td'); status.append(element('span', 'badge ' + (stateClasses[result.state] || 'neutral'), stateLabels[result.state] || result.state));
      const comment = element('td');
      if (!result.comment_state) comment.append(element('span', 'result-comment-detail', 'Không yêu cầu'));
      else {
        const classification = ['failed', 'uncertain'].includes(result.comment_state) ? 'error' : result.comment_state === 'commented' ? 'success' : 'neutral';
        comment.append(element('span', 'badge comment-status ' + classification, commentLabels[result.comment_state] || result.comment_state));
        if (result.comment_error) comment.append(element('span', 'result-comment-detail error-text', result.comment_error));
        if (result.comment_id) comment.append(element('span', 'result-comment-detail', 'ID · ' + result.comment_id));
        if (result.comment_due_at && pendingComments.has(result.comment_state)) comment.append(element('span', 'result-comment-detail', displayTime(result.comment_due_at)));
        if (pendingComments.has(result.comment_state)) {
          const cancel = element('button', 'button button-ghost comment-cancel', 'Hủy bình luận tự động'); cancel.type = 'button';
          cancel.setAttribute('aria-label', 'Hủy bình luận tự động cho bài ' + postNumber + ' trên ' + result.page_name);
          cancel.addEventListener('click', async () => {
            cancel.disabled = true;
            try {
              const response = await request('/api/comments/cancel', {method: 'POST', data: {job_id: job.id, result_index: resultIndex}});
              if (response.job) {if (state.job && state.job.id === response.job.id) state.job = response.job; if (state.viewJob && state.viewJob.id === response.job.id) state.viewJob = response.job;}
              renderJob(); refreshPlans(); notify('Đã hủy bình luận tự động. Bài đăng hoặc lịch Meta vẫn giữ nguyên.');
            } catch (err) {error(err.message); cancel.disabled = false;}
          });
          comment.append(cancel);
        }
      }
      const detail = element('td', 'result-detail' + (result.error ? ' error-text' : ''));
      if (result.error) detail.textContent = result.error;
      else if (result.post_id) {
        if (job.demo || String(result.post_id).startsWith('demo')) detail.textContent = 'Mô phỏng · ' + result.post_id;
        else {const link = element('a', '', 'ID · ' + result.post_id); link.href = postUrl(job, result); link.target = '_blank'; link.rel = 'noopener noreferrer'; detail.append(link);}
      } else detail.textContent = result.state === 'queued' ? 'Chờ đến lượt gửi' : result.state === 'running' ? 'Đang gửi đến Meta…' : result.state === 'cancelled' ? 'Chưa gửi đến Meta' : '—';
      if (result.publication_error) detail.append(element('div', 'result-comment-detail error-text', 'Kiểm tra xuất bản: ' + result.publication_error));
      row.append(name, time, status, comment, detail); $('jobResults').append(row);
    });
    renderDrafts(); renderSummary(); renderPages(); renderMedia();
  }

  function renderPlans() {
    const list = $('plansList'); list.replaceChildren();
    const plans = state.plans.filter(job => (job.results || []).some(result => ['scheduled', 'processing'].includes(result.state) || pendingComments.has(result.comment_state)));
    if (!plans.length) {list.append(element('p', 'empty-history', 'Chưa có lịch hoặc bình luận chờ.')); return;}
    for (const job of plans) {
      const row = element('button', 'history-item'); row.type = 'button';
      const graphic = element('span', 'history-item-icon'); graphic.append(icon('clock'));
      const info = element('div');
      const scheduled = (job.results || []).filter(result => result.state === 'scheduled');
      const comments = (job.results || []).filter(result => pendingComments.has(result.comment_state));
      info.append(element('strong', '', (job.posts && job.posts.length ? job.posts.length + ' bài' : kindNames[job.kind] || 'Bài viết') + ' · ' + job.total + ' lượt'));
      info.append(element('small', '', [scheduled.length && scheduled.length + ' lịch Meta', comments.length && comments.length + ' bình luận chờ'].filter(Boolean).join(' · ') || 'Meta đang xử lý'));
      if (scheduled.length) {const earliest = Math.min(...scheduled.map(result => result.scheduled_at).filter(Number.isFinite)); if (Number.isFinite(earliest)) info.append(element('small', '', displayTime(earliest)));}
      row.append(graphic, info, icon('arrow')); row.setAttribute('aria-label', 'Xem lịch và bình luận chờ của lượt ' + job.id);
      row.addEventListener('click', () => {state.viewJob = state.job && state.job.id === job.id ? null : job; renderJob(); $('jobPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});});
      list.append(row);
    }
  }

  function reconcileSubmission(job) {
    const unknown = state.uncertainSubmission;
    if (!unknown || !job || job.id !== unknown.request_id) return;
    if (unknown.batch) state.drafts = state.drafts.filter(draft => !unknown.posts.some(post => post.id === draft.id && JSON.stringify(postPayload(post)) === JSON.stringify(postPayload(draft))));
    state.uncertainSubmission = null; state.reviewIdentity = null;
    if (state.editingId && !state.drafts.some(post => post.id === state.editingId)) state.editingId = null;
    if (!state.job || state.job.id !== job.id) state.viewJob = job;
    if (state.pending && state.pending.request_id === job.id) {state.pending = null; $('reviewDialog').close();}
    renderDrafts(); renderJob();
    notify('Đã tìm thấy lượt gửi trước. Kiểm tra kết quả và lịch đã lưu.');
  }

  async function refreshPlans() {
    try {
      const result = await request('/api/plans'); state.plans = result.jobs || [];
      if (state.uncertainSubmission) reconcileSubmission(state.plans.find(job => job.id === state.uncertainSubmission.request_id));
      if (state.viewJob) {
        let updated = state.plans.find(job => job.id === state.viewJob.id);
        if (!updated) {const detail = await request('/api/jobs/' + encodeURIComponent(state.viewJob.id)); updated = detail.job;}
        if (updated && JSON.stringify(updated) !== JSON.stringify(state.viewJob)) {state.viewJob = updated; renderJob();}
      }
      renderPlans(); renderSummary();
    } catch (_) { /* The current job poll reports a lost local connection. */ }
  }

  function renderHistory() {
    const list = $('historyList'); list.replaceChildren();
    if (!state.history.length) {list.append(element('p', 'empty-history', 'Chưa có lượt đăng nào.')); return;}
    for (const job of state.history.slice(0, 4)) {
      const row = element('button', 'history-item'); row.type = 'button'; row.setAttribute('aria-label', 'Xem kết quả ' + (kindNames[job.kind] || 'bài viết') + ' trên ' + job.total + ' trang'); const graphic = element('span', 'history-item-icon'); graphic.append(icon(kindIcons[job.kind] || 'page'));
      const info = element('div'); info.append(element('strong', '', (kindNames[job.kind] || 'Bài viết') + ' · ' + (job.total || 0) + ' lượt'));
      let time = 'Phiên hiện tại';
      if (job.created_at) {const date = new Date(job.created_at); if (!Number.isNaN(date.getTime())) time = date.toLocaleString('vi-VN', {hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit'});}
      info.append(element('small', '', time));
      const badge = element('span', 'badge ' + (job.state === 'running' ? 'running' : job.state === 'stopped' ? 'neutral' : 'success'), job.state === 'running' ? 'Đang chạy' : job.state === 'stopped' ? 'Đã dừng' : 'Đã xử lý');
      row.append(graphic, info, badge); list.append(row);
      row.addEventListener('click', () => {state.viewJob = state.job && state.job.id === job.id ? null : job; renderJob(); $('jobPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});});
    }
  }

  async function bootstrap() {
    const data = await request('/api/bootstrap');
    state.csrf = data.csrf_token; state.connected = !!data.connected; state.demo = !!data.demo;
    state.graphVersion = data.graph_version || ''; state.sessionRevision = data.session_revision ?? 0; state.pages = data.pages || []; state.media = data.media || []; state.job = data.job || null; state.history = data.history || [];
    state.selected = new Set([...state.selected].filter(id => state.pages.some(page => page.id === id && publishable(page))));
    state.pickedMedia = new Set([...state.pickedMedia].filter(id => state.media.some(media => media.id === id)));
    renderSession(); renderJob(); renderHistory(); refreshPlans();
  }

  function openConnect() {
    $('connectError').hidden = true; $('accessToken').value = '';
    $('connectDialog').showModal(); $('accessToken').focus();
  }

  $('connectButton').addEventListener('click', openConnect);
  $('demoConnectButton').addEventListener('click', openConnect);
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  $('connectDialog').addEventListener('close', () => {$('accessToken').value = '';});
  $('connectForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (!$('connectForm').reportValidity()) return;
    const token = $('accessToken').value.trim(), version = $('graphVersion').value.trim();
    if (!token) return;
    $('connectSubmit').disabled = true; $('connectSubmit').textContent = 'Đang lấy Fanpage…'; $('connectError').hidden = true;
    try {
      await request('/api/connect', {method: 'POST', data: {access_token: token, graph_version: version}});
      $('accessToken').value = ''; state.selected.clear(); await bootstrap();
      $('connectDialog').close(); error(''); notify('Đã kết nối. Chọn các Fanpage bạn muốn đăng bài.');
    } catch (err) {$('connectError').textContent = err.message; $('connectError').hidden = false;}
    finally {$('accessToken').value = ''; $('connectSubmit').disabled = false; $('connectSubmit').replaceChildren(document.createTextNode('Kết nối & lấy Fanpage'), icon('arrow'));}
  });

  $('demoButton').addEventListener('click', async () => {
    state.busy = true; renderSummary(); error('');
    try {await request('/api/demo', {method: 'POST', data: {}}); state.selected.clear(); await bootstrap(); notify('Đã mở 50 Fanpage mẫu. Chọn trang để chạy thử.');}
    catch (err) {error(err.message);}
    finally {state.busy = false; renderSession();}
  });

  $('disconnectButton').addEventListener('click', async () => {
    state.busy = true; renderSummary();
    try {
      await request('/api/disconnect', {method: 'POST', data: {}});
      state.selected.clear(); state.pickedMedia.clear(); for (const url of state.previews.values()) URL.revokeObjectURL(url); state.previews.clear();
      await bootstrap(); error(''); notify('Đã ngắt kết nối và xóa token khỏi phiên chạy.');
    } catch (err) {error(err.message);}
    finally {state.busy = false; renderSession();}
  });

  $('pageSearch').addEventListener('input', renderPages);
  $('selectAll').addEventListener('change', () => {for (const page of visiblePages().filter(publishable)) {if ($('selectAll').checked) state.selected.add(page.id); else state.selected.delete(page.id);} renderPages(); renderSummary(); renderPreview();});
  document.querySelectorAll('.type-button').forEach(button => button.addEventListener('click', () => setKind(button.dataset.kind)));
  $('postMessage').addEventListener('input', () => {renderSummary(); renderPreview();});
  $('videoTitle').addEventListener('input', renderPreview);
  $('delaySeconds').addEventListener('input', renderSummary);
  $('mediaInput').addEventListener('change', event => uploadFiles(event.target.files));
  for (const name of ['dragenter', 'dragover']) $('uploadDropzone').addEventListener(name, event => {event.preventDefault(); if (!$('mediaInput').disabled) $('uploadDropzone').classList.add('dragover');});
  for (const name of ['dragleave', 'drop']) $('uploadDropzone').addEventListener(name, event => {event.preventDefault(); $('uploadDropzone').classList.remove('dragover'); if (name === 'drop') uploadFiles(event.dataTransfer.files);});

  for (const id of ['scheduleNow', 'scheduleLater']) $(id).addEventListener('change', () => {renderSchedule(); renderSummary();});
  for (const id of ['scheduledAt', 'commentMessage', 'commentDelaySeconds']) $(id).addEventListener('input', renderSummary);
  $('addDraftButton').addEventListener('click', () => {
    const hint = validation(); if (hint) {error(hint); return;}
    const post = snapshot();
    const next = state.editingId ? state.drafts.map(item => item.id === state.editingId ? post : item) : [...state.drafts, post];
    if (next.length > 100 || next.reduce((sum, item) => sum + item.page_ids.length, 0) > 1000) {error('Mỗi lượt gửi tối đa 100 bài và 1000 lượt bài × Fanpage.'); return;}
    const edited = !!state.editingId; state.drafts = next; error(''); clearComposer();
    notify(edited ? 'Đã lưu thay đổi bài trong danh sách.' : 'Đã thêm bài. Bạn có thể soạn bài khác cho cùng Fanpage.');
  });
  $('cancelEditButton').addEventListener('click', () => {clearComposer(); notify('Đã hủy thay đổi chưa lưu. Bài trong danh sách được giữ nguyên.');});

  function openPostReview(posts, isBatch, requestId = null) {
    const payloads = posts.map(postPayload), delay = Number($('delaySeconds').value);
    const signature = JSON.stringify({posts: payloads, delay_seconds: delay});
    if (requestId) state.reviewIdentity = {signature, request_id: requestId};
    else if (!state.reviewIdentity || state.reviewIdentity.signature !== signature) state.reviewIdentity = {signature, request_id: crypto.randomUUID()};
    state.pendingBatch = isBatch; state.pendingPosts = posts;
    state.pending = state.pendingBatch ? {request_id: state.reviewIdentity.request_id, posts: payloads, delay_seconds: delay} : {request_id: state.reviewIdentity.request_id, ...payloads[0], delay_seconds: delay};
    const total = posts.reduce((sum, post) => sum + post.page_ids.length, 0);
    const schedules = posts.filter(post => post.scheduled_at !== null).length;
    $('reviewHeading').textContent = state.demo ? 'Xác nhận chạy thử' : 'Xác nhận gửi bài & lịch';
    $('reviewDescription').textContent = (state.demo ? 'Mô phỏng ' : 'Gửi ') + posts.length + ' bài riêng biệt, ' + total + ' lượt bài × Fanpage.' + (state.pendingBatch ? ' Bài đang soạn chưa thêm sẽ không được gửi.' : '') + (state.demo ? ' Không gửi lên Facebook.' : '');
    $('reviewKind').textContent = posts.length === 1 ? kindNames[posts[0].kind] : posts.length + ' bài';
    $('reviewMedia').textContent = posts.reduce((sum, post) => sum + post.media_ids.length, 0) + ' tệp'; $('reviewDelay').textContent = delay + ' giây / lượt gửi';
    $('reviewMessage').hidden = true; $('reviewPages').hidden = true;
    const list = $('reviewPostList'); list.replaceChildren();
    posts.forEach((post, index) => {
      const card = element('article', 'review-post');
      card.append(element('h3', '', `Bài ${index+1} · ${kindNames[post.kind]} · ${post.page_ids.length} Fanpage`));
      if (post.title) card.append(element('p', '', 'Tiêu đề: ' + post.title));
      card.append(element('p', '', post.message || '(Không có nội dung văn bản)'));
      card.append(element('div', 'field-help', post.scheduled_at === null ? 'Đăng ngay khi gửi đến Meta' : 'Meta hẹn giờ: ' + displayTime(post.scheduled_at) + ' · ' + browserTimezone));
      if (post.media_ids.length) card.append(element('div', 'field-help', 'Tệp: ' + post.media_names.join(', ')));
      if (post.comment_message) {card.append(element('p', '', 'Bình luận: ' + post.comment_message)); card.append(element('div', 'field-help', 'Chờ ' + post.comment_delay_seconds + ' giây sau khi bài được đăng; cần tool đang chạy và kết nối.'));}
      const pages = element('div', 'review-pages');
      post.page_ids.forEach((id, i) => pages.append(element('span', 'review-page', post.page_names[i] || id))); card.append(pages); list.append(card);
    });
    $('reviewCaution').textContent = state.demo ? 'Bài, lịch hẹn và bình luận trong lượt này đều được mô phỏng.' : schedules ? 'Lịch sẽ gửi lên Meta ngay. Meta tự đăng khi máy tắt; bình luận cần tool mở và kết nối. Kiểm tra tất cả giờ hẹn và nội dung trước khi xác nhận.' : 'Kiểm tra nội dung và danh sách Fanpage. Bình luận chỉ gửi sau khi bài thực sự được đăng, khi tool đang mở và kết nối.';
    $('confirmPublish').textContent = state.demo ? 'Chạy thử ' + total + ' lượt' : 'Gửi ' + posts.length + ' bài · ' + total + ' lượt';
    $('reviewError').hidden = true; $('reviewDialog').showModal();
  }

  $('reviewButton').addEventListener('click', () => {
    const hint = reviewValidation(); if (hint) {error(hint); return;}
    const posts = state.drafts.length ? state.drafts.map(post => ({...post, page_ids: [...post.page_ids], media_ids: [...post.media_ids]})) : [snapshot()];
    openPostReview(posts, state.drafts.length > 0);
  });

  $('recoverSubmissionButton').addEventListener('click', async () => {
    $('recoverSubmissionButton').disabled = true;
    await refreshPlans();
    if (state.uncertainSubmission) openPostReview(state.uncertainSubmission.posts, state.uncertainSubmission.batch, state.uncertainSubmission.request_id);
    $('recoverSubmissionButton').disabled = false;
  });

  $('confirmPublish').addEventListener('click', async () => {
    if (!state.pending) return;
    for (const post of state.pending.posts || [state.pending]) {const issue = state.uncertainSubmission ? '' : scheduleError(post.scheduled_at); if (issue) {$('reviewError').textContent = issue + ' Quay lại để sửa giờ hẹn.'; $('reviewError').hidden = false; return;}}
    const submittedId = state.pending.request_id;
    const label = $('confirmPublish').textContent;
    $('confirmPublish').disabled = true; $('confirmPublish').textContent = 'Đang tạo hàng đợi…'; $('reviewError').hidden = true;
    try {
      const result = await request('/api/jobs', {method: 'POST', data: state.pending});
      state.job = result.job || result; state.viewJob = null; state.pending = null; state.reviewIdentity = null; state.uncertainSubmission = null; state.composerId = crypto.randomUUID(); if (state.pendingBatch) {state.drafts = []; state.editingId = null;} renderDrafts(); refreshPlans();
      $('reviewDialog').close(); error(''); renderJob();
      $('jobPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      notify(state.demo ? 'Đã bắt đầu mô phỏng bài, lịch và bình luận.' : 'Đã tiếp nhận danh sách. Tool đang gửi bài và lịch lên Meta.');
    } catch (err) {
      if (err.ambiguous) {
        state.uncertainSubmission = {request_id: submittedId, posts: state.pendingPosts, batch: state.pendingBatch};
        state.pending = null;
        $('reviewDialog').close();
        error('Chưa xác nhận được phản hồi tạo hàng đợi. Không gửi lại bài. Đang kiểm tra tiến trình…');
        try {
          const current = await request('/api/jobs/current'); state.job = current.job || null; state.viewJob = null; if (state.job && state.job.id === submittedId) reconcileSubmission(state.job); renderDrafts(); renderJob(); refreshPlans();
          error(state.job ? 'Đã tải lại tiến trình. Kiểm tra kết quả từng trang trước khi tạo lượt đăng mới.' : 'Chưa xác định được việc tạo hàng đợi. Kiểm tra Facebook trước khi tạo lượt đăng mới để tránh trùng bài.');
        } catch (_) {error('Có thể hàng đợi đã được tạo. Chờ kết nối khôi phục và kiểm tra tiến trình; không gửi lại bài lúc này.');}
      } else {state.uncertainSubmission = null; renderSummary(); $('reviewError').textContent = err.message; $('reviewError').hidden = false;}
    }
    finally {$('confirmPublish').disabled = false; $('confirmPublish').textContent = label;}
  });

  $('stopButton').addEventListener('click', async () => {
    $('stopButton').disabled = true;
    try {const result = await request('/api/jobs/stop', {method: 'POST', data: {}}); if (result.job) state.job = result.job; notify('Đã yêu cầu dừng. Lượt đang gửi có thể tiếp tục hoàn tất.'); renderJob();}
    catch (err) {error(err.message);}
    finally {$('stopButton').disabled = false;}
  });

  $('currentJobButton').addEventListener('click', () => {state.viewJob = null; renderJob();});

  $('exportButton').addEventListener('click', () => {
    const job = state.viewJob || state.job;
    if (!job) return;
    function cell(value) {let text = String(value ?? ''); if (/^[\s]*[=+@-]/.test(text)) text = "'" + text; return '"' + text.replace(/"/g, '""') + '"';}
    const rows = [['Bài số', 'Loại bài', 'ID Fanpage', 'Tên Fanpage', 'Giờ hẹn', 'Trạng thái bài', 'ID bài đăng', 'Lỗi bài', 'Lỗi kiểm tra xuất bản', 'Trạng thái bình luận', 'Giờ bình luận', 'ID bình luận', 'Lỗi bình luận'], ...(job.results || []).map(result => [(result.post_index ?? 0) + 1, kindNames[result.kind || job.kind] || '', result.page_id, result.page_name, result.scheduled_at == null ? 'Đăng ngay' : displayTime(result.scheduled_at), stateLabels[result.state] || result.state, result.post_id || '', result.error || '', result.publication_error || '', commentLabels[result.comment_state] || '', result.comment_due_at ? displayTime(result.comment_due_at) : '', result.comment_id || '', result.comment_error || ''])];
    const blob = new Blob(['\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const link = element('a'); const url = URL.createObjectURL(blob); link.href = url; link.download = 'pagepilot-ket-qua-' + String(job.id).replace(/[^a-zA-Z0-9_-]/g, '') + '.csv';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  async function poll() {
    try {
      const result = await request('/api/jobs/current');
      const oldJob = state.job;
      state.job = result.job || null; reconcileSubmission(state.job);
      if ((state.job && (!oldJob || JSON.stringify(state.job) !== JSON.stringify(oldJob))) || (!state.job && oldJob)) renderJob();
      if (state.job && (!oldJob || oldJob.id !== state.job.id || oldJob.state !== state.job.state)) {
        const history = await request('/api/history'); state.history = history.history || []; renderHistory(); refreshPlans();
      }
      if (state.pollError) {state.pollError = false; error('');}
    } catch (err) {if (!state.pollError) {state.pollError = true; error(err.message + ' Tiến trình sẽ tự kiểm tra lại.');}}
    finally {setTimeout(poll, 1200);}
  }

  renderSession(); renderHistory(); renderPlans();
  setInterval(refreshPlans, 7000);
  bootstrap().then(() => setTimeout(poll, 1200)).catch(err => {error(err.message); setTimeout(async () => {try {await bootstrap(); error('');} catch (retryError) {error(retryError.message);} poll();}, 2500);});
})();
