import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const restoreStatus = {restored:'已恢复', failed:'已拒绝'};

function backupPanel(state, selected) {
  const rows = state.backups.map(row=>[`<a href="#drill/${row.id}" ${row.id===selected?.id?'aria-current="true"':''}>备份 ${esc(row.snapshot_id.slice(0,8))}</a>`,
    esc(row.actor), esc(date(row.created_at)), esc(row.counts.runs ?? 0)]);
  return records(['快照','经办','备份时间','执行数'], rows, '尚无备份。经办员建立备份后，可在演练期间照常提交与签署。');
}

function restoreList(state, backup) {
  const related = state.restores.filter(row=>backup && row.backup_id===backup.id);
  return records(['目标库','状态','说明'], related.map(row=>[
    `<a href="#drill/${backup.id}/${row.id}">${esc(row.target || '—')}</a>`,
    esc(restoreStatus[row.status] || row.status),
    esc(row.status==='restored' ? row.report.summary : (row.error || '整份恢复被拒绝，目标空库保持原样'))]),
    '该备份还没有恢复演练记录。');
}

function detail(state, selected) {
  if (!selected) return '';
  const counts = selected.counts || {};
  return `<section class="panel"><h2>备份 ${esc(selected.snapshot_id)}</h2>
    <dl class="brief"><dt>捕获时点</dt><dd>${esc(selected.taken_at || date(selected.created_at))}</dd>
    <dt>校验码</dt><dd><code class="digest">${esc(selected.checksum)}</code></dd></dl>
    <p class="note">登录批次 ${esc(counts.cases ?? 0)} · 执行 ${esc(counts.runs ?? 0)} · 上架结果 ${esc(counts.evidence ?? 0)}。备份在只读快照事务中读取，队列与签署与快照来自同一时点。</p>
    <button class="secondary" type="button" id="backup-download" data-id="${selected.id}">导出备份 JSON</button></section>`;
}

function restoredDetail(state, row, operator) {
  if (row.status !== 'restored') {
    return panel('恢复结果', `<p class="error">${esc(row.error || '整份恢复被拒绝，目标空库保持原样。')}</p>`);
  }
  const report = row.report || {};
  const verified = report.verified || {};
  const data = state.restoreDetail?.restore.id === row.id ? state.restoreDetail : null;
  const evidence = data?.evidence || [];
  const held = (data?.runs || []).filter(run=>run.requeue);
  const pending = held.filter(run=>run.requeue==='pending');
  const orders = evidence.map(item=>panel(item.report.summary || '上架顺序单',
    entries(item.report.sections[0].rows) + (item.reviewer
      ? `<p data-testid="reviewed">复核签署：${esc(item.reviewer)} · ${esc(item.review_note)} · ${esc(date(item.reviewed_at))}</p>`
      : '<p class="note">尚未复核签署</p>')));
  const holdRows = held.map(run=>[esc(run.id), esc(run.requeue==='confirmed'?'已确认重新入队':'待确认'), esc(run.owner), esc(date(run.created_at))]);
  return panel(`演练目标 ${row.target}`,
    `<p>${esc(report.summary || '')}</p>
     <dl class="metrics">${[['引用核对', verified.references], ['上架结果', verified.evidence], ['复核签署', verified.signatures], ['待确认', pending.length]]
       .map(([label, value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value ?? '—')}</dd></div>`).join('')}</dl>
     ${data ? (orders.join('') || '<p class="note">该快照没有上架结果。</p>') : '<p class="note">正在读取演练目标库…</p>'}
     ${held.length ? `<h2>排队或执行中的作业</h2>${records(['执行','重新入队','经办','提交时间'], holdRows)}
       ${operator ? `<form id="requeue-form" class="actions"><button ${pending.length?'':'disabled'}>确认重新入队${pending.length?`（${pending.length}）`:'（已全部确认）'}</button></form>` : '<p class="note">确认重新入队需要经办员权限。</p>'}
       <p class="note">确认后作业才会重新进入队列；重复确认不会重复入队。</p>` : '<p class="note">备份时没有排队或执行中的作业。</p>'}`);
}

export function drillPage(state, identity, restoreId) {
  const operator = state.session.role==='operator';
  const selected = state.backups.find(row=>row.id===identity) || state.backups[0];
  const current = state.restores.find(row=>row.id===restoreId) || state.restores.find(row=>row.backup_id===selected?.id);
  const create = operator ? '<form id="backup-form" class="actions"><button>建立一致性备份</button></form>' : '';
  const restore = selected && operator ? `<form id="restore-form"><label class="field"><span>演练目标库（留空自动分配空库）</span>
    <input name="target" maxlength="63" pattern="[a-z][a-z0-9_]*" placeholder="workflow_drill_…" /></label>
    <div class="actions form-actions"><button>恢复到空库并核对</button></div></form>` : '';
  return `${heading(topic.modules[4], '备份期间源库照常排队与签署；恢复到空库后核对上架顺序与签署，再确认排队作业是否重新入队。', create)}
    <div class="spread"><section>${backupPanel(state, selected)}${detail(state, selected)}${restore}</section>
    <aside class="brief"><h2>恢复演练</h2>${selected ? restoreList(state, selected) : '<p class="note">先建立或选择一份备份。</p>'}</aside></div>
    ${current ? restoredDetail(state, current, operator) : ''}`;
}

export async function loadRestoreDetail(state, requested, api) {
  const parts = location.hash.slice(1).split('/');
  const selected = state.backups.find(row=>row.id===Number(parts[1])) || state.backups[0];
  const row = state.restores.find(item=>item.id===requested) || state.restores.find(item=>item.backup_id===selected?.id);
  if (!row || row.status!=='restored') { state.restoreDetail = null; return; }
  if (state.restoreDetail?.restore.id===row.id) return;
  try {
    state.restoreDetail = await api(`/restores/${row.id}`);
  } catch {
    state.restoreDetail = null;
  }
}

export function bindDrill(state, api, refresh, fail) {
  const parts = location.hash.slice(1).split('/');
  const selected = state.backups.find(row=>row.id===Number(parts[1])) || state.backups[0];
  submit('backup-form', async()=>{ await api('/backups', {method:'POST'}); await refresh(); }, fail);
  const exporter = document.getElementById('backup-download');
  if (exporter) exporter.addEventListener('click', async()=>{
    try {
      const response = await api(`/backups/${exporter.dataset.id}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `backup-${selected?.snapshot_id || exporter.dataset.id}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  if (selected) {
    submit('restore-form', async data=>{
      const target = data.get('target');
      const result = await api(`/backups/${selected.id}/restores`, {method:'POST', data:target?{target}:{}});
      location.hash = `drill/${selected.id}/${result.id}`;
      await refresh();
    }, fail);
  }
  const row = state.restores.find(item=>item.id===Number(parts[2])) || state.restores.find(item=>item.backup_id===selected?.id);
  if (row) {
    submit('requeue-form', async()=>{
      await api(`/restores/${row.id}/requeue`, {method:'POST', data:{}});
      state.restoreDetail = null;
      await refresh();
    }, fail);
  }
}
