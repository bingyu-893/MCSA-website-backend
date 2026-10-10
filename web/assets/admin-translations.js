/* English drafts are separate from Chinese content and never publish on typing. */
(() => {
  'use strict';
  window.MCSATranslations = function (host) {
    const {esc, api, status} = host;
    let snapshot = {fields: {}, glossary: {version: 1, terms: []}}, filter = 'all';
    const edits = new Map();
    let termDraft = null, termVersion = null, busy = false;
    function lock(value) { busy = value; const cms = document.querySelector('.cms'); if (cms) cms.inert = value; }
    function publishedText(record) {
      return record.source.trim() ? record.protected || (record.candidate_source === record.source ? record.candidate : '') : '';
    }
    const labels = {pending: '待翻译', failed: '翻译失败', review: '待校对', stale: '中文已更新，待复核', legacy: '历史译文待确认', approved: '已批准', empty: '空内容'};
    const errors = {not_configured: '英文翻译服务未配置；请在服务器设置 Azure 密钥。', network_error: '网络连接失败，系统将有限次重试。', service_unavailable: '翻译服务繁忙，系统将有限次重试。', service_configuration: '翻译服务配置有误，请检查服务器密钥与区域。', invalid_response: '翻译结果格式或模板占位符不正确，请重试或人工填写。'};
    function editTerms() {
      if (termDraft === null) { termDraft = structuredClone(snapshot.glossary.terms); termVersion = snapshot.glossary.version; }
    }
    function stateOf(record) {
      return record.protected && record.protected_source !== record.source ? 'stale' : record.legacy ? 'legacy' : record.status;
    }
    function textFor(record) {
      return edits.get(record.id) ?? record.draft ?? (record.candidate_source === record.source && record.candidate ? record.candidate : record.protected || '');
    }
    function panel(id) {
      const record = snapshot.fields[id];
      if (!record) return '<p class="cms-hint">保存中文后生成英文候选。</p>';
      const candidateCurrent = record.candidate_source === record.source;
      return `<div class="translation-editor" data-translation-editor="${esc(id)}">
        <p class="translation-state">${labels[stateOf(record)]}${record.status === 'pending' ? ' · 正在等待翻译' : ''}${record.draft !== null ? ' · 有人工草稿' : ''}</p>
        ${record.error ? `<p class="error">${esc(errors[record.error] || '翻译失败，请重试。')}</p>` : ''}
        ${record.reviewed_by ? `<small>上次批准：${esc(record.reviewed_by)} · ${esc(new Date(record.reviewed_at * 1000).toLocaleString())}</small>` : ''}
        <label class="cms-field">当前公开英文<textarea readonly>${esc(publishedText(record))}</textarea></label>
        <details data-open-key="candidate-${id}"><summary>机翻候选${!candidateCurrent && record.candidate ? '（对应旧中文）' : ''}</summary><p class="translation-copy">${esc(record.candidate || '尚未生成')}</p></details>
        <label class="cms-field">英文校对稿<textarea data-english="${esc(id)}" maxlength="12000">${esc(textFor(record))}</textarea></label>
        ${record.draft !== null && record.draft_source !== record.source ? '<p class="error">人工草稿对应旧中文，请对照当前中文重新检查。</p>' : ''}
        <div class="translation-actions"><button type="button" data-translation-action="adopt" data-id="${id}" ${!record.candidate || !candidateCurrent ? 'disabled' : ''}>采用机翻</button><button type="button" data-translation-action="draft" data-id="${id}">保存英文草稿</button><button type="button" data-translation-action="approve" data-id="${id}">批准并发布</button><button type="button" data-translation-action="retry" data-id="${id}">重新翻译</button></div>
      </div>`;
    }
    function review() {
      const all = Object.values(snapshot.fields).filter(r => r.source.trim());
      const shown = all.filter(r => filter === 'all' || stateOf(r) === filter || r.status === filter);
      return `<p>先展示机翻，批准后展示人工稿。已有人工稿受保护；中文更新后需重新校对。</p>
        <div class="translation-actions"><label>状态 <select id="translation-filter"><option value="all">全部</option>${Object.entries(labels).filter(([key]) => key !== 'empty').map(([key,label]) => `<option value="${key}" ${filter===key?'selected':''}>${label}</option>`).join('')}</select></label><button type="button" id="refresh-translations">刷新翻译状态</button><button type="button" id="retry-missing">补译待处理／失败项</button><button type="button" id="approve-selected">批准已勾选校对稿</button></div>
        <p>${shown.length} 项${snapshot.configured ? '' : ' · Azure 翻译服务未配置'}</p>
        ${shown.map(r => `<details class="editor-card" data-open-key="review-${r.id}"><summary>${esc(r.source.slice(0, 70))} · ${labels[stateOf(r)]}</summary><div class="editor-body"><label><input type="checkbox" data-review-select="${r.id}">已检查此项，可批量批准</label><small>${esc(r.path.join(' / '))}</small><p class="translation-copy">${esc(r.source)}</p>${panel(r.id)}</div></details>`).join('')}`;
    }
    function terms() {
      const list = termDraft || snapshot.glossary.terms;
      return `<p>启用经确认的部门、活动及商家名称。术语更新会生成新候选，不覆盖人工稿；中文名称必须唯一。</p><div class="translation-actions"><button type="button" id="add-term">添加术语</button><button type="button" id="save-terms">保存术语表</button><button type="button" id="reload-terms">重新载入术语表</button></div>${list.map((term, i) => `<div class="glossary-row"><label>中文名称<input data-term="${i}" data-key="zh" maxlength="200" value="${esc(term.zh)}"></label><label>指定英文<input data-term="${i}" data-key="en" maxlength="200" value="${esc(term.en)}"></label><label><input type="checkbox" data-term="${i}" data-key="enabled" ${term.enabled ? 'checked' : ''}>已确认并启用</label><button type="button" data-remove-term="${i}">删除</button></div>`).join('')}`;
    }
    function guard() {
      if (busy) return false;
      if (host.isDirty()) {
        status('请先保存中文内容，再操作翻译；未保存的英文校对稿会保留。', true);
        return false;
      }
      return host.can('site.write');
    }
    async function action(kind, ids) {
      if (!guard() || !ids.length) return;
      const entries = ids.map(id => ({id, version: snapshot.fields[id].version, text: textFor(snapshot.fields[id])}));
      lock(true);
      try {
        const result = await api('/admin/translations', {method: 'POST', body: JSON.stringify({action: kind, entries})});
        if (kind !== 'retry') ids.forEach(id => edits.delete(id));
        host.accept(result);
        status(kind === 'approve' ? '英文已批准并发布。' : kind === 'draft' ? '英文草稿已保存，尚未发布。' : '已加入翻译队列；稍后刷新状态查看结果。');
      } catch (error) { status(error.message, true); }
      finally { lock(false); }
    }
    function bind() {
      document.querySelectorAll('[data-english]').forEach(input => input.oninput = () => { edits.set(input.dataset.english, input.value); status('英文校对稿有未保存修改；保存草稿不会发布。'); });
      document.querySelectorAll('[data-translation-action]').forEach(button => button.onclick = () => {
        const id = button.dataset.id, kind = button.dataset.translationAction;
        if (kind === 'adopt') {
          edits.set(id, snapshot.fields[id].candidate);
          host.render();
          status('机翻已放入校对框，检查后保存草稿或批准发布。');
        } else action(kind, [id]);
      });
      document.querySelectorAll('[data-approve-entry]').forEach(button => button.onclick = () => {
        const prefix = button.dataset.approveEntry + '.';
        const ids = Object.values(snapshot.fields).filter(r => (r.path.join('.') + '.').startsWith(prefix) && r.source.trim()).map(r => r.id);
        action('approve', ids);
      });
      const select = document.querySelector('#translation-filter');
      if (select) select.onchange = () => { filter = select.value; host.render(); };
      const refresh = document.querySelector('#refresh-translations');
      if (refresh) refresh.onclick = async () => {
        if (busy) return;
        if (host.isDirty()) return status('请先保存中文修改再刷新。', true);
        lock(true);
        try { host.accept(await api('/admin/site')); status('翻译状态已刷新；本地英文编辑已保留。'); } catch (error) { status(error.message, true); }
        finally { lock(false); }
      };
      const retry = document.querySelector('#retry-missing');
      if (retry) retry.onclick = () => action('retry', Object.values(snapshot.fields).filter(r => ['pending','failed'].includes(r.status)).map(r => r.id));
      const approve = document.querySelector('#approve-selected');
      if (approve) approve.onclick = () => action('approve', [...document.querySelectorAll('[data-review-select]:checked')].map(el => el.dataset.reviewSelect));
      const add = document.querySelector('#add-term');
      if (add) add.onclick = () => { editTerms(); termDraft.push({zh:'', en:'', enabled:false}); host.render(); };
      document.querySelectorAll('[data-term]').forEach(input => input.oninput = () => {
        editTerms();
        termDraft[Number(input.dataset.term)][input.dataset.key] = input.type === 'checkbox' ? input.checked : input.value;
      });
      document.querySelectorAll('[data-remove-term]').forEach(button => button.onclick = () => {
        editTerms();
        termDraft.splice(Number(button.dataset.removeTerm), 1); host.render();
      });
      const reloadTerms = document.querySelector('#reload-terms');
      if (reloadTerms) reloadTerms.onclick = async () => {
        if (busy || host.isDirty()) return status('请先保存中文修改。', true);
        if (termDraft !== null && !confirm('放弃本地术语修改，重新载入最新术语表？')) return;
        lock(true);
        try { const result = await api('/admin/site'); termDraft = null; termVersion = null; host.accept(result); }
        catch (error) { status(error.message, true); }
        finally { lock(false); }
      };
      const save = document.querySelector('#save-terms');
      if (save) save.onclick = async () => {
        if (!guard()) return;
        lock(true);
        try {
          const result = await api('/admin/translation-glossary', {method:'PUT', body:JSON.stringify({version:termVersion ?? snapshot.glossary.version, terms:termDraft || snapshot.glossary.terms})});
          termDraft = null; termVersion = null; host.accept(result); status('术语已保存，受影响内容已加入翻译队列。');
        } catch (error) { status(error.message, true); }
        finally { lock(false); }
      };
    }
    function preview(value, mode) {
      const copy = structuredClone(value);
      function walk(node) {
        if (!node || typeof node !== 'object') return;
        if ('zh' in node && 'en' in node) {
          const record = snapshot.fields[node._translationId];
          if (record && mode === 'candidate') node.en = textFor(record);
          if (record && mode !== 'candidate') {
            node.en = publishedText(record);
            node._translationStatus = record.protected ? (record.protected_source !== record.source ? 'stale' : '') : node.en ? 'machine' : 'pending';
          }
        } else Object.values(node).forEach(walk);
      }
      walk(copy);
      return copy;
    }
    return {panel, review, terms, bind, preview, receive: data => { snapshot = data || snapshot; },
      dirty: () => edits.size > 0 || termDraft !== null};
  };
})();
