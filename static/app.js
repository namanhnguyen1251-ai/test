'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const kindNames = {text: 'Văn bản', photos: 'Ảnh', video: 'Video', reel: 'Reels'};
  const kindIcons = {text: 'text', photos: 'photo', video: 'video', reel: 'reel'};
  const finalStates = new Set(['published', 'processing', 'failed', 'uncertain', 'cancelled']);
  const stateLabels = {queued: 'Trong hàng đợi', running: 'Đang đăng', published: 'Đã đăng', processing: 'Meta đang xử lý', failed: 'Thất bại', uncertain: 'Cần kiểm tra', cancelled: 'Đã hủy'};
  const stateClasses = {queued: 'neutral', running: 'running', published: 'success', processing: 'processing', failed: 'error', uncertain: 'processing', cancelled: 'neutral'};
  const state = {csrf: '', connected: false, demo: false, graphVersion: '', pages: [], selected: new Set(), media: [], pickedMedia: new Set(), previews: new Map(), kind: 'text', job: null, viewJob: null, history: [], uploading: false, busy: false, pending: null, pollError: false};
  let toastTimer;

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
  const locked = () => running() || state.busy;
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

  function validation() {
    if (!active()) return 'Kết nối Meta App hoặc dùng thử để bắt đầu.';
    if (running()) return 'Đang xử lý một lượt đăng. Đợi hoàn tất hoặc dừng hàng đợi.';
    if (state.uploading) return 'Đang tải tệp. Vui lòng đợi tải lên hoàn tất.';
    if (!selectedPages().length) return 'Chọn ít nhất một Fanpage để đăng bài.';
    if (state.kind === 'text' && !$('postMessage').value.trim()) return 'Nhập nội dung cho bài viết của bạn.';
    const count = chosenMedia().length;
    if (state.kind === 'photos' && (!count || count > 10)) return 'Chọn từ 1 đến 10 ảnh cho bài viết.';
    if (['video', 'reel'].includes(state.kind) && count !== 1) return 'Chọn đúng một tệp video MP4.';
    const delay = Number($('delaySeconds').value);
    if (!Number.isInteger(delay) || delay < 1 || delay > 60) return 'Khoảng nghỉ cần là số nguyên từ 1 đến 60 giây.';
    return '';
  }

  function renderSummary() {
    $('summarySelected').textContent = selectedPages().length;
    $('summaryKind').textContent = kindNames[state.kind];
    const count = chosenMedia().length;
    $('summaryMedia').textContent = count ? count + (state.kind === 'photos' ? ' ảnh' : ' video') : '—';
    $('mediaCount').textContent = count + ' tệp đã chọn';
    const hint = validation();
    $('reviewButton').disabled = !!hint || state.busy;
    $('readyHint').textContent = hint || (state.demo ? 'Chạy thử để xem quy trình. Không đăng lên Facebook.' : 'Bạn sẽ xem lại và xác nhận trước khi đăng.');
    $('reviewButton').replaceChildren(document.createTextNode(state.demo ? 'Xem lại & chạy thử' : 'Xem lại & đăng bài'), icon('arrow'));
    $('postMessage').disabled = locked();
    $('videoTitle').disabled = locked();
    $('delaySeconds').disabled = locked();
    $('mediaInput').disabled = locked() || !active() || state.uploading;
    $('uploadDropzone').classList.toggle('disabled', $('mediaInput').disabled);
    document.querySelectorAll('.type-button').forEach(button => {button.disabled = locked() || state.uploading;});
    $('disconnectButton').disabled = locked() || state.uploading;
    $('connectButton').disabled = locked() || state.uploading || !state.csrf;
    $('demoButton').disabled = locked() || state.uploading || !state.csrf;
    $('demoConnectButton').disabled = locked() || state.uploading || !state.csrf;
  }

  function renderSession() {
    const badge = $('sessionBadge');
    badge.textContent = state.demo ? 'DÙNG THỬ' : state.connected ? 'Đã kết nối' : 'Chưa kết nối';
    badge.className = 'badge ' + (state.demo ? 'demo' : state.connected ? 'connected' : 'neutral');
    $('demoNotice').hidden = !state.demo;
    $('disconnectButton').hidden = !active();
    $('connectButton').hidden = state.connected;
    $('demoButton').hidden = active();
    $('connectionDetail').textContent = state.demo ? 'Phiên dùng thử · 50 Fanpage mẫu' : state.connected ? 'Meta Graph API ' + state.graphVersion : 'Chưa kết nối';
    if (/^v[1-9]\d?\.0$/.test(state.graphVersion)) $('graphVersion').value = state.graphVersion;
    renderPages(); renderSummary(); renderPreview(); renderMedia();
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
      remove.type = 'button'; remove.title = 'Xóa tệp ' + media.name; remove.setAttribute('aria-label', remove.title); remove.disabled = locked() || state.uploading; remove.append(icon('x'));
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
    if (job.kind === 'reel') return 'https://www.facebook.com/reel/' + encodeURIComponent(id);
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
    const published = count('published'), failed = count('failed'), processing = count('processing'), uncertain = count('uncertain'), cancelled = count('cancelled');
    const total = job.total || results.length;
    const badge = $('jobStateBadge');
    badge.textContent = job.state === 'running' ? 'Đang chạy' : job.state === 'stopped' ? 'Đã dừng' : 'Hoàn tất';
    badge.className = 'badge ' + (job.state === 'running' ? 'running' : job.state === 'stopped' ? 'neutral' : 'success');
    $('jobProgressText').textContent = completed + ' / ' + total + ' trang đã xử lý';
    const details = [published && published + ' đã đăng', processing && processing + ' Meta xử lý', failed && failed + ' lỗi', uncertain && uncertain + ' cần kiểm tra', cancelled && cancelled + ' hủy'].filter(Boolean);
    $('jobSummaryText').textContent = details.join(' · ') || 'Đang chuẩn bị hàng đợi';
    const progress = total ? Math.round(completed / total * 100) : 0;
    $('jobProgressFill').style.width = progress + '%'; $('jobProgress').setAttribute('aria-valuenow', String(progress));
    $('jobHeading').textContent = state.viewJob ? 'Kết quả lượt đăng trước' : 'Tiến trình đăng bài';
    $('currentJobButton').hidden = !state.viewJob || !state.job;
    $('stopButton').hidden = !!state.viewJob || job.state !== 'running';
    $('jobNote').textContent = uncertain ? 'Có kết quả chưa xác định. Kiểm tra trên Facebook trước khi đăng lại để tránh trùng bài.' : processing ? 'Meta đang xử lý tệp. Kiểm tra trên Facebook để xác nhận bài đã hiển thị trước khi đăng lại.' : job.state === 'running' ? 'Dừng hàng đợi sẽ hủy các trang chưa bắt đầu; lượt đang gửi có thể tiếp tục hoàn tất.' : job.demo ? 'Đây là kết quả mô phỏng. Không có bài viết nào được gửi lên Facebook.' : 'Kết quả được ghi riêng cho từng trang. Không tự động đăng lại các lượt gặp lỗi.';
    $('jobResults').replaceChildren();
    for (const result of results) {
      const row = element('tr');
      const name = element('td'); name.append(element('span', 'result-name', result.page_name || result.page_id), element('small', 'result-id', result.page_id));
      const status = element('td'); status.append(element('span', 'badge ' + (stateClasses[result.state] || 'neutral'), stateLabels[result.state] || result.state));
      const detail = element('td', 'result-detail' + (result.error ? ' error-text' : ''));
      if (result.error) detail.textContent = result.error;
      else if (result.post_id) {
        if (job.demo || String(result.post_id).startsWith('demo')) detail.textContent = 'Mô phỏng · ' + result.post_id;
        else {const link = element('a', '', 'ID · ' + result.post_id); link.href = postUrl(job, result); link.target = '_blank'; link.rel = 'noopener noreferrer'; detail.append(link);}
      } else detail.textContent = result.state === 'queued' ? 'Chờ đến lượt' : result.state === 'running' ? 'Đang gửi đến Meta…' : result.state === 'cancelled' ? 'Chưa gửi đến Meta' : '—';
      row.append(name, status, detail); $('jobResults').append(row);
    }
    renderSummary(); renderPages(); renderMedia();
  }

  function renderHistory() {
    const list = $('historyList'); list.replaceChildren();
    if (!state.history.length) {list.append(element('p', 'empty-history', 'Chưa có lượt đăng nào.')); return;}
    for (const job of state.history.slice(0, 4)) {
      const row = element('button', 'history-item'); row.type = 'button'; row.setAttribute('aria-label', 'Xem kết quả ' + (kindNames[job.kind] || 'bài viết') + ' trên ' + job.total + ' trang'); const graphic = element('span', 'history-item-icon'); graphic.append(icon(kindIcons[job.kind] || 'page'));
      const info = element('div'); info.append(element('strong', '', (kindNames[job.kind] || 'Bài viết') + ' · ' + (job.total || 0) + ' trang'));
      let time = 'Phiên hiện tại';
      if (job.created_at) {const date = new Date(job.created_at); if (!Number.isNaN(date.getTime())) time = date.toLocaleString('vi-VN', {hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit'});}
      info.append(element('small', '', time));
      const badge = element('span', 'badge ' + (job.state === 'running' ? 'running' : job.state === 'stopped' ? 'neutral' : 'success'), job.state === 'running' ? 'Đang chạy' : job.state === 'stopped' ? 'Đã dừng' : 'Xong');
      row.append(graphic, info, badge); list.append(row);
      row.addEventListener('click', () => {state.viewJob = state.job && state.job.id === job.id ? null : job; renderJob(); $('jobPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});});
    }
  }

  async function bootstrap() {
    const data = await request('/api/bootstrap');
    state.csrf = data.csrf_token; state.connected = !!data.connected; state.demo = !!data.demo;
    state.graphVersion = data.graph_version || ''; state.pages = data.pages || []; state.media = data.media || []; state.job = data.job || null; state.history = data.history || [];
    state.selected = new Set([...state.selected].filter(id => state.pages.some(page => page.id === id && publishable(page))));
    state.pickedMedia = new Set([...state.pickedMedia].filter(id => state.media.some(media => media.id === id)));
    renderSession(); renderJob(); renderHistory();
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

  $('reviewButton').addEventListener('click', () => {
    const hint = validation(); if (hint) {error(hint); return;}
    const pages = selectedPages();
    state.pending = {request_id: crypto.randomUUID(), page_ids: pages.map(page => page.id), kind: state.kind, message: $('postMessage').value.trim(), title: state.kind === 'video' ? $('videoTitle').value.trim() : '', media_ids: chosenMedia().map(media => media.id), delay_seconds: Number($('delaySeconds').value)};
    $('reviewHeading').textContent = state.demo ? 'Xác nhận chạy thử' : 'Xác nhận đăng bài';
    $('reviewDescription').textContent = state.demo ? 'Mô phỏng đăng cùng nội dung lên ' + pages.length + ' Fanpage mẫu. Không gửi bài lên Facebook.' : 'Nội dung dưới đây sẽ được gửi lên ' + pages.length + ' Fanpage bạn đã chọn.';
    $('reviewKind').textContent = kindNames[state.kind]; $('reviewMedia').textContent = chosenMedia().length + ' tệp'; $('reviewDelay').textContent = state.pending.delay_seconds + ' giây / trang';
    $('reviewMessage').textContent = state.pending.message || (state.pending.title ? 'Tiêu đề: ' + state.pending.title : '(Không có nội dung văn bản)');
    $('reviewPages').replaceChildren(); for (const page of pages) $('reviewPages').append(element('span', 'review-page', page.name));
    $('reviewCaution').textContent = state.demo ? 'Bạn có thể kiểm tra kết quả từng trang sau khi mô phỏng.' : 'Bài có thể hiển thị công khai theo cài đặt của Fanpage. Kiểm tra nội dung và danh sách trang trước khi xác nhận.';
    $('confirmPublish').textContent = state.demo ? 'Chạy thử ' + pages.length + ' Fanpage' : 'Đăng lên ' + pages.length + ' Fanpage';
    $('reviewError').hidden = true; $('reviewDialog').showModal();
  });

  $('confirmPublish').addEventListener('click', async () => {
    if (!state.pending) return;
    const label = $('confirmPublish').textContent;
    $('confirmPublish').disabled = true; $('confirmPublish').textContent = 'Đang tạo hàng đợi…'; $('reviewError').hidden = true;
    try {
      const result = await request('/api/jobs', {method: 'POST', data: state.pending});
      state.job = result.job || result; state.viewJob = null; state.pending = null;
      $('reviewDialog').close(); error(''); renderJob();
      $('jobPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      notify(state.demo ? 'Đã bắt đầu mô phỏng.' : 'Đã bắt đầu đăng bài theo hàng đợi.');
    } catch (err) {
      if (err.ambiguous) {
        state.pending = null;
        $('reviewDialog').close();
        error('Chưa xác nhận được phản hồi tạo hàng đợi. Không gửi lại bài. Đang kiểm tra tiến trình…');
        try {
          const current = await request('/api/jobs/current'); state.job = current.job || null; state.viewJob = null; renderJob();
          error(state.job ? 'Đã tải lại tiến trình. Kiểm tra kết quả từng trang trước khi tạo lượt đăng mới.' : 'Chưa xác định được việc tạo hàng đợi. Kiểm tra Facebook trước khi tạo lượt đăng mới để tránh trùng bài.');
        } catch (_) {error('Có thể hàng đợi đã được tạo. Chờ kết nối khôi phục và kiểm tra tiến trình; không gửi lại bài lúc này.');}
      } else {$('reviewError').textContent = err.message; $('reviewError').hidden = false;}
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
    const rows = [['ID Fanpage', 'Tên Fanpage', 'Trạng thái', 'ID bài đăng', 'Chi tiết'], ...(job.results || []).map(result => [result.page_id, result.page_name, stateLabels[result.state] || result.state, result.post_id || '', result.error || ''])];
    const blob = new Blob(['\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const link = element('a'); const url = URL.createObjectURL(blob); link.href = url; link.download = 'pagepilot-ket-qua-' + String(job.id).replace(/[^a-zA-Z0-9_-]/g, '') + '.csv';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  async function poll() {
    try {
      const result = await request('/api/jobs/current');
      const oldJob = state.job;
      state.job = result.job || null;
      if ((state.job && (!oldJob || JSON.stringify(state.job) !== JSON.stringify(oldJob))) || (!state.job && oldJob)) renderJob();
      if (state.job && (!oldJob || oldJob.id !== state.job.id || oldJob.state !== state.job.state)) {
        const history = await request('/api/history'); state.history = history.history || []; renderHistory();
      }
      if (state.pollError) {state.pollError = false; error('');}
    } catch (err) {if (!state.pollError) {state.pollError = true; error(err.message + ' Tiến trình sẽ tự kiểm tra lại.');}}
    finally {setTimeout(poll, 1200);}
  }

  renderSession(); renderHistory();
  bootstrap().then(() => setTimeout(poll, 1200)).catch(err => {error(err.message); setTimeout(async () => {try {await bootstrap(); error('');} catch (retryError) {error(retryError.message);} poll();}, 2500);});
})();
