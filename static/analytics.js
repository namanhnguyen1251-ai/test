'use strict';

(() => {
  const $ = id => document.getElementById(id);
  if (!$('analyticsPanel')) return;
  const keys = ['views', 'engagements', 'followers', 'new_followers'];
  const labels = {views: 'Lượt xem nội dung', engagements: 'Tương tác bài viết', followers: 'Người theo dõi hiện tại', new_followers: 'Theo dõi mới (trong kỳ)'};
  const presets = {today: 'Hôm nay', '7days': '7 ngày gần nhất', '30days': '30 ngày gần nhất'};
  const metricLabels = {ok: 'Đủ dữ liệu', partial: 'Dữ liệu một phần', unavailable: 'Không khả dụng', error: 'Lỗi lấy dữ liệu'};
  const rowLabels = {queued: 'Chờ tải', loading: 'Đang tải', ok: 'Đã tải', partial: 'Một phần', error: 'Có lỗi', not_loaded: 'Chưa tải'};
  const model = {csrf: '', initialized: false, initializing: false, initAttempts: 0, initTimer: null, revision: null, session: {connected: false, demo: false, pages: []}, report: null, generation: 0, busy: false, fetching: false, timer: null, search: '', sort: 'page_name', direction: 'asc', tab: 'posting'};
  const number = value => Number.isFinite(value) ? new Intl.NumberFormat('vi-VN').format(value) : '—';
  const hasValue = value => Number.isFinite(value);

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function safeText(value) {
    return String(value ?? '').replace(/https?:\/\/[^\s<>"']+/gi, '[đường dẫn]');
  }

  function formatTime(value) {
    if (!value) return 'Chưa có';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Chưa xác định' : date.toLocaleString('vi-VN', {day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'});
  }

  function showError(message) {
    $('analyticsError').textContent = safeText(message);
    $('analyticsError').hidden = !message;
    if (message && !model.initialized) {
      const retry = node('button', 'button button-ghost button-small', 'Thử lại kết nối thống kê');
      retry.type = 'button'; retry.addEventListener('click', () => {model.initAttempts = 0; initialize();});
      $('analyticsError').append(retry);
    }
  }

  async function api(path, {method = 'GET', data} = {}) {
    const options = {method, credentials: 'same-origin', cache: 'no-store', headers: {}};
    if (method !== 'GET') {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = model.csrf;
      options.body = JSON.stringify(data || {});
    }
    let response;
    try {response = await fetch(path, options);}
    catch (_) {throw new Error('Không kết nối được với ứng dụng trên máy. Kiểm tra cửa sổ chạy PagePilot.');}
    let dataResponse;
    try {dataResponse = await response.json();}
    catch (_) {throw new Error('Ứng dụng trả về phản hồi thống kê không hợp lệ.');}
    if (!response.ok) throw new Error(dataResponse.error || 'Chưa lấy được dữ liệu thống kê.');
    return dataResponse;
  }

  function unwrap(data) {
    return data && Object.prototype.hasOwnProperty.call(data, 'report') ? data.report : data && data.id ? data : null;
  }

  function reportLabel() {
    const report = model.report;
    return report ? report.range?.label || presets[report.preset] || 'Kỳ đã lưu' : presets[$('analyticsPreset').value];
  }

  function metricInfo(metric, key) {
    if (!metric) return labels[key] + ': chưa tải dữ liệu.';
    const scope = metric.scope === 'current' || key === 'followers' ? 'ảnh chụp hiện tại' : 'trong kỳ';
    const parts = [labels[key] + ' · ' + scope, metricLabels[metric.status] || 'Chưa tải'];
    if (metric.source) parts.push('Chỉ số Meta: ' + safeText(metric.source));
    if (Number.isInteger(metric.data_points)) {
      const days = model.report?.range?.days;
      parts.push(metric.scope !== 'current' && key !== 'followers' && days ? `${metric.data_points}/${days} mốc dữ liệu ngày` : metric.data_points + ' mốc dữ liệu');
    }
    if (metric.first_end_time) parts.push('Mốc đầu: ' + formatTime(metric.first_end_time));
    if (metric.last_end_time) parts.push('Mốc cuối: ' + formatTime(metric.last_end_time));
    return parts.join(' · ');
  }

  function filteredRows({includeAll = false} = {}) {
    const query = model.search.trim().toLocaleLowerCase('vi');
    const rows = (model.report?.rows || []).filter(row => includeAll || !query || String(row.page_name).toLocaleLowerCase('vi').includes(query) || String(row.page_id).includes(query));
    return [...rows].sort((a, b) => {
      if (model.sort === 'page_name') {
        const comparison = String(a.page_name || '').localeCompare(String(b.page_name || ''), 'vi', {numeric: true, sensitivity: 'base'});
        return model.direction === 'asc' ? comparison : -comparison;
      }
      const left = a[model.sort], right = b[model.sort];
      if (!hasValue(left) && !hasValue(right)) return String(a.page_name || '').localeCompare(String(b.page_name || ''), 'vi');
      if (!hasValue(left)) return 1;
      if (!hasValue(right)) return -1;
      return model.direction === 'asc' ? left - right : right - left;
    });
  }

  function renderCards() {
    const report = model.report;
    const total = report?.total_pages ?? model.session.pages.length;
    for (const key of keys) {
      const metric = report?.summary?.[key];
      const value = document.querySelector(`[data-analytics-value="${key}"]`);
      const coverage = document.querySelector(`[data-analytics-coverage="${key}"]`);
      const badge = document.querySelector(`[data-analytics-status="${key}"]`);
      const card = document.querySelector(`[data-analytics-card="${key}"]`);
      value.textContent = number(metric?.value);
      const available = metric?.available_pages ?? 0;
      const partial = metric?.partial_pages ?? 0;
      coverage.textContent = `${available}/${metric?.total_pages ?? total}`;
      coverage.title = `${available}/${metric?.total_pages ?? total} Fanpage có dữ liệu` + (partial ? ` · ${partial} trang có dữ liệu một phần` : '') + ' · ' + metricInfo(metric, key);
      const status = metric?.status;
      badge.textContent = metric ? (metricLabels[status] || (available ? 'Đã tải' : 'Không có dữ liệu')) + (partial ? ' · ' + partial + ' trang' : '') : 'Chưa tải';
      badge.className = 'analytics-card-status ' + (status === 'ok' ? 'success' : status === 'partial' ? 'partial' : status === 'error' ? 'error' : 'neutral');
      card.title = metricInfo(metric, key) + (key === 'followers' ? ' · Tổng hiện tại không phụ thuộc kỳ xem.' : key === 'new_followers' ? ' · Tổng lượt theo dõi mới, không trừ lượt bỏ theo dõi.' : '');
    }
  }

  function renderRows() {
    const body = $('analyticsRows');
    const opened = new Set([...body.querySelectorAll('details[open]')].map(item => item.dataset.pageId));
    body.replaceChildren();
    const rows = filteredRows();
    const total = model.report?.rows?.length || 0;
    $('analyticsRowCount').textContent = rows.length + '/' + total + ' Fanpage';
    if (!rows.length) {
      const row = node('tr'), cell = node('td', 'analytics-empty');
      cell.append(node('strong', '', total ? 'Không tìm thấy Fanpage' : 'Thống kê sẽ xuất hiện ở đây'), node('p', '', total ? 'Không có Fanpage khớp từ khóa.' : model.session.connected || model.session.demo ? 'Chọn kỳ xem rồi bấm Cập nhật dữ liệu để tải toàn bộ Fanpage.' : 'Kết nối Meta App hoặc dùng thử để tải thống kê Fanpage.'));
      cell.colSpan = 6; row.append(cell); body.append(row);
    }
    for (const rowData of rows) {
      const row = node('tr');
      const name = node('td');
      name.append(node('strong', 'analytics-page-name', rowData.page_name || 'Fanpage'), node('small', 'analytics-page-id', 'ID · ' + rowData.page_id));
      row.append(name);
      for (const key of keys) {
        const cell = node('td');
        cell.append(node('span', 'analytics-number' + (!hasValue(rowData[key]) ? ' analytics-missing' : ''), number(rowData[key])));
        const metric = rowData.metrics?.[key];
        cell.title = metricInfo(metric, key);
        if (metric?.status === 'partial' && hasValue(rowData[key])) {
          const partial = node('span', 'analytics-partial-marker', '*');
          partial.setAttribute('aria-label', 'Dữ liệu một phần'); cell.append(partial);
        }
        row.append(cell);
      }
      const detailCell = node('td', 'analytics-details');
      const classification = rowData.status === 'ok' ? 'success' : rowData.status === 'partial' ? 'processing' : rowData.status === 'error' ? 'error' : rowData.status === 'loading' ? 'running' : 'neutral';
      detailCell.append(node('span', 'badge', rowLabels[rowData.status] || 'Chưa tải'));
      detailCell.firstChild.classList.add(classification);
      if (rowData.status !== 'queued' && rowData.status !== 'loading' && rowData.status !== 'not_loaded') {
        const details = node('details', 'analytics-row-details');
        details.dataset.pageId = String(rowData.page_id);
        details.open = opened.has(String(rowData.page_id));
        const issues = rowData.issues || [];
        details.append(node('summary', '', issues.length ? issues.length + ' vấn đề · xem chi tiết' : 'Nguồn & độ phủ'));
        if (issues.length) {
          const list = node('ul', 'analytics-issues');
          for (const issue of issues) list.append(node('li', '', (labels[issue.metric] || safeText(issue.metric) || 'Dữ liệu') + ': ' + safeText(issue.message)));
          details.append(list);
        }
        const diagnostics = node('div', 'analytics-metric-notes');
        for (const key of keys) diagnostics.append(node('p', '', metricInfo(rowData.metrics?.[key], key)));
        if (rowData.collected_at) diagnostics.append(node('p', '', 'Thu thập: ' + formatTime(rowData.collected_at)));
        details.append(diagnostics); detailCell.append(details);
      }
      row.append(detailCell); body.append(row);
    }
    document.querySelectorAll('[data-analytics-sort]').forEach(button => {
      const selected = button.dataset.analyticsSort === model.sort;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-label', button.dataset.label + (selected ? (model.direction === 'asc' ? ', tăng dần' : ', giảm dần') : ', bấm để sắp xếp'));
      button.closest('th').setAttribute('aria-sort', selected ? model.direction === 'asc' ? 'ascending' : 'descending' : 'none');
    });
  }

  function render() {
    const report = model.report;
    const running = report?.state === 'running';
    $('analyticsRefresh').disabled = !model.initialized || !model.csrf || !(model.session.connected || model.session.demo) || model.busy || running;
    $('analyticsRefresh').textContent = model.busy ? 'Đang bắt đầu…' : 'Cập nhật dữ liệu';
    $('analyticsStop').hidden = !running;
    $('analyticsStop').disabled = model.busy;
    $('analyticsPreset').disabled = model.busy || running;
    $('analyticsExport').disabled = !(report?.rows?.length);
    $('analyticsExport').title = 'Xuất toàn bộ Fanpage; chỉ số chưa có được để trống.';
    const badge = $('analyticsModeBadge');
    if (report?.demo || (!report && model.session.demo)) {badge.textContent = 'DỮ LIỆU MẪU'; badge.className = 'badge demo';}
    else if (report?.cached) {badge.textContent = 'DỮ LIỆU ĐÃ LƯU'; badge.className = 'badge neutral';}
    else {badge.textContent = 'PAGE INSIGHTS'; badge.className = 'badge connected';}
    $('analyticsUpdated').textContent = report ? (report.cached ? 'Bản lưu gần nhất · ' : 'Cập nhật · ') + formatTime(report.updated_at || report.finished_at || report.started_at) : 'Chưa tải chỉ số';
    const total = report?.total_pages ?? model.session.pages.length;
    const completed = report?.completed_pages ?? 0;
    $('analyticsProgressText').textContent = report ? `${completed}/${total} Fanpage đã xử lý` + (running ? ' · Đang tải' : report.state === 'cancelled' ? ' · Đã dừng' : ' · Hoàn tất') : `${total} Fanpage trong kết nối · chưa tải dữ liệu`;
    const progress = total ? Math.min(100, Math.round(completed / total * 100)) : 0;
    $('analyticsProgressFill').style.width = progress + '%';
    $('analyticsProgress').setAttribute('aria-valuenow', String(progress));
    let notice = report ? 'Đang hiển thị ' + reportLabel() + ' cho toàn bộ Fanpage, độc lập với các trang chọn để đăng bài.' : 'Thống kê toàn bộ Fanpage trong kết nối, độc lập với lựa chọn ở màn hình đăng bài.';
    if (report?.demo) notice += ' Đây là dữ liệu mô phỏng, không lấy từ Facebook.';
    if (report?.cached) notice += ' Đây là bản đã lưu, không phải dữ liệu vừa tải.';
    if (report && $('analyticsPreset').value !== report.preset) notice += ' Bạn đã chọn kỳ khác; bấm Cập nhật dữ liệu để tải kỳ mới.';
    if (!(model.session.connected || model.session.demo)) notice += ' Kết nối Meta App để cập nhật.';
    $('analyticsNotice').textContent = notice;
    renderCards(); renderRows();
  }

  function clearTimer() {
    clearTimeout(model.timer); model.timer = null;
  }

  function schedulePoll(generation) {
    clearTimer();
    if (model.report?.state !== 'running' || generation !== model.generation) return;
    model.timer = setTimeout(() => poll(generation), 1500);
  }

  async function poll(generation) {
    if (generation !== model.generation || model.report?.state !== 'running') return;
    try {
      const result = await api('/api/analytics');
      if (generation !== model.generation) return;
      model.report = unwrap(result); showError(''); render();
    } catch (err) {if (generation === model.generation) showError(err.message + ' Ứng dụng sẽ kiểm tra lại tiến trình.');}
    finally {schedulePoll(generation);}
  }

  async function fetchSnapshot() {
    const generation = model.generation;
    try {
      const result = await api('/api/analytics');
      if (generation !== model.generation) return;
      model.report = unwrap(result);
      if (model.report?.preset) $('analyticsPreset').value = model.report.preset;
      showError(''); render(); schedulePoll(generation);
    } catch (err) {if (generation === model.generation) {showError(err.message); render();}}
  }

  function applySession(detail) {
    if (!detail || detail.revision === undefined || detail.revision === null) return;
    const changed = model.revision !== detail.revision;
    model.session = {connected: !!detail.connected, demo: !!detail.demo, pages: detail.pages || []};
    if (!changed) {render(); return;}
    model.revision = detail.revision; model.generation++; model.report = null; model.busy = false;
    clearTimer(); showError(''); render();
    if (model.initialized) fetchSnapshot();
    else if (!model.initializing) initialize();
  }

  function activateTab(tab, {focus = false} = {}) {
    model.tab = tab;
    const analytics = tab === 'analytics';
    $('postingPanel').hidden = analytics; $('analyticsPanel').hidden = !analytics;
    for (const [id, chosen] of [['postingTab', !analytics], ['analyticsTab', analytics]]) {
      $(id).setAttribute('aria-selected', String(chosen)); $(id).tabIndex = chosen ? 0 : -1;
      $(id).classList.toggle('active', chosen); if (chosen && focus) $(id).focus();
    }
  }

  for (const [id, tab] of [['postingTab', 'posting'], ['analyticsTab', 'analytics']]) {
    $(id).addEventListener('click', () => activateTab(tab));
    $(id).addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === 'Home' ? 'posting' : event.key === 'End' ? 'analytics' : model.tab === 'analytics' ? 'posting' : 'analytics';
      activateTab(target, {focus: true});
    });
  }
  $('analyticsPreset').addEventListener('change', render);
  $('analyticsSearch').addEventListener('input', event => {model.search = event.target.value; renderRows();});
  document.querySelectorAll('[data-analytics-sort]').forEach(button => {
    button.dataset.label = button.textContent.trim();
    button.addEventListener('click', () => {
      const key = button.dataset.analyticsSort;
      model.direction = model.sort === key ? model.direction === 'asc' ? 'desc' : 'asc' : key === 'page_name' ? 'asc' : 'desc';
      model.sort = key; renderRows();
    });
  });

  $('analyticsRefresh').addEventListener('click', async () => {
    if (model.busy || model.report?.state === 'running') return;
    const generation = ++model.generation; model.busy = true; clearTimer(); showError(''); render();
    try {
      const result = await api('/api/analytics/refresh', {method: 'POST', data: {preset: $('analyticsPreset').value}});
      if (generation !== model.generation) return;
      model.report = unwrap(result); render(); schedulePoll(generation);
    } catch (err) {
      if (generation !== model.generation) return;
      const message = err.message + ' Kiểm tra quyền đọc chỉ số và thử cập nhật lại khi sẵn sàng.';
      showError(message);
      // A lost response may still have started the read-only collection; inspect its local state.
      await fetchSnapshot();
      if (generation === model.generation && model.report?.state !== 'running') showError(message);
    } finally {if (generation === model.generation) {model.busy = false; render();}}
  });

  $('analyticsStop').addEventListener('click', async () => {
    const generation = ++model.generation; model.busy = true; clearTimer(); render();
    try {
      const result = await api('/api/analytics/stop', {method: 'POST', data: {}});
      if (generation !== model.generation) return;
      clearTimer(); model.report = unwrap(result); render();
    } catch (err) {if (generation === model.generation) showError(err.message);}
    finally {if (generation === model.generation) {model.busy = false; render(); schedulePoll(generation);}}
  });

  $('analyticsExport').addEventListener('click', () => {
    const report = model.report; if (!report) return;
    function cell(value) {
      let text = String(value ?? '').replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '');
      if (/^[\s]*[=+@-]/.test(text)) text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    }
    const rows = [
      ['PagePilot · Thống kê Fanpage'], ['Loại dữ liệu', report.demo ? 'Mô phỏng' : 'Meta Page Insights'],
      ['Kỳ', report.preset, reportLabel()], ['Cập nhật (UTC)', report.updated_at || report.finished_at || report.started_at || ''],
      ['Trạng thái báo cáo', report.state, report.cached ? 'Bản đã lưu' : ''],
      ['Phạm vi', 'Toàn bộ Fanpage trong báo cáo'],
      ['Lưu ý', 'Người theo dõi hiện tại là ảnh chụp tại thời điểm thu thập. Theo dõi mới là tổng lượt mới, không trừ lượt bỏ theo dõi. Chỉ số thiếu để trống.'], [],
      ['ID Fanpage (văn bản)', 'Tên Fanpage', ...keys.map(key => labels[key]), 'Trạng thái', 'Vấn đề', ...keys.map(key => labels[key] + ' · nguồn & độ phủ'), 'Thu thập (UTC)'],
      ...filteredRows({includeAll: true}).map(row => ["'" + String(row.page_id), row.page_name, ...keys.map(key => hasValue(row[key]) ? row[key] : ''), rowLabels[row.status] || row.status, (row.issues || []).map(issue => (labels[issue.metric] || issue.metric || '') + ': ' + safeText(issue.message)).join(' | '), ...keys.map(key => metricInfo(row.metrics?.[key], key)), row.collected_at || ''])
    ];
    const blob = new Blob(['\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const link = node('a'), url = URL.createObjectURL(blob);
    link.href = url; link.download = 'pagepilot-thong-ke-' + String(report.preset || '30days').replace(/[^a-z0-9]/gi, '') + '-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  window.addEventListener('pagepilot:session', event => applySession(event.detail));
  async function initialize() {
    if (model.initialized || model.initializing) return;
    clearTimeout(model.initTimer); model.initTimer = null;
    model.initializing = true; model.initAttempts++;
    try {
      const bootstrap = await api('/api/bootstrap'); model.csrf = bootstrap.csrf_token || '';
      if (model.revision === null) applySession({connected: bootstrap.connected, demo: bootstrap.demo, revision: bootstrap.session_revision ?? 0, pages: (bootstrap.pages || []).map(page => ({id: page.id, name: page.name}))});
      model.initialized = true; render(); await fetchSnapshot();
    } catch (err) {showError(err.message); render();}
    finally {
      model.initializing = false;
      // Retry failed startup only; successful initialization never polls bootstrap.
      if (!model.initialized && model.initAttempts < 3) model.initTimer = setTimeout(initialize, 1500 * model.initAttempts);
    }
  }

  render(); initialize();
})();
