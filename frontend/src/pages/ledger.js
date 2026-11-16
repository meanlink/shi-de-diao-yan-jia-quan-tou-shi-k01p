import {datasets,ledger} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const admin=()=>session.user.role==='admin';
const writer=()=>session.user.role!=='viewer';
const yes=v=>v?'合格':'不合格';

export async function ledgerPage(){
 const [state,observations,merges,adjudications,frames,batches,corrections,allDatasets]=await Promise.all([
   ledger.state(),ledger.observations(200),ledger.merges(),ledger.adjudications(),ledger.frames(),ledger.batches(),ledger.history(),datasets.list(),
 ]);
 const importable=allDatasets.filter(d=>!d.ledger_batch_id);
 workspace.ledger={state,observations,merges,adjudications,frames,batches,corrections};
 const pending=adjudications.filter(a=>a.status==='pending'||a.reopened);
 const activeMerges=merges.filter(m=>m.revoked_version===null||m.revoked_version===undefined);
 const tokenOptions=[...new Set(observations.map(o=>o.token))].slice(0,300);
 const overview='<div class="ledger-strip">'+[
   ['台账版本',state.version],['逐点观察',state.observations],['有效归并',activeMerges.length],
   ['待裁定',pending.length],['日表批次',state.batches],
 ].map(([k,v])=>'<div><span>'+k+'</span><strong>'+fmt(v)+'</strong></div>').join('')+'</div>';

 const observeForm='<form id="observe-form"><div class="parameter-grid"><label>观察日<input type="date" name="day" required value="2026-11-16"></label><label>采样队<select name="team"><option>A队</option><option>B队</option></select></label><label>区域层<input name="zone" required placeholder="中心区"></label><label>服务点编号<input name="token" placeholder="留空按区域+名称生成"></label><label>服务点名称<input name="point_name" required placeholder="东门站"></label><label>结论<select name="passed"><option value="true">合格</option><option value="false">不合格</option></select></label></div><label>备注<input name="note" maxlength="500" placeholder="现场情况（可选）"></label><button type="submit" class="primary" '+(writer()?'':'disabled')+'>记录一次逐点观察</button><p class="hint">每次观察都留存编号、日期、队伍与版本；同点同日同队相同结论只计一次，不同结论进入裁定。</p></form>';

 const mergeForm='<form id="merge-form"><label>源服务点编号<input name="source_token" list="token-list" required></label><label>目标服务点编号<input name="target_token" list="token-list" required></label><label>归并原因<input name="reason" maxlength="500" placeholder="同一服务点换名/别称"></label><datalist id="token-list">'+tokenOptions.map(t=>'<option value="'+esc(t)+'"></option>').join('')+'</datalist><button type="submit" class="primary" '+(writer()?'':'disabled')+'>建立归并别名</button><p class="hint">归并是追加事件；撤销某次归并只回退该条，之后独立建立的归并继续生效。</p></form>';

 const frameForm='<form id="frame-form"><div class="parameter-grid"><label>区域层<input name="zone" required value="'+(frames[0]?esc(frames[0].zone):'中心区')+'"></label><label>抽样框点数<input type="number" name="size" min="1" max="1000000000" required value="'+(frames[0]?frames[0].size:6200)+'"></label></div><button type="submit" class="primary" '+(admin()?'':'disabled')+'>登记抽样框规模</button></form>';

 const importForm='<form id="import-form"><label>已有日表批次<select name="dataset_id" required>'+importable.map(d=>'<option value="'+d.id+'">'+esc(d.name)+' · '+d.rows+' 行</option>').join('')+'</select></label><button type="submit" class="primary" '+(admin()&&importable.length?'':'disabled')+'>把原日表导入台账</button><p class="hint">按每行的观察点数展开为逐点观察，总量与原日表一致；同一批次只能导入一次，原导入与分析流程不变。</p></form>';

 const batchForm='<form id="batch-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：11-16 日表批次"></label><button type="submit" class="primary" '+(writer()?'':'disabled')+'>从当前台账生成日表批次</button><p class="hint">批次的版本在生成时固定；之后的新观察或归并不会改写它。</p></form>';

 const observerRows=observations.slice(0,120).map(o=>'<tr><td>'+o.id+'</td><td>'+esc(o.token)+'</td><td>'+esc(o.point_name)+'</td><td>'+esc(o.day)+'</td><td>'+esc(o.team)+'</td><td>'+esc(o.zone)+'</td><td>'+yes(o.passed)+'</td><td>'+o.version+'</td><td>'+(admin()?'<button class="text-button" data-correct="'+o.id+'">更正身份</button>':'—')+'</td></tr>').join('');

 const mergeRows=merges.map(m=>'<tr><td>'+m.id+'</td><td>'+esc(m.source_token)+'</td><td>'+esc(m.target_token)+'</td><td>'+m.version+'</td><td>'+(m.revoked_version==null?'<span class="tag ok">生效</span>':'<span class="tag">已撤销 v'+m.revoked_version+'</span>')+'</td><td>'+(m.revoked_version==null&&admin()?'<button class="text-button" data-revoke="'+m.id+'">撤销</button>':'—')+'</td></tr>').join('');

 const adjudicationRows=adjudications.map(a=>{
   const open=a.status==='pending'||a.reopened;
   const state=a.status==='superseded'?'<span class="tag">已收起</span>':open?(a.reopened?'<span class="tag warn">新证据需重裁</span>':'<span class="tag warn">待裁定</span>'):'<span class="tag ok">已裁定：'+esc(a.kind==='conclusion'?yes(a.decision):a.decision)+'</span>';
   return '<tr><td>'+a.id+'</td><td>'+esc(a.kind==='conclusion'?'结论':'区域')+'</td><td>'+esc(a.day)+' · '+esc(a.team)+'</td><td>'+esc(a.cluster)+'</td><td>'+esc((a.options||[]).map(v=>typeof v==='boolean'?yes(v):v).join(' / '))+'</td><td>'+state+'</td><td>'+(open&&admin()?'<button class="text-button" data-decide="'+a.id+'">裁定</button>':'—')+'</td></tr>';
 }).join('');

 const batchRows=batches.map(b=>'<tr><td>'+b.id+'</td><td>'+esc(b.name)+'</td><td>v'+b.version+'</td><td>'+b.row_count+' 行</td><td>'+(b.conflict_count?'<span class="tag warn">'+b.conflict_count+' 项待清</span>':'<span class="tag ok">无冲突</span>')+'</td><td>'+(b.status==='published'?'<span class="tag ok">已发布 · 数据集 #'+b.published_dataset_id+'</span>':b.stale?'<span class="tag warn">台账已更新，需重算</span>':'<span class="tag">待核对</span>')+'</td><td>'+buttonRow(b)+'</td></tr>').join('');

 const correctionRows=corrections.slice(0,20).map(c=>'<li><strong>观察 #'+c.observation_id+'</strong><span>'+esc(c.old_token)+' → '+esc(c.new_token)+' · v'+c.version+' · '+esc(c.reason||'身份更正')+'</span></li>').join('');

 return heading('06 / POINT LEDGER','逐点观察台账','每次逐点观察保留独立身份；可归并同一服务点的别名，撤销单次归并保留后续归并，冲突未清不发布。')+overview+
 '<div class="ledger-grid"><section class="panel"><div class="section-title"><h2>记录观察</h2><span>来源需可追溯</span></div>'+observeForm+'</section>'+
 '<section class="panel"><div class="section-title"><h2>服务点归并</h2><span>'+activeMerges.length+' 条生效</span></div>'+mergeForm+'</section></div>'+
 '<div class="ledger-grid"><section class="panel"><div class="section-title"><h2>抽样框规模</h2><span>版本化登记</span></div>'+frameForm+'<ul class="frame-list">'+frames.map(f=>'<li><strong>'+esc(f.zone)+'</strong><span>'+fmt(f.size)+' 个点 · v'+f.version+'</span></li>').join('')+'</ul></section>'+
 '<section class="panel"><div class="section-title"><h2>生成日表批次</h2><span>固定完整版本</span></div>'+batchForm+'</section></div>'+
 '<div class="ledger-grid"><section class="panel"><div class="section-title"><h2>导入原日表</h2><span>兼容既有批次</span></div>'+importForm+'</section><section class="panel"><div class="section-title"><h2>台账口径</h2><span>版本化规则</span></div><ul class="questions"><li>同点同日同队只计一次，重复提交幂等。</li><li>归并按版本回放；撤销一条不影响后续独立归并。</li><li>结论或区域冲突未清时批次不可发布。</li><li>批次固定完整版本；旧计算晚回不覆盖新结论。</li></ul></section></div>'+
 '<section class="panel"><div class="section-title"><h2>待裁定冲突</h2><span>结论或区域冲突必须裁定后才能发布</span></div>'+(adjudicationRows?'<div class="table-wrap"><table><thead><tr><th>#</th><th>类型</th><th>日期 / 队伍</th><th>服务点</th><th>候选项</th><th>状态</th><th>操作</th></tr></thead><tbody>'+adjudicationRows+'</tbody></table></div>':'<p class="empty">当前没有结论或区域冲突。</p>')+'</section>'+
 '<section class="panel"><div class="section-title"><h2>日表批次</h2><span>待核对 → 已发布</span></div>'+batchWarnings(batches)+(batchRows?'<div class="table-wrap"><table><thead><tr><th>#</th><th>名称</th><th>版本</th><th>规模</th><th>冲突</th><th>状态</th><th>操作</th></tr></thead><tbody>'+batchRows+'</tbody></table></div>':'<p class="empty">尚无批次。先记录观察并登记抽样框规模，再生成日表批次。</p>')+'<p class="hint">发布失败会保留同一次处理；使用同一幂等键重试会续作而不是新建批次。发布后的批次会作为不可变数据批次供原加权透视复用。</p></section>'+
 '<section class="panel" id="trace-panel" hidden><div class="section-title"><h2>批次追溯</h2><span id="trace-title"></span></div><div id="trace-body"></div></section>'+
 '<section class="panel"><div class="section-title"><h2>归并事件</h2><span>追加式记录</span></div>'+(mergeRows?'<div class="table-wrap"><table><thead><tr><th>#</th><th>源</th><th>目标</th><th>版本</th><th>状态</th><th>操作</th></tr></thead><tbody>'+mergeRows+'</tbody></table></div>':'<p class="empty">尚无归并。同一点换名时在此建立别名。</p>')+'</section>'+
 '<section class="panel"><div class="section-title"><h2>逐点观察明细</h2><span>最近 120 条</span></div>'+(observerRows?'<div class="table-wrap"><table><thead><tr><th>#</th><th>编号</th><th>名称</th><th>观察日</th><th>队伍</th><th>区域</th><th>结论</th><th>版本</th><th>操作</th></tr></thead><tbody>'+observerRows+'</tbody></table></div>':'<p class="empty">尚无逐点观察。</p>')+'<p class="hint">身份更正会让关联的未批准分析失效；已批准资料保留原值。</p></section>'+
 '<section class="panel"><div class="section-title"><h2>身份更正轨迹</h2><span>最近 20 条</span></div>'+(correctionRows?'<ul class="timeline">'+correctionRows+'</ul>':'<p class="empty">尚无身份更正。</p>')+'</section>';
}

function batchWarnings(batches){
 const warnings=[...new Set(batches.filter(b=>b.status!=='published').flatMap(b=>b.warnings||[]))];
 return warnings.length?'<div class="warning-stack">'+warnings.map(w=>'<p class="warning">'+esc(w)+'</p>').join('')+'</div>':'';
}

function buttonRow(batch){
 let buttons='<button class="text-button" data-trace="'+batch.id+'">查看追溯</button>';
 if(batch.status==='published')return buttons+'<a class="text-button" href="#/datasets">查看数据批次 ↗</a>';
 if(batch.stale)buttons+='<button class="text-button" data-recompute="'+batch.id+'">按最新台账重算</button>';
 if(admin()&&!batch.stale&&!batch.conflict_count)buttons+='<button class="text-button" data-publish="'+batch.id+'">发布为数据批次</button>';
 return buttons||'<span class="tag warn">待处理</span>';
}

function findBatch(id){return (workspace.ledger?.batches||[]).find(b=>b.id===Number(id));}

export function bindLedger(refresh){
 submit(document.querySelector('#observe-form'),async f=>{
   await ledger.observe({day:f.get('day'),team:f.get('team'),zone:f.get('zone'),point_name:f.get('point_name'),token:f.get('token'),passed:f.get('passed')==='true',note:f.get('note')});
   notify('已记录逐点观察');await refresh();
 });
 const mergeForm=document.querySelector('#merge-form');
 const previewBox=document.createElement('div');
 previewBox.id='merge-preview';
 previewBox.className='merge-preview';
 mergeForm.appendChild(previewBox);
 const runPreview=async()=>{
   const source=mergeForm.querySelector('[name=source_token]').value.trim();
   const target=mergeForm.querySelector('[name=target_token]').value.trim();
   if(!source||!target||source===target){previewBox.innerHTML='';return;}
   try{
     const p=await ledger.preview(source,target);
     const names=[...(p.source_names||[]),...(p.target_names||[])];
     const label=names.length?'合并后名称：'+[...new Set(names)].join(' / '):'';
     const conflicts=(p.would_conflict_on_conclusion||[]).length;
     previewBox.innerHTML='<span class="tag '+(p.would_conflict_on_zone||conflicts?'warn':'ok')+'">将影响 '+p.source_observations+' + '+p.target_observations+' 条观察 · '+p.source_points+' → '+p.target_points+' 个点</span><p class="hint">'+esc(label)+(p.would_conflict_on_zone?' · 将产生区域冲突':'' )+(conflicts?' · 将产生 '+conflicts+' 项结论冲突':'')+'</p>';
   }catch(e){previewBox.innerHTML='<p class="hint">'+esc(e.message)+'</p>';}
 };
 mergeForm.querySelector('[name=source_token]').addEventListener('change',runPreview);
 mergeForm.querySelector('[name=target_token]').addEventListener('change',runPreview);
 submit(document.querySelector('#merge-form'),async f=>{
   await ledger.merge({source_token:f.get('source_token'),target_token:f.get('target_token'),reason:f.get('reason')});
   notify('归并别名已建立');await refresh();
 });
 submit(document.querySelector('#frame-form'),async f=>{
   await ledger.setFrame(f.get('zone'),Number(f.get('size')));notify('抽样框规模已登记');await refresh();
 });
 submit(document.querySelector('#import-form'),async f=>{
   const result=await ledger.importDataset(Number(f.get('dataset_id')));
   notify('已把 '+result.observations+' 条观察导入台账'+(result.skipped.length?'，跳过 '+result.skipped.length+' 行':''));
   await refresh();
 });
 submit(document.querySelector('#batch-form'),async f=>{
   await ledger.createBatch(f.get('name'));notify('日表批次已生成，待核对');await refresh();
 });
 document.querySelectorAll('[data-trace]').forEach(b=>b.onclick=async()=>{
   try{
     const batch=await ledger.batch(b.dataset.trace);
     const panel=document.querySelector('#trace-panel');
     document.querySelector('#trace-title').textContent=batch.name+' · v'+batch.version+' · '+(batch.status==='published'?'已发布':'待核对');
     const merges=(batch.merges||[]).map(m=>'<li>归并 #'+m.id+'：'+esc(m.source)+' → '+esc(m.target)+'（v'+m.version+(m.revoked_version?'，v'+m.revoked_version+' 撤销':'')+'）</li>').join('');
     const traceRows=(batch.trace||[]).slice(0,40).map(t=>{
       const points=t.points||[];
       const shown=points.slice(0,3).map(p=>esc(p.name||p.token)+' [观察 '+p.observation_ids.join(',')+']'+(p.aliases&&p.aliases.length>1?' · 别名 '+p.aliases.filter(a=>a!==p.token).join('、'):'')+'<small>'+(p.decisions&&p.decisions.length?' · 裁定 '+p.decisions.map(d=>d.kind==='conclusion'?(d.decision?'合格':'不合格'):d.decision).join(' / '):'')+'</small>').join('<br>');
       return '<tr><td>'+esc(t.day)+'</td><td>'+esc(t.team)+'</td><td>'+esc(t.zone)+'</td><td>'+t.observed+'</td><td>'+t.passed+'</td><td>'+shown+(points.length>3?'<br><small>… 另有 '+(points.length-3)+' 个服务点</small>':'')+'</td><td>'+((t.adjudications||[]).join(', ')||'无')+'</td></tr>';
     }).join('');
     document.querySelector('#trace-body').innerHTML='<p class="hint">批次固定版本 v'+batch.version+'（当前台账 v'+batch.current_version+'），引用 '+(batch.snapshot?.observation_ids||[]).length+' 条原始观察；以下逐行列出观察编号与裁定事项。</p>'+
       '<div class="table-wrap"><table><thead><tr><th>日期</th><th>队伍</th><th>区域</th><th>观察</th><th>合格</th><th>服务点（原始观察编号）</th><th>裁定事项</th></tr></thead><tbody>'+traceRows+'</tbody></table></div>'+
       ((batch.conflicts||[]).length?'<h3>冲突与裁定</h3><ul class="questions">'+batch.conflicts.map(c=>'<li>'+esc(c.kind==='conclusion'?'结论':'区域')+' · '+esc(c.day)+' · '+esc(c.team)+' · 服务点 '+esc(c.cluster)+' → '+(c.resolved?'已裁定 '+(c.kind==='conclusion'?(c.decision?'合格':'不合格'):esc(c.decision)):'待裁定')+'</li>').join('')+'</ul>':'')+
       (merges?'<h3>引用的归并</h3><ul class="questions">'+merges+'</ul>':'');
     panel.hidden=false;panel.scrollIntoView({behavior:'smooth'});
   }catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-correct]').forEach(b=>b.onclick=async()=>{
   const observation=(workspace.ledger?.observations||[]).find(o=>o.id===Number(b.dataset.correct));
   const token=prompt('更正后的服务点编号（留空保持不变）：',observation?.token||'');if(token===null)return;
   const name=prompt('更正后的服务点名称（留空保持不变）：',observation?.point_name||'');if(name===null)return;
   const reason=prompt('更正原因：','现场编号录入错误');if(reason===null)return;
   try{const r=await ledger.correct(b.dataset.correct,{token,point_name:name,reason});notify('身份已更正；失效未批准分析 '+(r.jobs||[]).length+' 个，保留已批准 '+(r.kept_approved_jobs||[]).length+' 个');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
   const reason=prompt('撤销该次归并的原因（保留后续独立归并）：')??null;if(reason===null)return;
   try{await ledger.revoke(b.dataset.revoke,reason);notify('该次归并已撤销，后续归并仍生效');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-decide]').forEach(b=>b.onclick=async()=>{
   const adjudication=(workspace.ledger?.adjudications||[]).find(a=>a.id===Number(b.dataset.decide));
   const options=(adjudication?.options||[]).map(v=>typeof v==='boolean'?(v?'合格':'不合格'):v);
   const answer=prompt('选择裁定结果：'+options.join(' / '),options[0]??'');
   if(answer===null)return;
   let decision=answer.trim();
   if(adjudication?.kind==='conclusion'){
     if(!['合格','不合格'].includes(decision)){notify('结论裁定必须填写 合格 或 不合格',true);return;}
     decision=decision==='合格';
   }else if(!(adjudication?.options||[]).includes(decision)){
     notify('区域裁定必须选择冲突区域之一',true);return;
   }
   const note=prompt('裁定依据（可选）：')??'';
   try{await ledger.decide(b.dataset.decide,decision,note);notify('裁定已记录，可重算批次');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-recompute]').forEach(b=>b.onclick=async()=>{
   try{await ledger.recompute(b.dataset.recompute);notify('批次已按最新台账重算');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-publish]').forEach(b=>b.onclick=async()=>{
   const batch=findBatch(b.dataset.publish);if(!batch)return;
   const key='ledger-'+batch.id+'-v'+batch.version;
   try{const r=await ledger.publish(batch.id,key);notify(r.resumed?'已续作原发布处理：数据批次 #'+r.dataset_id:'已发布为数据批次 #'+r.dataset_id);await refresh();}catch(e){notify(e.message,true);}
 });
}
