/* 前端交互逻辑。由 index.html 拆分而来，仍然零构建、无打包器。 */
/* ============ 全局状态 ============ */
const S = { token:'', user:null, sessionId: null, sessions: [], sending:false };
const $ = id => document.getElementById(id);

// 后端地址自动探测：页面由后端本机服务时直接用同源地址，用户无需看到也无需配置。
// 直接双击打开本地文件（file://）等特殊场景回退到默认本机端口。
(function resolveApiBase(){
  const el = $('apiBase');
  if(!el) return;
  if(location.protocol === 'http:' || location.protocol === 'https:'){
    el.value = location.origin;
  }else if(!el.value){
    el.value = 'http://127.0.0.1:8000';
  }
})();

// 智能滚动：流式输出时用户可以自由上下翻看 —— 只有当视口本来就停在底部附近
// （距底 28px 内）才自动跟随新内容；用户一旦向上滚动，就不再抢滚动条，
// 直到用户重新滚回底部。
function autoScroll(force=false){
  const box = $('messages');
  if(!box) return;
  const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
  // 距离底部足够近（或强制滚动）时才滑到底部
  if(force || gap < 28){ box.scrollTop = box.scrollHeight; }
}

// 登录形态：演示模式（后端 DEMO_MODE=true）才允许选择内置演示账号；
// 正式交付为手输账号密码。由 /api/system/config 决定，取不到时按正式形态处理。
let __DEMO_MODE = false;
let __selectedDemo = '';   // 卡片式身份选择：当前选中的演示账号
// 卡片式身份选择（演示模式）：用"角色类型"陈列，不暴露真实员工姓名与完整架构；
// 每张卡片对应一个真实存在的演示账号，点击即选中，密码统一写在交付文档里。
const __LOGIN_CARDS = [
  { user:'sales_emp', icon:'👤', title:'普通员工',   desc:'仅访问本部门知识库，可发起请假 / 报销' },
  { user:'fin_mgr',   icon:'🗂', title:'部门管理员', desc:'可上传与维护本部门文档' },
  { user:'ceo',       icon:'🧭', title:'审批负责人', desc:'可审批本部门请假 / 报销单' },
  { user:'admin',     icon:'⚙', title:'系统管理员', desc:'管理看板 · 知识治理 · 全局配置' },
];
async function bootstrapLoginMode(){
  try{
    const r = await fetch(`${$('apiBase').value}/api/system/config`);
    if(!r.ok) throw new Error('config unavailable');
    const cfg = await r.json();
    __DEMO_MODE = !!cfg.demo_mode;
  }catch(e){ __DEMO_MODE = false; }   // 取不到配置时按正式部署处理：手输账号

  const cardsBox = $('loginCards');
  if(__DEMO_MODE){
    $('loginUser').style.display = 'none';
    $('loginDemoUser').style.display = 'none';
    __selectedDemo = __LOGIN_CARDS[0].user;
    cardsBox.innerHTML = __LOGIN_CARDS.map((c,i)=>`
      <button type="button" class="login-card${i===0?' selected':''}" data-user="${c.user}">
        <span class="lc-icon">${c.icon}</span>
        <span class="lc-body">
          <span class="lc-title">${c.title}</span>
          <span class="lc-desc">${c.desc}</span>
        </span>
        <span class="lc-check">✓</span>
      </button>`).join('');
    cardsBox.querySelectorAll('.login-card').forEach(btn=>{
      btn.onclick = () => {
        __selectedDemo = btn.dataset.user;
        cardsBox.querySelectorAll('.login-card').forEach(x=>x.classList.remove('selected'));
        btn.classList.add('selected');
      };
    });
    $('loginSub').textContent = '多角色登录：不同身份拥有不同的部门数据权限';
    $('loginHint').textContent = '点击卡片选择登录身份，仍需输入该账号密码；首次登录后请立即修改密码。';
  }else{
    cardsBox.innerHTML = '';
    $('loginUser').style.display = 'block';
    $('loginDemoUser').style.display = 'none';
    $('loginSub').textContent = '请使用企业统一分配的账号登录';
    $('loginHint').textContent = '首次登录请使用工号 + 初始密码，登录后请立即修改密码；忘记密码请联系系统管理员。';
  }
}
bootstrapLoginMode();

/* ============ 极简 Markdown 渲染（离线可用，先转义再解析） ============ */
function esc(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function md(text){
  const blocks = [];
  const lines = esc(text).split('\n');
  let inCode=false, codeBuf=[], buf=[];
  const flushP = () => { if(buf.length){ blocks.push('<p>'+buf.join('<br/>')+'</p>'); buf=[]; } };
  // 连续的 | 分隔行解析为表格（第二行 --- 视为表头分隔，可省略）
  const isTableLine = l => /^\s*\|.*\|\s*$/.test(l);
  const splitRow = l => l.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
  const isSepRow  = cells => cells.length && cells.every(c=>/^:?-{2,}:?$/.test(c));
  let tableBuf = [];
  const flushTable = () => {
    if(!tableBuf.length) return;
    const rows = tableBuf.map(splitRow);
    tableBuf = [];
    if(rows.length >= 2 && isSepRow(rows[1])){
      const head = rows[0], body = rows.slice(2);
      blocks.push('<table><thead><tr>'+head.map(c=>`<th>${inline(c)}</th>`).join('')+'</tr></thead><tbody>'
        + body.map(r=>'<tr>'+r.map(c=>`<td>${inline(c)}</td>`).join('')+'</tr>').join('')+'</tbody></table>');
    }else{
      blocks.push('<table><tbody>'+rows.map(r=>'<tr>'+r.map(c=>`<td>${inline(c)}</td>`).join('')+'</tr>').join('')+'</tbody></table>');
    }
  };
  for(const raw of lines){
    if(raw.trim().startsWith('```')){
      flushTable(); 
      if(inCode){ blocks.push('<pre><code>'+codeBuf.join('\n')+'</code></pre>'); codeBuf=[]; inCode=false; }
      else { flushP(); inCode=true; }
      continue;
    }
    if(inCode){ codeBuf.push(raw); continue; }
    const line = raw.trimEnd();
    if(isTableLine(line)){ flushP(); tableBuf.push(line); continue; }
    flushTable();
    if(!line){ flushP(); continue; }
    let m;
    if(m = line.match(/^(#{1,3})\s+(.*)$/)){ flushP(); blocks.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
    if(m = line.match(/^\s*([-*]|\d+\.)\s+(.*)$/)){
      flushP(); blocks.push(`<li>${inline(m[2])}</li>`); continue;
    }
    buf.push(inline(line));
  }
  flushTable();
  if(inCode) blocks.push('<pre><code>'+codeBuf.join('\n')+'</code></pre>');
  flushP();
  // 把连续的 li 包成 ul
  return blocks.join('').replace(/(?:<li>.*<\/li>)+/g, g => `<ul>${g}</ul>`);
}
function inline(s){
  return s
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>');
}

/* ============ SSE 消费（fetch + ReadableStream，支持 POST + 自定义头） ============ */
async function streamChat(question, hooks, signal){
  const res = await fetch(`${$('apiBase').value}/api/chat/stream`, {
    method:'POST',
    headers:{ 'Content-Type':'application/json', 'Authorization':'Bearer '+S.token },
    body: JSON.stringify({ question, session_id: S.sessionId }),
    signal
  });
  if(!res.ok){
    let msg = `HTTP ${res.status}`;
    try{ msg = (await res.json()).message || msg; }catch(e){}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    buf += decoder.decode(value, {stream:true});
    let i;
    while((i = buf.indexOf('\n\n')) !== -1){
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if(frame.trim() === ': ping') continue;      // 心跳帧，忽略
      let event = null, data = null;
      for(const line of frame.split('\n')){
        if(line.startsWith('event: ')) event = line.slice(7);
        else if(line.startsWith('data: ')) data = line.slice(6);
      }
      if(!event || !data) continue;
      let payload = data;
      try{ payload = JSON.parse(data); }catch(e){}
      hooks[event] && hooks[event](payload);
    }
  }
}

/* ============ 消息渲染 ============ */
function newAiMsg(){
  $('messages').querySelector('.empty')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg';
  wrap.innerHTML = `
    <div class="avatar ai">AI</div>
    <div>
      <div class="stages"></div>
      <div class="bubble"><span class="cursor"></span></div>
      <div class="evi-note" style="display:none"></div>
      <div class="cites" style="display:none"><button class="cites-toggle" type="button">📚 引用来源 <span class="cites-count"></span><span class="chev">▸</span></button><div class="cite-list"></div></div>
      <div class="source-badge" style="display:none"></div>
      <div class="meta-bar"></div>
      <div class="gap-fb" style="display:none"></div>
    </div>`;
  $('messages').appendChild(wrap);
  autoScroll(true);
  return {
    root: wrap,
    stages: wrap.querySelector('.stages'),
    bubble: wrap.querySelector('.bubble'),
    note: wrap.querySelector('.evi-note'),
    cites: wrap.querySelector('.cites'),
    citeList: wrap.querySelector('.cite-list'),
    source: wrap.querySelector('.source-badge'),
    meta: wrap.querySelector('.meta-bar'),
    fb: wrap.querySelector('.gap-fb'),
  };
}
const STAGE_LABEL = { classify_intent:'意图识别', permission_gate:'权限校验', retrieve:'知识检索', generate:'生成回答', deny:'权限拒绝' };
function renderStage(el, node, ok, label){
  const chip = document.createElement('span');
  chip.className = 'stage-chip' + (ok===false ? ' deny' : ' done');
  chip.textContent = (ok===false?'✕ ':'✓ ') + (label || STAGE_LABEL[node] || node);
  el.appendChild(chip);
}
function renderCites(el, list){
  if(!list || !list.length) return;
  el.cites.style.display = 'block';
  // 默认收起：只显示一个"引用来源 N 条"的胶囊按钮，点击展开
  el.cites.querySelector('.cites-count').textContent = `${list.length} 条`;
  el.cites.querySelector('.cites-toggle').onclick = () => el.cites.classList.toggle('open');
  // RRF 融合分的绝对值跨问题没有可比性，且典型值只有 0.0x，直接显示毫无意义。
  // 换算成本组引用内的相对强弱，用户才能一眼看出"哪条最贴题"。
  const scores = list.map(c => Number(c.score) || 0);
  const maxScore = Math.max(...scores, 1e-9);
  const ratioOf = s => Math.max(0.06, Math.min(1, (Number(s) || 0) / maxScore));
  const confLabel = c => c==='high'?'高可信':c==='medium'?'中可信':'低可信';
  el.citeList.innerHTML = list.map((c,i)=>{
    const ratio = ratioOf(c.score);
    return `
    <div class="cite">
      <button type="button" class="cite-head" onclick="this.closest('.cite').classList.toggle('open')">
        <span class="idx">${i+1}</span>
        <span class="cite-name">${esc(c.doc_name)}</span>
        <span class="conf ${c.confidence}">${confLabel(c.confidence)}</span>
        <span class="cite-dept">${esc(c.department_name||c.department_id)}</span>
        ${c.page_num?`<span class="cite-page">P${c.page_num}</span>`:''}
        <span class="cite-chev">▾</span>
      </button>
      <div class="cite-body">
        <div class="rel" title="本组引用内的相对相关度（融合分归一化，不是命中概率）">
          <span class="rel-bar"><i style="width:${(ratio*100).toFixed(1)}%"></i></span>
          <span class="rel-num">${Math.round(ratio*100)}</span>
        </div>
        <div class="snippet">${esc(c.snippet||'')}</div>
      </div>
    </div>`}).join('');
}
function renderEvidence(el, low){
  // 相关性不足时后端已把答案替换成确定性兜底话术，但如果前端不标注，
  // 用户会把它当成"有依据的正式答复"——这正是幻觉最容易伤人的地方。
  const n = el.note;
  if(!n) return;
  if(!low){ n.style.display = 'none'; return; }
  n.style.display = 'block';
  n.innerHTML = '<b>依据不足</b>：命中的片段与该问题相关性偏低，以上是兜底提示而非正式答复，请以制度原文为准。';
}
function renderSource(el, src){
  // 来源徽章：公网/天气 → 橙色"仅供参考"警示；内网知识库 → 绿色可信标识
  const map = {
    web:          { text: '🌐 公网搜索结果 · 仅供参考，不代表公司内部信息' },
    weather:      { text: '🌐 实时天气（公网）· 仅供参考' },
    market:       { text: '📈 实时行情数据（公网）· 仅供参考，不构成投资建议' },
    encyclopedia: { text: '📖 公开百科词条 · 仅供参考，不代表公司内部规定' },
    holiday:      { text: '📅 法定节假日安排（公网）· 以国务院公布为准' },
    kb:           { text: '📚 来自公司知识库' },
  };
  const ORANGE = ['web', 'weather', 'market', 'encyclopedia', 'holiday'];
  const m = map[src];
  if(!m) return;                       // chat（纯闲聊）不展示徽章
  const s = el.source;
  s.style.display = 'block';
  s.style.margin = '6px 0 0';
  s.style.padding = '6px 10px';
  s.style.borderRadius = '8px';
  s.style.fontSize = '12px';
  s.style.fontWeight = '600';
  if(ORANGE.includes(src)){
    s.style.background = '#fff7ed'; s.style.color = '#c2410c'; s.style.border = '1px solid #fed7aa';
  }else{
    s.style.background = '#ecfdf5'; s.style.color = '#047857'; s.style.border = '1px solid #a7f3d0';
  }
  s.textContent = m.text;
}
function renderMeta(el, d){
  const tags = [];
  if(d.intent) tags.push(`<span class="tag">意图 ${esc(d.intent)}</span>`);
  if(d.cache_hit) tags.push(`<span class="tag hit">缓存命中</span>`);
  if(d.timings && isFinite(d.timings.__total__)) tags.push(`<span class="tag">总耗时 ${(d.timings.__total__/1000).toFixed(2)}s</span>`);
  if(d.timings && d.timings.retrieve!=null) tags.push(`<span class="tag">检索 ${d.timings.retrieve}ms</span>`);
  if(d.graph_engine) tags.push(`<span class="tag">${esc(d.graph_engine)}</span>`);
  if(d.trace_id) tags.push(`<span class="tag">trace ${esc(String(d.trace_id).slice(0,8))}</span>`);
  el.meta.innerHTML = tags.join('') + `
    <div class="feedback">
      <button class="fb-btn" data-fb="like">👍 有用</button>
      <button class="fb-btn" data-fb="dislike">👎 没用</button>
    </div>`;
  el.meta.querySelectorAll('.fb-btn').forEach(b=>{
    b.onclick = async () => {
      b.classList.add('on');
      try{
        await fetch(`${$('apiBase').value}/api/chat/feedback`, {
          method:'POST',
          headers:{'Content-Type':'application/json','Authorization':'Bearer '+S.token},
          body: JSON.stringify({ session_id:S.sessionId||'', question:el.question||'', answer:el.answer||'', feedback_type:b.dataset.fb })
        });
        // 点了"没用"就把知识缺口反馈组件展开：满意度数据只能告诉你"不满意"，
        // 只有让用户说出缺什么，缺口才可能闭环补上。
        if(b.dataset.fb === 'dislike') renderGapFeedback(el, '没帮上忙～说说缺的是什么，我们补充到知识库');
      }catch(e){}
    };
  });
}

/* ============ 知识缺口反馈闭环（用户侧入口） ============ */
// 与后端 gap_store.FEEDBACK_REASONS 一一对应
const GAP_REASONS = [
  ['inaccurate','答案不准确'], ['not_found','没有找到资料'],
  ['irrelevant_cite','引用不相关'], ['outdated','内容已过期'],
];
function maybeGapFeedback(el, opt){
  // 只有知识库类回答才值得反馈：直连外部工具（天气/公网）的答案不归知识库管。
  if(!el || !el.fb) return;
  const src = (opt && opt.source) || '';
  if(src && src !== 'kb') return;
  const low = !!(opt && opt.low);
  const empty = !(opt && opt.cites);
  if(low || empty) renderGapFeedback(el, low ? '依据偏弱，告诉我们正确的资料在哪' : '知识库里没有这部分内容，说一下缺什么');
}
function renderGapFeedback(el, hint){
  const box = el && el.fb;
  if(!box) return;
  if(box.dataset.built === '1'){ box.style.display = 'block'; return; }  // 已构建：保持当前折叠/展开状态
  box.dataset.built = '1';
  box.dataset.hint = hint || '';
  box.style.display = 'block';
  // 默认只显示「没解决」按钮，表单收起；点击展开，提交后收起并可再次展开
  box.innerHTML = `
    <button class="gap-toggle">😕 没解决</button>
    <div class="gap-form" style="display:none">
      <div class="gap-title"></div>
      <div class="gap-reason-row">${GAP_REASONS.map(([v,t]) =>
        `<span class="gap-chip" data-r="${v}">${t}</span>`).join('')}</div>
      <textarea class="gap-note" rows="2" placeholder="选填：期望看到什么内容 / 应该引用哪份制度 / 正确答案是什么"></textarea>
      <div class="gap-actions">
        <button class="btn gap-cancel">取消</button>
        <button class="btn primary gap-submit">提交改进</button>
        <span class="gap-tip">提交后会生成知识缺口工单，由知识管理员跟进补充</span>
      </div>
    </div>`;
  const form = box.querySelector('.gap-form');
  const title = box.querySelector('.gap-title');
  const toggle = box.querySelector('.gap-toggle');
  const setExpanded = v => {
    form.style.display = v ? 'block' : 'none';
    if(v){
      title.textContent = (box.dataset.done === '1' ? '✓ 已提交过一次，可继续补充 —— ' : '😕 ')
        + (box.dataset.hint || '没解决？告诉我们缺什么');
      autoScroll();
    }
  };
  toggle.onclick = () => setExpanded(form.style.display === 'none');
  box.querySelector('.gap-cancel').onclick = () => setExpanded(false);
  box.querySelectorAll('.gap-chip').forEach(c => {
    c.onclick = () => c.classList.toggle('on');
  });
  box.querySelector('.gap-submit').onclick = async () => {
    const reasons = [...box.querySelectorAll('.gap-chip.on')].map(c => c.dataset.r);
    if(!reasons.length){ toast('请先选择至少一个原因', 'warn'); return; }
    try{
      await api('/api/knowledge/gap', { method:'POST', body: JSON.stringify({
        question: el.question || '', reasons,
        note: box.querySelector('.gap-note').value || ''
      })});
      box.dataset.done = '1';
      setExpanded(false);   // 提交后收起表单
      toggle.textContent = '✓ 已提交改进';
      toggle.classList.add('done');
      toast('感谢反馈，已生成知识缺口工单', 'ok');
    }catch(e){ toast('提交失败：' + e.message, 'no'); }
  };
}
async function checkGapNotify(){
  // 轮询"我报的缺口已被补充"的通知（后端在工单流转到 supplemented 时生成）
  if(!S.token) return;
  try{
    const d = await (await api('/api/knowledge/gap/notifications')).json();
    for(const n of (d.items || [])){
      toast('📚 ' + (n.message || '你反馈的知识缺口已补充'), 'ok');
      api(`/api/knowledge/gap/notifications/${encodeURIComponent(n.id)}/read`, {method:'POST'}).catch(()=>{});
    }
  }catch(e){}
}
function addUserMsg(text){
  $('messages').querySelector('.empty')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg user';
  wrap.innerHTML = `<div class="avatar me">${esc((S.user?.display_name||'我').slice(0,2))}</div><div class="bubble">${esc(text)}</div>`;
  $('messages').appendChild(wrap);
  autoScroll(true);
}

/* ============ 发送/停止/重新生成/清空 ============ */
function setSending(v){
  S.sending = v;
  $('btnSend').style.display = v ? 'none' : 'inline-block';
  $('btnStop').style.display = v ? 'inline-block' : 'none';
  // 有上一问且当前空闲时才允许「重新生成」
  if($('btnRegenerate')) $('btnRegenerate').style.display = (!v && S.lastQuestion) ? 'inline-block' : 'none';
  if(!v){ S.abortCtrl = null; $('input').focus(); }
}
function stopGeneration(){
  if(S.abortCtrl){ S.abortCtrl.abort(); S.abortCtrl = null; }
}

async function send(question){
  if(S.sending || !question.trim()) return;
  S.lastQuestion = question.trim();          // 记住最后一问，供「重新生成」复用
  await ensureSession();                     // 首次发言才建会话，标题即首个问题
  S.sending = true;
  setSending(true);
  addUserMsg(question);
  const el = newAiMsg();
  el.question = question;
  let answer = '', citations = [], intent = '';
  const ctrl = new AbortController();
  S.abortCtrl = ctrl;

  try{
    await streamChat(question, {
      meta: d => { console.log('meta', d); },
      stage: d => {
        // 按意图动态渲染进度标签（与后端条件边一致）：
        //   chitchat/operation → 只显示"意图识别"（若走了外部工具再显示工具节点）
        //   其余意图 → 意图识别 + 权限校验 + 知识检索
        if(d.node === 'classify_intent'){
          el._intent = d.intent || el._intent;
          renderStage(el.stages, d.node, true, d.label);
        }
        else if(d.node === 'permission_gate' && d.allowed === false) renderStage(el.stages, 'deny', false);
        else if(d.node === 'permission_gate' && (el._intent === 'chitchat' || el._intent === 'operation')){
          // 闲聊/操作型不展示权限校验（后端条件边已跳过，这里仅防御）
        }
        else if(d.node === 'retrieve' && (el._intent === 'chitchat' || el._intent === 'operation')){
          // 闲聊只有真正调用了外部工具（联网搜索/实时天气）才值得一个标签
          if(d.label === '联网搜索' || d.label === '查询实时天气') renderStage(el.stages, d.node, true, d.label);
        }
        else if(d.node !== 'generate') renderStage(el.stages, d.node, true, d.label);
        if(d.intent) intent = d.intent;
        autoScroll();
      },
      citation: d => { citations = d.citations || []; renderCites(el, citations); },
      token: d => {
        answer += d.delta;
        el.bubble.innerHTML = md(answer) + '<span class="cursor"></span>';
        autoScroll();
      },
      error: d => {
        answer += `\n\n> ⚠ ${d.message||'处理失败'}`;
        el.bubble.innerHTML = md(answer);
      },
      done: d => {
        answer = d.answer || answer;
        citations = d.citations || citations;
        el.answer = answer;
        el.bubble.innerHTML = md(answer);
        renderCites(el, citations);
        renderSource(el, d.answer_source);
        renderEvidence(el, d.low_evidence);
        renderMeta(el, d);
        // 未在知识库里命中 / 依据不足时，主动给用户一个"补资料"的入口
        maybeGapFeedback(el, { low: d.low_evidence, cites: citations.length, source: d.answer_source });
        autoScroll(true);
        // 审批结果提示（审批通过后下次任意聊天都会弹出）
        if(d.leave_notifications && d.leave_notifications.length){
          d.leave_notifications.forEach(ev => {
            if(ev.status === 'approved') toast(`🎉 您的【${ev.leave_type}】请假已审批通过，请假生效！`, 'ok');
            else toast(`您的【${ev.leave_type}】请假被驳回`, 'no');
          });
        }
        if(d.reimburse_notifications && d.reimburse_notifications.length){
          d.reimburse_notifications.forEach(ev => {
            if(ev.status === 'approved') toast(`🎉 您的【${ev.rb_type}】报销（¥${ev.amount}）已审批通过，报销生效！`, 'ok');
            else toast(`您的【${ev.rb_type}】报销（¥${ev.amount}）被驳回`, 'no');
          });
        }
        // 服务端已用首个问题自动命名，回拉一次让侧边栏标题刷新
        loadSessions().catch(()=>{});
      }
    }, ctrl.signal);
    if(!el.answer){ el.answer = answer; el.bubble.innerHTML = md(answer); renderMeta(el,{intent}); }
  }catch(e){
    if(e.name === 'AbortError' || (e.message && /aborted/i.test(e.message))){
      // 用户主动点击停止：保留已生成的内容，并给出中断提示
      el.answer = answer;
      el.bubble.innerHTML = (answer ? md(answer) : '') +
        `<p style="color:var(--muted);font-size:12px;margin-top:8px">⏹ 已中断生成</p>`;
      renderMeta(el,{intent});
    }else{
      el.bubble.innerHTML = `<span style="color:var(--danger)">请求失败：${esc(e.message)}<br/>请检查后端地址与登录状态。</span>`;
    }
  }finally{
    setSending(false);
  }
}

/* ============ 登录 ============ */
async function doLogin(){
  const username = __DEMO_MODE ? (__selectedDemo || __LOGIN_CARDS[0].user) : $('loginUser').value.trim();
  const password = $('loginPwd').value;
  $('loginErr').textContent = '';
  if(!username){ $('loginErr').textContent = '请输入账号'; return; }
  try{
    const r = await fetch(`${$('apiBase').value}/api/auth/login`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, password})
    });
    const d = await r.json();
    if(!r.ok) throw new Error(d.message || '登录失败');
    S.token = d.access_token; S.refreshToken = d.refresh_token || ''; S.user = d.user;
    $('userChip').textContent = `${d.user.display_name} · ${d.user.dept_name}`;
    $('userChip').title = `可访问部门：${d.user.accessible_depts.join(', ')}`;
    applyRoleMenu(d.user.role);
    $('mask').style.display = 'none';
    if(d.user.must_change_password){
      // FR-AUTH-01：首次登录强制改密，未改密前其它接口一律 401 拦截
      openPwdModal();
    } else {
      finishLogin();
    }
  }catch(e){
    $('loginErr').textContent = e.message;
  }
}
/* ============ 按角色动态渲染侧栏菜单 ============ */
// 不同角色看到的「知识治理 / 审批管理」入口不同，无权限的入口直接隐藏（而非置灰），
// 避免用户看到点不动的按钮。与后端 current_user 的 role 及 can_upload 校验保持一致。
//   employee(普通员工): 知识目录 / 请假·报销
//   dept_director·sub_manager(部门管理员): + 文档管理
//   executive(审批负责人/总经理): + 文档管理
//   admin(系统管理员): + 文档管理 + 管理看板
let __canApprove = false;   // 当前登录账号是否具审批权：普通员工(sub_employee)为 false，其余角色均具审批权
function applyRoleMenu(role){
  const canManageDocs = ['admin','executive','dept_director','sub_manager','knowledge_reviewer'].includes(role);
  const isAdmin = role === 'admin';
  const isCompliance = role === 'admin' || role === 'compliance_reviewer';   // 合规审计：只读，最小权限
  // 审计员(compliance_reviewer)是只读角色：只看审计日志，不参与审批，也不发起/配置权限
  const isAuditor = role === 'compliance_reviewer';
  // 普通员工(sub_employee)没有任何审批权限；审计员只读不审批；主管/总监/审批负责人/管理员可审批
  __canApprove = !['sub_employee', 'compliance_reviewer'].includes(role);
  if($('btnDocs'))  $('btnDocs').style.display  = canManageDocs ? 'flex' : 'none';
  if($('btnAdmin')) $('btnAdmin').style.display = isAdmin ? 'flex' : 'none';
  if($('btnCompliance')) $('btnCompliance').style.display = isCompliance ? 'flex' : 'none';
  // 权限申请：业务角色均可发起；审计员不涉及业务权限，入口直接隐藏（而不是显示但点不动）
  if($('btnPerm'))  $('btnPerm').style.display  = isAuditor ? 'none' : 'flex';
  // 菜单名称跟着角色走：员工只看到「请假 / 报销」，审批角色才显示「请假 / 审批 / 报销 / 审批」
  // （菜单写什么就承诺用户能做什么——员工不能审批，就不写「审批」二字）
  if($('leaveLabel')) $('leaveLabel').textContent = __canApprove ? '📝 请假 / 审批' : '📝 请假';
  if($('rbLabel'))    $('rbLabel').textContent    = __canApprove ? '💰 报销 / 审批' : '💰 报销';
  if($('leaveTitle')) $('leaveTitle').textContent = __canApprove ? '请假 / 审批' : '请假';
  if($('rbTitle'))    $('rbTitle').textContent    = __canApprove ? '报销 / 审批' : '报销';
  // 弹窗内「待我审批」Tab：仅审批角色可见；员工点开只显示「我的请假 / 我的报销」
  if($('leaveTabPending')) $('leaveTabPending').style.display = __canApprove ? '' : 'none';
  if($('rbTabPending'))    $('rbTabPending').style.display    = __canApprove ? '' : 'none';
}

/* ============ 后端 API 封装 ============ */
// 访问令牌过期（401）时，用刷新令牌静默续期一次并重放原请求，
// 避免 30 分钟掉线打断操作；刷新也失败则回到登录页。
let __refreshing = null;
async function tryRefresh(){
  if(!S.refreshToken) return false;
  if(!__refreshing){
    __refreshing = (async () => {
      try{
        const r = await fetch(`${$('apiBase').value}/api/auth/refresh`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ refresh_token: S.refreshToken })
        });
        if(!r.ok) return false;
        const d = await r.json();
        S.token = d.access_token;
        S.refreshToken = d.refresh_token || S.refreshToken;   // 轮换：换发新的刷新令牌
        return true;
      }catch(e){ return false; }
      finally{ setTimeout(()=>{ __refreshing = null; }, 0); }
    })();
  }
  return __refreshing;
}
async function api(path, opts={}, __retried=false){
  const isForm = opts.body instanceof FormData;   // FormData 交给浏览器设置 multipart 边界
  const r = await fetch($('apiBase').value + path, {
    ...opts,
    headers: { ...(isForm?{}:{ 'Content-Type':'application/json' }), 'Authorization':'Bearer '+S.token, ...(opts.headers||{}) }
  });
  if(r.status === 401 && !__retried && await tryRefresh()){
    return api(path, opts, true);
  }
  if(!r.ok){
    let m = `HTTP ${r.status}`;
    try{ const d = await r.json(); m = d.detail || d.message || m; }catch(e){}
    if(r.status === 401){ S.token=''; S.refreshToken=''; $('mask').style.display='flex'; }
    throw new Error(m);
  }
  return r;
}
/* ---- 登出：通知后端拉黑令牌，清空本地状态回到登录页 ---- */
async function doLogout(){
  try{
    await fetch(`${$('apiBase').value}/api/auth/logout`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ access_token:S.token, refresh_token:S.refreshToken })
    });
  }catch(e){ /* 后端不可达也要保证前端登出 */ }
  S.token=''; S.refreshToken=''; S.user=null; S.sessionId=null; S.sessions=[]; S.lastQuestion='';
  $('userChip').textContent = '未登录';
  $('notifyWrap').style.display = 'none';
  $('notifyPanel').style.display = 'none'; __notifyOpen = false;
  renderSessions(); emptyState();
  $('mask').style.display = 'flex';
}

/* ============ 会话（按账号隔离，服务端持久化） ============ */
// 会话列表跟着登录账号走：换账号就清空重拉，看不到别人的会话
async function loadSessions(){
  try{
    S.sessions = await (await api('/api/chat/sessions')).json();
  }catch(e){ S.sessions = []; }
  renderSessions();
}
// 懒创建：不发第一条消息前不落库，避免侧边栏堆一排空的"新会话"
async function ensureSession(){
  if(S.sessionId) return;
  try{
    const d = await (await api('/api/chat/sessions', {method:'POST'})).json();
    S.sessionId = d.id;
    S.sessions.unshift(d); renderSessions();
  }catch(e){ console.warn('create session failed', e); }
}
function emptyState(){
  $('messages').innerHTML = '<div class="empty">选择左侧示例问题，或直接在下方输入。<br/>回答将逐字流式返回，并在答案下方标注来源与置信度。</div>';
}
async function newSession(){
  S.sessionId = null;
  renderSessions();
  emptyState();
  $('input').focus();
}
async function openSession(id){
  if(id === S.sessionId) return;
  S.sessionId = id;
  renderSessions();
  $('messages').innerHTML = '';
  try{
    const s = await (await api('/api/chat/sessions/'+id)).json();
    const msgs = s.messages || [];
    if(!msgs.length){ emptyState(); return; }
    for(const m of msgs){
      if(m.role === 'user'){ addUserMsg(m.content); continue; }
      const el = newAiMsg();
      el.question = ''; el.answer = m.content || '';
      el.stages.remove();
      el.bubble.innerHTML = md(m.content || '');
      renderCites(el, m.citations || []);
      // 历史消息不再单独打"历史会话"标签（避免与"引用来源"并列造成逻辑混淆）；
      // 仅渲染引用来源，保持与实时回答一致的呈现。
      el.meta.innerHTML = '';
    }
    autoScroll(true);
  }catch(e){
    emptyState();
    $('messages').insertAdjacentHTML('beforeend',
      `<div class="msg"><div class="bubble" style="color:var(--danger)">加载历史失败：${esc(e.message)}</div></div>`);
  }
}
async function removeSession(id, ev){
  ev.stopPropagation();
  if(!await uiConfirm({ title:'删除会话', message:'删除该会话？此操作不可恢复。', okText:'删除' })) return;
  try{ await api('/api/chat/sessions/'+id, {method:'DELETE'}); }catch(e){}
  if(id === S.sessionId){ await newSession(); }
  await loadSessions();
}
function renderSessions(){
  const list = $('sessionList');
  if(!S.sessions.length){
    list.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px 8px">暂无会话，发送第一条消息后自动创建</div>';
    return;
  }
  // 双保险排序：置顶在前（服务端已按 pin 时间排序），普通按最近更新
  const sessions = [...S.sessions].sort((a,b) => (b.pinned?1:0)-(a.pinned?1:0));
  list.innerHTML = sessions.map(s=>`
    <div class="side-item ${s.id===S.sessionId?'active':''} ${s.pinned?'pinned':''}" data-id="${s.id}"
         title="${esc(s.title)}${s.pinned?'（已置顶）':''}${s.pinned?' · 拖拽可调整置顶顺序':''}"
         ${s.pinned?'draggable="true"':''}>
      <span class="si-flag">${s.pinned?'📌':''}</span>
      <span class="si-title">💬 ${esc(s.title||'新会话')}</span>
      <span class="si-turns">${s.turns||0} 轮</span>
      <span class="si-act si-pinbtn" data-pin="${s.id}" title="${s.pinned?'取消置顶':'置顶会话'}">📌</span>
      <span class="si-act si-del" data-del="${s.id}" title="删除会话">✕</span>
    </div>`).join('');
  list.querySelectorAll('.side-item').forEach(el=>{
    el.onclick = () => openSession(el.dataset.id);
  });
  list.querySelectorAll('.si-del').forEach(b=>{
    b.onclick = ev => removeSession(b.dataset.del, ev);
  });
  list.querySelectorAll('.si-pinbtn').forEach(b=>{
    b.onclick = ev => togglePin(b.dataset.pin, ev);
  });
  list.querySelectorAll('.si-title').forEach(t=>{
    t.ondblclick = ev => startRename(t, ev);
  });
  bindPinDrag(list);
}
/* ---- 会话置顶 / 取消置顶 ---- */
async function togglePin(id, ev){
  ev.stopPropagation();
  const s = S.sessions.find(x => x.id === id);
  if(!s) return;
  try{
    await api(`/api/chat/sessions/${encodeURIComponent(id)}/pin`,
      { method:'POST', body: JSON.stringify({ pinned: !s.pinned }) });
    s.pinned = !s.pinned;
    await loadSessions();
    toast(s.pinned ? '已置顶，拖拽可调整顺序' : '已取消置顶', 'ok');
  }catch(e){ toast('操作失败：' + e.message, 'no'); }
}
/* ---- 双击重命名：回车保存，Esc / 失焦取消 ---- */
function startRename(titleEl, ev){
  ev.stopPropagation();
  const item = titleEl.closest('.side-item');
  const id = item.dataset.id;
  const old = (S.sessions.find(x => x.id === id) || {}).title || '';
  const input = document.createElement('input');
  input.className = 'si-rename';
  input.value = old;
  input.maxLength = 40;
  titleEl.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = save => {
    if(done) return; done = true;
    const nv = input.value.trim();
    if(save && nv && nv !== old){
      api(`/api/chat/sessions/${encodeURIComponent(id)}`,
        { method:'PATCH', body: JSON.stringify({ title: nv }) })
        .then(() => { const s = S.sessions.find(x => x.id === id); if(s) s.title = nv; renderSessions(); toast('已重命名', 'ok'); })
        .catch(e => { renderSessions(); toast('重命名失败：' + e.message, 'no'); });
    }else{ renderSessions(); }
  };
  input.onkeydown = e => {
    e.stopPropagation();
    if(e.key === 'Enter') finish(true);
    else if(e.key === 'Escape') finish(false);
  };
  input.onblur = () => finish(false);
  input.onclick = e => e.stopPropagation();
}
/* ---- 置顶会话之间的拖拽排序 ---- */
function bindPinDrag(list){
  let dragId = null;
  list.querySelectorAll('.side-item.pinned').forEach(el => {
    el.ondragstart = e => { dragId = el.dataset.id; e.dataTransfer.effectAllowed = 'move'; };
    el.ondragend = () => { dragId = null; list.querySelectorAll('.side-item').forEach(x => x.classList.remove('drag-over')); };
    el.ondragover = e => {
      if(!dragId || dragId === el.dataset.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      list.querySelectorAll('.side-item').forEach(x => x.classList.remove('drag-over'));
      el.classList.add('drag-over');
    };
    el.ondrop = async e => {
      e.preventDefault();
      if(!dragId || dragId === el.dataset.id) return;
      const pinnedIds = S.sessions.filter(s => s.pinned).map(s => s.id);
      const from = pinnedIds.indexOf(dragId), to = pinnedIds.indexOf(el.dataset.id);
      if(from < 0 || to < 0) return;
      pinnedIds.splice(to, 0, pinnedIds.splice(from, 1)[0]);   // 拖到目标位置
      try{
        await api('/api/chat/sessions/pin-order', { method:'POST', body: JSON.stringify({ ids: pinnedIds }) });
        await loadSessions();
      }catch(err){ toast('排序失败：' + err.message, 'no'); }
    };
  });
}

/* ============ 请假 / 审批 ============ */
const $leave = id => document.getElementById(id);
/* ---- 全局提示：统一走 Ant Design message（antd-bridge.js 提供主题化实例） ----
   类型映射：'ok'→success，'no'→error，其余 info/warning 直传 */
function toast(msg, type){
  if(window.AntdUI) AntdUI.message(msg, type);
  else console.log('[toast]', type || 'info', msg);
}
/* ---- 顶部导航统一入口：打开一个弹窗时自动关闭其它（只留点开的内容），并同步头部按钮蓝色高亮 ----
   管理看板已从弹窗改为全页视图（#adminView），不在 __NAV 弹窗列表里，单独处理高亮 */
const __NAV = [['leaveMask','btnLeave'],['rbMask','btnReimburse'],['docMask','btnDocs'],
               ['permMask','btnPerm'],['complianceMask','btnCompliance'],['healthMask','healthRing'],['sysHealthMask','btnHealth'],
               ['helpMask','btnHelp']];
function syncNavActive(){
  __NAV.forEach(([m,b]) => { const btn = $(b); if(btn) btn.classList.toggle('active', $(m).style.display === 'flex'); });
  const ba = $('btnAdmin');
  if(ba) ba.classList.toggle('active', $('adminView').style.display === 'flex');
}
function openMask(maskId){
  __NAV.forEach(([m]) => { $(m).style.display = m === maskId ? 'flex' : 'none'; });
  syncNavActive();
}
function closeMask(maskId){
  $(maskId).style.display = 'none';
  syncNavActive();
}
async function openLeave(){
  openMask('leaveMask');
  if(!__canApprove) switchLeaveTab('my');   // 员工强制停留在「我的请假」
  await renderLeave();
}
async function renderLeave(){
  try{
    const my = await (await api('/api/leave/my')).json();
    renderMyLeaves(my || []);
    if(__canApprove){
      const pending = await (await api('/api/leave/pending/me')).json();
      renderPending(pending || []);
      updateLeaveBadge((pending || []).length);
    } else {
      $('pendingLeaves').innerHTML = '';
      updateLeaveBadge(0);
    }
  }catch(e){ console.warn('renderLeave failed', e); }
}
/* 把审批链渲染成可视化步骤条：提交 → 各级审批 → 结束 */
function stepBarHtml(r){
  const chain = r.chain || [];
  const steps = [{ label:'提交', cls:'done' }];
  chain.forEach(s => {
    const cls = s.status === 'approved' ? 'done' : s.status === 'rejected' ? 'rejected' : 'current';
    steps.push({ label: esc(s.approver_name || '审批人'), cls });
  });
  let endCls = 'wait';
  if(r.status === 'approved') endCls = 'done';
  else if(r.status === 'rejected') endCls = 'rejected';
  steps.push({ label:'结束', cls: endCls });
  return '<div class="step-bar">' + steps.map((s,i)=>{
    const dot = s.cls === 'done' ? '✓' : s.cls === 'rejected' ? '✕' : (s.cls === 'current' ? '●' : '○');
    const line = i < steps.length - 1 ? '<div class="step-line"></div>' : '';
    return `<div class="step ${s.cls}"><div class="step-dot">${dot}</div><div class="step-label">${s.label}</div>${line}</div>`;
  }).join('') + '</div>';
}
function renderMyLeaves(list){
  const box = $leave('myLeaves');
  if(!list.length){ box.innerHTML = '<div class="empty" style="padding:30px 0">暂无请假记录。在聊天里输入「我想请假」即可发起。</div>'; return; }
  const me = S.user ? S.user.user_id : '';
  box.innerHTML = list.map(r => {
    const stcls = r.status === 'approved' ? 'lv-ok' : r.status === 'rejected' ? 'lv-no' : 'lv-wait';
    const st = r.status === 'approved' ? '已通过 ✅' : r.status === 'rejected' ? '已驳回 ✕' : '审批中 ⏳';
    // 如果当前登录账号就是下一步待审批人，直接给出通过/驳回操作区
    const cur = (r.chain || []).find(s => s.status === 'pending');
    let actions = '';
    if(r.status === 'pending' && cur && cur.approver_id === me){
      actions = `<div style="margin-top:10px;padding:10px;background:#eff6ff;border-radius:8px;border:1px solid #bfdbfe">
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">当前账号 <b>${esc(S.user.display_name)}</b> 就是审批人，可直接处理：</div>
        <div class="lv-actions">
          <input class="lv-comment" id="cm-my-${esc(r.id)}" placeholder="审批意见（选填）" />
          <button class="btn primary" onclick="leaveApprove('${esc(r.id)}', 'cm-my-')">通过</button>
          <button class="btn" style="color:var(--danger);border-color:var(--danger)" onclick="leaveReject('${esc(r.id)}', 'cm-my-')">驳回</button>
        </div>
      </div>`;
    } else if(r.status === 'pending' && cur){
      actions = `<div style="margin-top:8px;font-size:12px;color:var(--muted)">下一步请切换至 <b>${esc(cur.approver_name)}</b> 账号审批。</div>`;
    }
    return `<div class="lv-card">
      <div class="lv-head"><b>${esc(r.leave_type)}</b> <span class="lv-tag ${stcls}">${st}</span>
        <span class="lv-id">${esc(r.id)}</span></div>
      <div class="lv-meta">${esc(r.applicant_name)} · ${esc((r.start_time||'').slice(0,16))} ~ ${esc((r.end_time||'').slice(0,16))} · ${r.duration_days} 天</div>
      <div class="lv-meta">事由：${esc(r.reason||'')}${r.handover ? (' · 交接：' + esc(r.handover)) : ''}</div>
      ${stepBarHtml(r)}
      ${actions}
    </div>`;
  }).join('');
}
function renderPending(list){
  const box = $leave('pendingLeaves');
  if(!list.length){ box.innerHTML = '<div class="empty" style="padding:30px 0">暂无待您审批的请假单 🎉</div>'; return; }
  box.innerHTML = list.map(r => {
    const cur = (r.chain || []).find(s => s.status === 'pending');
    return `<div class="lv-card">
      <div class="lv-head"><b>${esc(r.applicant_name)}</b> 的 <b>${esc(r.leave_type)}</b>
        <span class="lv-id">${esc(r.id)}</span></div>
      <div class="lv-meta">${esc((r.start_time||'').slice(0,16))} ~ ${esc((r.end_time||'').slice(0,16))} · ${r.duration_days} 天</div>
      <div class="lv-meta">事由：${esc(r.reason||'')}${r.handover ? (' · 交接：' + esc(r.handover)) : ''}</div>
      ${stepBarHtml(r)}
      <div class="lv-meta">当前审批人：<b>${esc(cur ? cur.approver_name : '')}</b>（您）</div>
      <div class="lv-actions">
        <input class="lv-comment" id="cm-${esc(r.id)}" placeholder="审批意见（选填）" />
        <button class="btn primary" onclick="leaveApprove('${esc(r.id)}')">通过</button>
        <button class="btn" style="color:var(--danger);border-color:var(--danger)" onclick="leaveReject('${esc(r.id)}')">驳回</button>
      </div>
    </div>`;
  }).join('');
}
async function leaveApprove(id, prefix=''){
  const c = ($leave(prefix + 'cm-' + id) || {}).value || '';
  try{ await api(`/api/leave/${id}/approve`, {method:'POST', body:JSON.stringify({comment:c})}); await renderLeave(); toast('已通过，审批流程已推进'); }
  catch(e){ toast('操作失败：' + e.message); }
}
async function leaveReject(id, prefix=''){
  const c = ($leave(prefix + 'cm-' + id) || {}).value || '';
  if(!await uiConfirm({ title:'驳回请假申请', message:'确认驳回该请假申请？', okText:'驳回' })) return;
  try{ await api(`/api/leave/${id}/reject`, {method:'POST', body:JSON.stringify({comment:c})}); await renderLeave(); toast('已驳回'); }
  catch(e){ toast('操作失败：' + e.message); }
}
function updateLeaveBadge(n){
  const b = $leave('leaveBadge'), b2 = $leave('leaveBadge2');
  // 员工无审批权，侧栏不显示「待审」红点（待我审批入口本就对其隐藏）
  if(!__canApprove){ b.style.display = 'none'; if(b2) b2.style.display = 'none'; return; }
  if(n > 0){ b.style.display = 'inline-block'; b.textContent = n; b2.style.display = 'inline-block'; b2.textContent = n; }
  else { b.style.display = 'none'; b2.style.display = 'none'; }
}
function switchLeaveTab(which){
  if(!__canApprove) which = 'my';   // 员工无审批权，强制停留在「我的请假」
  $leave('leaveTabMy').classList.toggle('active', which === 'my');
  $leave('leaveTabPending').classList.toggle('active', which === 'pending');
  $leave('myPanel').style.display = which === 'my' ? 'block' : 'none';
  $leave('pendingPanel').style.display = which === 'pending' ? 'block' : 'none';
}
async function refreshLeaveBadge(){
  if(!S.token) return;
  try{ const p = await (await api('/api/leave/pending/me')).json(); updateLeaveBadge((p||[]).length); }
  catch(e){}
}
async function checkLeaveNotify(){
  if(!S.token) return;
  try{
    const d = await (await api('/api/leave/notify')).json();
    (d.events || []).forEach(ev => {
      if(ev.status === 'approved') toast(`🎉 您的【${ev.leave_type}】请假已审批通过，请假生效！`, 'ok');
      else toast(`您的【${ev.leave_type}】请假被驳回`, 'no');
    });
  }catch(e){}
}

/* ============ 报销 / 审批 ============ */
async function openRb(){
  openMask('rbMask');
  if(!__canApprove) switchRbTab('my');
  await renderRb();
}
async function renderRb(){
  try{
    const my = await (await api('/api/reimburse/my')).json();
    renderMyRbs(my || []);
    if(__canApprove){
      const pending = await (await api('/api/reimburse/pending/me')).json();
      renderPendingRbs(pending || []);
      updateRbBadge((pending || []).length);
    } else {
      $('pendingRbs').innerHTML = '';
      updateRbBadge(0);
    }
  }catch(e){ console.warn('renderRb failed', e); }
}
function renderMyRbs(list){
  const box = $('myRbs');
  if(!list.length){ box.innerHTML = '<div class="empty" style="padding:30px 0">暂无报销记录。在聊天里输入「我想报销」即可发起。</div>'; return; }
  const me = S.user ? S.user.user_id : '';
  box.innerHTML = list.map(r => {
    const stcls = r.status === 'approved' ? 'lv-ok' : r.status === 'rejected' ? 'lv-no' : 'lv-wait';
    const st = r.status === 'approved' ? '已通过 ✅' : r.status === 'rejected' ? '已驳回 ✕' : '审批中 ⏳';
    const cur = (r.chain || []).find(s => s.status === 'pending');
    let actions = '';
    if(r.status === 'pending' && cur && cur.approver_id === me){
      actions = `<div style="margin-top:10px;padding:10px;background:#eff6ff;border-radius:8px;border:1px solid #bfdbfe">
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">当前账号 <b>${esc(S.user.display_name)}</b> 就是审批人，可直接处理：</div>
        <div class="lv-actions">
          <input class="lv-comment" id="rbm-my-${esc(r.id)}" placeholder="审批意见（选填）" />
          <button class="btn primary" onclick="rbApprove('${esc(r.id)}', 'rbm-my-')">通过</button>
          <button class="btn" style="color:var(--danger);border-color:var(--danger)" onclick="rbReject('${esc(r.id)}', 'rbm-my-')">驳回</button>
        </div>
      </div>`;
    } else if(r.status === 'pending' && cur){
      actions = `<div style="margin-top:8px;font-size:12px;color:var(--muted)">下一步请切换至 <b>${esc(cur.approver_name)}</b> 账号审批。</div>`;
    }
    return `<div class="lv-card">
      <div class="lv-head"><b>${esc(r.rb_type)}</b> <span class="lv-tag ${stcls}">${st}</span>
        <span class="lv-id">${esc(r.id)}</span></div>
      <div class="lv-meta">${esc(r.applicant_name)} · ¥${r.amount} · 发生日 ${esc((r.expense_date||'').slice(0,10))}</div>
      <div class="lv-meta">说明：${esc(r.reason||'')}${r.payee ? (' · 收款：' + esc(r.payee)) : ''}</div>
      ${stepBarHtml(r)}
      ${actions}
    </div>`;
  }).join('');
}
function renderPendingRbs(list){
  const box = $('pendingRbs');
  if(!list.length){ box.innerHTML = '<div class="empty" style="padding:30px 0">暂无待您审批的报销单 🎉</div>'; return; }
  box.innerHTML = list.map(r => {
    const cur = (r.chain || []).find(s => s.status === 'pending');
    return `<div class="lv-card">
      <div class="lv-head"><b>${esc(r.applicant_name)}</b> 的 <b>${esc(r.rb_type)}</b>
        <span class="lv-id">${esc(r.id)}</span></div>
      <div class="lv-meta">¥${r.amount} · 发生日 ${esc((r.expense_date||'').slice(0,10))}</div>
      <div class="lv-meta">说明：${esc(r.reason||'')}${r.payee ? (' · 收款：' + esc(r.payee)) : ''}</div>
      <div class="lv-meta">当前审批人：<b>${esc(cur ? cur.approver_name : '')}</b>（您）</div>
      <div class="lv-actions">
        <input class="lv-comment" id="${esc(r.id)}" placeholder="审批意见（选填）" />
        <button class="btn primary" onclick="rbApprove('${esc(r.id)}')">通过</button>
        <button class="btn" style="color:var(--danger);border-color:var(--danger)" onclick="rbReject('${esc(r.id)}')">驳回</button>
      </div>
    </div>`;
  }).join('');
}
async function rbApprove(id, prefix=''){
  const el = document.getElementById(prefix + id);
  const comment = el ? el.value : '';
  try{ await api(`/api/reimburse/${id}/approve`, {method:'POST', body:JSON.stringify({comment})}); await renderRb(); toast('已通过，审批流程已推进'); }
  catch(e){ toast('操作失败：' + e.message); }
}
async function rbReject(id, prefix=''){
  const el = document.getElementById(prefix + id);
  const comment = el ? el.value : '';
  if(!await uiConfirm({ title:'驳回报销申请', message:'确认驳回该报销申请？', okText:'驳回' })) return;
  try{ await api(`/api/reimburse/${id}/reject`, {method:'POST', body:JSON.stringify({comment})}); await renderRb(); toast('已驳回'); }
  catch(e){ toast('操作失败：' + e.message); }
}
function updateRbBadge(n){
  const b = $('rbBadge'), b2 = $('rbBadge2');
  if(!__canApprove){ b.style.display = 'none'; if(b2) b2.style.display = 'none'; return; }
  if(n > 0){ b.style.display = 'inline-block'; b.textContent = n; b2.style.display = 'inline-block'; b2.textContent = n; }
  else { b.style.display = 'none'; b2.style.display = 'none'; }
}
function switchRbTab(which){
  if(!__canApprove) which = 'my';
  $('rbTabMy').classList.toggle('active', which === 'my');
  $('rbTabPending').classList.toggle('active', which === 'pending');
  $('rbMyPanel').style.display = which === 'my' ? 'block' : 'none';
  $('rbPendingPanel').style.display = which === 'pending' ? 'block' : 'none';
}
async function refreshRbBadge(){
  if(!S.token) return;
  try{ const p = await (await api('/api/reimburse/pending/me')).json(); updateRbBadge((p||[]).length); }
  catch(e){}
}
async function checkRbNotify(){
  if(!S.token) return;
  try{
    const d = await (await api('/api/reimburse/notify')).json();
    (d.events || []).forEach(ev => {
      if(ev.status === 'approved') toast(`🎉 您的【${ev.rb_type}】报销（¥${ev.amount}）已审批通过，报销生效！`, 'ok');
      else toast(`您的【${ev.rb_type}】报销（¥${ev.amount}）被驳回`, 'no');
    });
  }catch(e){}
}

/* ============ 通用确认弹窗（Promise 化，替代所有原生 confirm） ============ */
/* ---- 通用确认弹窗：统一走 Ant Design Modal.confirm（antd-bridge.js 提供主题化实例） ----
   危险操作（删除/停用/驳回/撤回/清空/回滚）自动使用红色确认按钮 */
function uiConfirm({ title = '确认操作', message = '', okText = '确定', cancelText = '取消', danger } = {}){
  const isDanger = danger !== undefined ? danger
    : /删除|停用|驳回|撤回|清空|回滚/.test(title + ' ' + okText);
  return AntdUI.confirm({ title, message, okText, cancelText, danger: isDanger });
}

/* ============ 管理看板（仅管理员） ============ */
// 配置类指标：把后端返回的技术标识翻译成面向业务的中文（私有化交付语境），
// 不暴露 memory / cloud 等词，避免客户对"数据是否出内网"产生疑虑。
function cfgLabel(cat, raw){
  const M = {
    store: { memory:'本地存储', milvus:'向量库(Milvus)', pgvector:'向量库(PGVector)', redis:'Redis', pg:'PostgreSQL' },
    llm:   { cloud:'本地 Qwen-14B', vllm:'本地 Qwen-14B', ollama:'本地 Qwen-14B', mock:'本地模拟后端' },
    cache: { memory:'本地 Redis 缓存', redis:'本地 Redis 缓存', none:'未启用' },
  };
  return (M[cat] && M[cat][raw]) || raw;
}
let __auditType = '';
const TYPE_LABELS = {chat:'问答', chat_deny:'拦截', login_ok:'登录', login_fail:'登录失败',
  leave_submit:'请假提交', leave_approve:'请假审批', leave_reject:'请假驳回',
  reimburse_submit:'报销提交', reimburse_approve:'报销审批', reimburse_reject:'报销驳回',
  cache_invalidate:'清缓存', gap_feedback:'缺口反馈', gap_assign:'缺口指派',
  gap_resolve:'缺口已补充', gap_close:'缺口关闭', gap_reopen:'缺口重开'};
async function openAdmin(){
  /* 全页工作台：关闭其它弹窗与目录页，隐藏会话区，显示看板页 */
  __NAV.forEach(([m]) => { $(m).style.display = 'none'; });
  $('catalogView').style.display = 'none';
  if($('chat')) $('chat').style.display = 'none';
  $('adminView').style.display = 'flex';
  syncNavActive();
  await renderAdmin();
}
function closeAdmin(){
  $('adminView').style.display = 'none';
  if($('chat')) $('chat').style.display = 'flex';
  syncNavActive();
}
async function renderAdmin(){
  try{
    const st = await (await api('/api/admin/stats')).json();
    /* 统计卡三组分组：业务概览 / 待办事项 / 系统状态，每组有标题，主次分明 */
    const group = (title, inner) =>
      `<div class="stat-group"><div class="stat-group-title">${title}</div><div class="stat-grid">${inner}</div></div>`;
    const card = (grp, l, numHtml, key, badge, trendHtml) =>
      `<div class="stat-card clickable grp-${grp}" data-goto="${key}" title="点击查看对应明细"><div class="num">${numHtml}${badge||''}</div><div class="lbl">${esc(l)}</div>${trendHtml||''}</div>`;
    const withSub = (main, sub) => `${esc(String(main))}<span class="num-sub">${esc(sub)}</span>`;
    const pendBadge = n => n > 0 ? `<span class="pend-badge" title="待处理 ${n} 条">${n}</span>` : '';
    /* 周趋势：近 7 天 vs 前 7 天（数据来自审计日志，无数据时不显示，不编造） */
    let qTrend = '';
    try{
      const an = await (await api('/api/admin/analytics?days=14')).json();
      const t = an.trend || [];
      if(t.length >= 14){
        const cur = t.slice(7).reduce((s,x)=>s+(x.questions||0),0);
        const prev = t.slice(0,7).reduce((s,x)=>s+(x.questions||0),0);
        if(prev > 0){
          const pct = Math.round((cur-prev)/prev*100);
          const cls = pct > 0 ? 'trend-up' : pct < 0 ? 'trend-down' : 'trend-flat';
          const arrow = pct > 0 ? '↑' : pct < 0 ? '↓' : '→';
          qTrend = `<div class="trend ${cls}">${arrow} ${Math.abs(pct)}% 较上周</div>`;
        }
      }
    }catch(e){ /* 趋势拿不到不影响主卡片 */ }
    const satPct = Math.round((st.satisfaction||0)*100);
    const satCls = satPct >= 85 ? 'num-ok' : 'num-warn';
    const gapPending = (st.knowledge_gaps && st.knowledge_gaps.pending) || 0;
    $('adminStats').innerHTML =
      group('📊 业务概览', [
        card('biz', '累计提问', esc(String(st.questions)), 'questions', '', qTrend),
        card('biz', '会话总数', esc(String(st.sessions)), 'sessions'),
        card('biz', '独立问题', esc(String(st.unique_questions)), 'unique'),
        card('biz', '满意度', `<span class="${satCls}">${satPct}%</span>`, 'satisfaction'),
      ].join('')) +
      group('📋 待办事项', [
        card('biz', '请假单', withSub(st.leave_total, `（待审 ${st.leave_pending}）`), 'leave', pendBadge(st.leave_pending)),
        card('biz', '报销单', withSub(st.reimburse_total, `（待审 ${st.reimburse_pending}）`), 'reimburse', pendBadge(st.reimburse_pending)),
        card('biz', '知识缺口', st.knowledge_gaps
          ? withSub(st.knowledge_gaps.total_unique, `（${st.knowledge_gaps.total_asks} 次）`)
          : '0', 'gaps', pendBadge(gapPending)),
      ].join('')) +
      group('🔧 系统状态', [
        card('sys', '注册账号', esc(String(st.users)), 'users'),
        card('cfg', '存储模式', esc(cfgLabel('store', st.store_mode)), 'store'),
        card('cfg', 'LLM 后端', esc(cfgLabel('llm', st.llm_backend)), 'llm'),
        card('cfg', '缓存', esc(cfgLabel('cache', st.cache_mode)), 'cache'),
      ].join(''));
    $('adminStats').querySelectorAll('.stat-card').forEach(c => {
      c.onclick = () => statCardJump(c.dataset.goto, c);
    });
    await renderTodo(st);
    const hot = await (await api('/api/chat/hot-questions?top=8')).json();
    $('adminHot').innerHTML = (hot||[]).length
      ? hot.map((h,i) => `<div style="padding:4px 0;border-bottom:1px dashed var(--border);font-size:12px">${i+1}. ${esc(h.question)} <span style="color:var(--muted)">× ${h.count}</span></div>`).join('')
      : '<div class="empty" style="padding:16px 0">暂无提问记录</div>';
    await renderGaps();
    await renderAudit();
    await renderDocs();
    await renderDelReqs();
  }catch(e){
    $('adminStats').innerHTML = `<div class="stat-card"><div class="lbl">加载失败：${esc(e.message)}</div></div>`;
  }
}
/* ---- 待办事项汇总（默认页）：有事项才显示，没有给"全部处理完毕"的正反馈 ---- */
async function renderTodo(st){
  const box = $('adminTodo');
  if(!box) return;
  const jobs = [];
  try{ const g = await (await api('/api/admin/knowledge-gaps?status=pending&top=1')).json();
       jobs.push({ key:'gaps', icon:'🕳', title:'知识缺口工单', n:(g.summary&&g.summary.pending)||0,
         latest: (g.items&&g.items[0]) ? `最近：${g.items[0].question}` : '' }); }catch(e){}
  try{ const p = await (await api('/api/leave/pending/me')).json();
       const it = (p||[])[0];
       jobs.push({ key:'leave', icon:'📋', title:'请假单审批', n:(p||[]).length,
         latest: it ? `最近：${it.applicant_name || it.display_name || it.username} · ${it.leave_type || ''}` : '' }); }catch(e){}
  try{ const p = await (await api('/api/reimburse/pending/me')).json();
       const it = (p||[])[0];
       jobs.push({ key:'reimburse', icon:'📋', title:'报销单审批', n:(p||[]).length,
         latest: it ? `最近：${it.applicant_name || it.display_name || it.username} · ¥${it.amount ?? '-'}` : '' }); }catch(e){}
  try{ const d = await (await api('/api/admin/doc-deletions?status=pending')).json();
       const it = (d.items||[])[0];
       jobs.push({ key:'delreqs', icon:'🗑', title:'文档删除审批', n:(d.items||[]).length,
         latest: it ? `最近：${it.doc_name}` : '' }); }catch(e){}
  jobs.sort((a,b)=>(b.n||0)-(a.n||0));   // 寰呭姙浼樺厛锛氭湁鏁板€肩殑鎺掑墠闈紝鏆傛棤寰呭姙鐨勬矇搴昤r
  const act = jobs.map(j => {
    const jump = j.key === 'gaps'    ? `switchAdminTab('gaps'); _clickChip('gapStatusFilter','s','pending'); _scrollFlash('pane-gaps');`
               : j.key === 'leave'   ? `openLeave(); switchLeaveTab('pending');`
               : j.key === 'reimburse' ? `openRb(); switchRbTab('pending');`
               : `switchAdminTab('docs'); _scrollFlash('adminDelReqs');`;
    const has = j.n > 0;
    return `<div class="lv-card" style="display:flex;align-items:center;gap:10px;${has?'':'opacity:.62'}">
      <div style="font-size:18px">${j.icon}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600">${esc(j.title)}
          ${has ? `<span class="pend-badge" style="margin-left:4px">${j.n} 条待处理</span>` : '<span style="font-size:12px;color:var(--muted)">暂无待办</span>'}
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(j.latest || (has ? '' : '无需处理'))}</div>
      </div>
      <button class="btn ${has?'primary':''}" style="padding:3px 12px;font-size:12px" onclick="${jump}">${has?'立即处理':'查看'}</button>
    </div>`;
  }).join('');
  const total = jobs.reduce((s,j)=>s+j.n,0);
  box.innerHTML = (total > 0
    ? `<div style="font-size:12.5px;margin-bottom:8px">📌 共 <b style="color:#dc2626">${total}</b> 条待办需要您处理</div>`
    : `<div style="font-size:12.5px;color:var(--success);margin-bottom:8px">✓ 全部处理完毕，没有待办事项</div>`) + act;
}
/* ---- 统计卡点击跳转：滚到对应区块并高亮，或打开对应审批弹窗 ---- */
function _scrollFlash(id){
  const t = $(id);
  if(!t) return;
  t.scrollIntoView({ behavior:'smooth', block:'start' });
  t.classList.remove('sec-flash');
  void t.offsetWidth;           // 重启动画
  t.classList.add('sec-flash');
  setTimeout(() => t.classList.remove('sec-flash'), 1500);
}
function _clickChip(filterId, attr, val){
  const chip = document.querySelector(`#${filterId} .chip[data-${attr}="${val}"]`);
  if(chip) chip.click();
}
/* ---- Tab 切换：点哪个显示哪个，数据在打开看板时已统一加载 ---- */
function switchAdminTab(name){
  document.querySelectorAll('#adminTabs .admin-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.admin-pane').forEach(p =>
    p.classList.toggle('active', p.id === 'pane-' + name));
  // 三个管理页签按需懒加载，避免打开看板时一次性打满接口
  if(name === 'users') loadUsers();
  if(name === 'org') loadOrgTree();
  if(name === 'analytics') loadAnalytics();
  if(name === 'syscfg'){ loadSysConfig(); loadBackups(); loadModels(); }
}

/* ============ 用户管理（新增 / 重置密码 / 调角色 / 启停） ============ */
let __umRoles = null;
async function loadUsers(){
  const box = $('adminUsers');
  box.innerHTML = '<div class="empty" style="padding:16px 0">加载中…</div>';
  try{
    const d = await (await api('/api/admin/users')).json();
    __umRoles = d.roles || {};
    // 新增账号表单的角色/部门下拉（只填一次）
    const roleSel = $('umNewRole');
    if(!roleSel.options.length){
      roleSel.innerHTML = Object.entries(__umRoles).map(([v,l])=>`<option value="${v}">${esc(l)}</option>`).join('');
      const depts = await (await api('/api/admin/departments')).json();
      $('umNewDept').innerHTML = (depts||[]).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
    }
    const items = d.items || [];
    box.innerHTML = items.length ? `<table class="audit-table"><thead>
      <tr><th>账号</th><th>姓名</th><th>角色</th><th>部门</th><th style="width:70px">状态</th><th style="width:210px">操作</th></tr></thead>
      <tbody>${items.map(u=>`<tr>
        <td>${esc(u.username)}${u.source==='custom'?'<span class="lv-tag lv-wait" style="margin-left:4px">新建</span>':''}</td>
        <td>${esc(u.display_name)}</td>
        <td>${esc(u.role_label)}</td>
        <td>${esc(u.dept_name||u.dept_id)}</td>
        <td>${u.disabled?'<span class="lv-tag lv-no">已停用</span>':'<span class="lv-tag lv-ok">正常</span>'}</td>
        <td>
          <button class="btn" style="padding:2px 8px;font-size:11px" onclick="umResetPwd('${esc(u.username)}')">重置密码</button>
          <button class="btn" style="padding:2px 8px;font-size:11px" onclick="umSetRole('${esc(u.username)}','${esc(u.role)}','${esc(u.dept_id)}')">调角色</button>
          ${u.username !== S.user?.username
            ? `<button class="btn ${u.disabled?'':'btn-danger'}" style="padding:2px 8px;font-size:11px"
                 onclick="umToggle('${esc(u.username)}',${u.disabled?false:true})">${u.disabled?'启用':'停用'}</button>`
            : ''}
        </td>
      </tr>`).join('')}</tbody></table>` : '<div class="empty" style="padding:16px 0">暂无用户</div>';
  }catch(e){ box.innerHTML = `<div class="empty" style="padding:16px 0">加载失败：${esc(e.message)}</div>`; }
}
async function createUser(){
  const msg = $('umMsg');
  const body = {
    username: $('umNewUser').value.trim(), display_name: $('umNewName').value.trim(),
    password: $('umNewPwd').value, role: $('umNewRole').value, dept_id: $('umNewDept').value,
  };
  if(!body.username || !body.display_name || !body.password){ msg.textContent='账号、姓名、初始密码均必填'; msg.style.color='var(--danger)'; return; }
  try{
    await api('/api/admin/users', { method:'POST', body: JSON.stringify(body) });
    msg.textContent = `✓ 已创建账号 ${body.username}，首次登录须修改密码`; msg.style.color='var(--ok)';
    $('umNewUser').value=''; $('umNewName').value=''; $('umNewPwd').value='';
    await loadUsers();
  }catch(e){ msg.textContent = e.message; msg.style.color='var(--danger)'; }
}
async function umResetPwd(username){
  const vals = await AntdUI.formModal({
    title: `重置密码 — ${username}`,
    okText: '重置密码',
    fields: [{
      name: 'pwd', label: '新密码', type: 'password', required: true,
      requiredMsg: '请输入新密码',
      placeholder: '至少 8 位，须同时包含字母与数字',
      extra: '重置后该用户首次登录须修改密码。',
      rules: [{
        validator: (_, v) => {
          if(!v) return Promise.resolve();                    // 空值交给 required 规则
          if(String(v).length < 8) return Promise.reject(new Error('密码至少 8 位'));
          if(!(/[a-zA-Z]/.test(v) && /\d/.test(v))) return Promise.reject(new Error('密码须同时包含字母与数字'));
          return Promise.resolve();
        }
      }]
    }]
  });
  if(!vals || !vals.pwd) return;
  try{
    await api(`/api/admin/users/${encodeURIComponent(username)}/reset-password`,
      { method:'POST', body: JSON.stringify({ password: vals.pwd }) });
    toast('✓ 密码已重置，该用户首次登录须修改密码', 'ok');
  }catch(e){ toast('重置失败：'+e.message, 'no'); }
}
async function umSetRole(username, curRole, curDept){
  const options = Object.entries(__umRoles || {}).map(([v, l]) => ({ value: v, label: `${l}（${v}）` }));
  if(!options.length){ toast('角色列表未加载，请稍后重试', 'no'); return; }
  const vals = await AntdUI.formModal({
    title: `调整角色 — ${username}`,
    okText: '确认调整',
    fields: [{
      name: 'role', label: '新角色', type: 'select', required: true,
      requiredMsg: '请选择新角色', initialValue: curRole,
      options, showSearch: true,
      extra: `当前角色：${(__umRoles || {})[curRole] || curRole}（${curRole}）`
    }]
  });
  const role = vals && vals.role;
  if(!role || role === curRole) return;
  try{
    await api(`/api/admin/users/${encodeURIComponent(username)}/role`,
      { method:'POST', body: JSON.stringify({ role }) });
    toast('✓ 角色已调整', 'ok');
    await loadUsers();
  }catch(e){ toast('调整失败：'+e.message, 'no'); }
}
async function umToggle(username, disabled){
  const ok = await uiConfirm({ title: disabled?'停用账号':'启用账号',
    message: disabled ? `停用后「${username}」将无法登录系统，已签发的会话随即失效。确认停用？`
                      : `确认恢复「${username}」的登录能力？`,
    okText: disabled?'停用':'启用' });
  if(!ok) return;
  try{
    await api(`/api/admin/users/${encodeURIComponent(username)}/status`,
      { method:'POST', body: JSON.stringify({ disabled }) });
    toast(disabled?'已停用':'已启用', 'ok');
    await loadUsers();
  }catch(e){ toast('操作失败：'+e.message, 'no'); }
}

/* ============ 部门管理（组织树 + 成员分布，只读视图） ============ */
async function loadOrgTree(){
  const box = $('adminOrg');
  box.innerHTML = '<div class="empty" style="padding:16px 0">加载中…</div>';
  try{
    const d = await (await api('/api/admin/org-tree')).json();
    const items = (d.items||[]).sort((a,b)=>a.level-b.level || a.id.localeCompare(b.id));
    box.innerHTML = items.map(x=>`
      <div style="padding:8px 4px;border-bottom:1px dashed var(--border);${x.level===2?'padding-left:28px':''}">
        <div style="display:flex;align-items:center;gap:8px">
          <b style="font-size:${x.level===1?'13.5':'12.5'}px">${x.level===2?'└ ':''}${esc(x.name)}</b>
          <span style="font-size:11.5px;color:var(--muted)">${x.member_count} 人</span>
        </div>
        ${x.members.length ? `<div style="font-size:11.5px;color:var(--muted);margin-top:3px;line-height:1.7">
          ${x.members.map(m=>`${esc(m.display_name)}（${esc(m.role_label)}）${m.disabled?' [已停用]':''}`).join(' · ')}</div>` : ''}
      </div>`).join('');
  }catch(e){ box.innerHTML = `<div class="empty" style="padding:16px 0">加载失败：${esc(e.message)}</div>`; }
}

/* ============ 系统配置（只读） + 数据备份 ============ */
async function loadSysConfig(){
  const box = $('sysCfgBody');
  box.innerHTML = '<div class="empty" style="padding:8px 0">加载中…</div>';
  try{
    const c = await (await api('/api/admin/system-config')).json();
    const row = (k,v)=>`<div style="display:flex;font-size:12.5px;padding:4px 0;border-bottom:1px dashed var(--border)">
      <span style="width:150px;color:var(--muted)">${k}</span><span>${esc(String(v))}</span></div>`;
    box.innerHTML =
      row('系统版本', c.version) + row('运行环境', c.environment) +
      row('离线模式', c.offline_mode ? '已启用（断外网）' : '未启用') +
      row('知识库检索', cfgLabel('store', c.store_mode)) + row('问答缓存', cfgLabel('cache', c.cache_mode)) +
      row('缓存策略', `TTL ${c.cache_ttl_seconds}s · 上限 ${c.cache_max_items} 条`) +
      row('大语言模型', `${c.llm.model}（备用 ${c.llm.fallback}）`) +
      row('生成参数', `temperature=${c.llm.temperature} · max_tokens=${c.llm.max_tokens} · 首字超时 ${c.llm.timeout_seconds}s`) +
      row('向量模型', `${c.embedding.model}（dim=${c.embedding.dim}）`) +
      row('OCR', c.ocr_enabled ? '已启用（PaddleOCR）' : '未启用') +
      row('敏感部门', (c.sensitive_depts||[]).join('、') || '未配置') +
      row('钉钉集成', c.dingtalk_mock ? '本地模拟（未接真实钉钉）' : '已接真实钉钉');
  }catch(e){ box.innerHTML = `<div class="empty" style="padding:8px 0">加载失败：${esc(e.message)}</div>`; }
}
async function loadBackups(){
  const box = $('backupList');
  try{
    const d = await (await api('/api/admin/backups')).json();
    const items = d.items || [];
    box.innerHTML = items.length
      ? items.map(b=>`<div style="display:flex;font-size:12px;padding:4px 0;border-bottom:1px dashed var(--border)">
          <span>${esc(b.file)}</span>
          <span style="margin-left:auto;color:var(--muted)">${b.size_kb} KB · ${new Date(b.created_at*1000).toLocaleString('zh-CN')}</span>
        </div>`).join('')
        + `<div style="font-size:11.5px;color:var(--muted);margin-top:6px">备份目录：${esc(d.dir||'')}</div>`
      : '<div style="font-size:12px;color:var(--muted)">暂无备份记录，点「立即备份」创建第一份。</div>';
  }catch(e){ box.innerHTML = `<div style="font-size:12px;color:var(--danger)">加载失败：${esc(e.message)}</div>`; }
}
async function doBackup(){
  const msg = $('backupMsg');
  msg.textContent = '备份中…';
  try{
    const d = await (await api('/api/admin/backup', { method:'POST', body:'{}' })).json();
    msg.textContent = `✓ 已备份 ${d.files} 个文件（${d.size_kb} KB）`;
    await loadBackups();
  }catch(e){ msg.textContent = '备份失败：'+e.message; }
}

/* ============ 数据看板（运营趋势，纯只读） ============ */
let __anDays = 14;
/* 极简柱状图：不引第三方图表库，交付环境零外部依赖更稳 */
function __bars(rows, max, color, height){
  max = Math.max(1, max);
  return `<div style="display:flex;align-items:flex-end;gap:3px;height:${height}px;border-bottom:1px solid var(--border);padding:2px 0">
    ${rows.map(r => `
      <div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%">
        <div style="font-size:10px;color:var(--muted)">${r.v || ''}</div>
        <div title="${esc(r.tip || '')}" style="width:72%;height:${Math.max(2, (r.v || 0) / max * 100)}%;background:${color};border-radius:3px 3px 0 0"></div>
        <div style="font-size:10px;color:var(--muted);margin-top:3px">${esc(r.date)}</div>
      </div>`).join('')}
  </div>`;
}
async function loadAnalytics(){
  const cards = $('anCards'), tr = $('anTrend'), dt = $('anDocTrend');
  cards.innerHTML = '<div class="empty" style="padding:10px 0">加载中…</div>';
  try{
    const d = await (await api(`/api/admin/analytics?days=${__anDays}`)).json();
    cards.innerHTML = [
      ['今日活跃用户', d.dau], ['30 天活跃用户', d.mau], ['区间问答量', d.questions_total],
      ['越权拦截', d.denied_total], ['文档总数', d.docs_total], ['区间文档更新', d.docs_new_window],
    ].map(([k, v]) => `<div class="stat-card"><div class="num">${v ?? 0}</div><div class="label">${k}</div></div>`).join('');
    const trend = d.trend || [], docs = d.doc_trend || [];
    tr.innerHTML =
      `<div style="font-size:11.5px;color:var(--muted);margin-bottom:2px">问答量</div>` +
      __bars(trend.map(t => ({ date: t.date, v: t.questions,
        tip: `${t.date} 问答 ${t.questions} 次 · 活跃 ${t.active_users} 人` })),
        Math.max(...trend.map(t => t.questions), 0), 'var(--primary, #2f6fed)', 110) +
      `<div style="font-size:11.5px;color:var(--muted);margin:8px 0 2px">活跃用户</div>` +
      __bars(trend.map(t => ({ date: t.date, v: t.active_users, tip: `${t.date} 活跃 ${t.active_users} 人` })),
        Math.max(...trend.map(t => t.active_users), 0), '#2ea36b', 70);
    dt.innerHTML = __bars(docs.map(x => ({ date: x.date, v: x.docs, tip: `${x.date} 文档更新 ${x.docs} 次` })),
      Math.max(...docs.map(x => x.docs), 0), '#8a63d2', 90);
  }catch(e){ cards.innerHTML = `<div class="empty" style="padding:10px 0">加载失败：${esc(e.message)}</div>`; }
}

/* ============ 模型管理（清单 + 主备热切换） ============ */
function _cfgRow(k, v){
  return `<div style="display:flex;font-size:12.5px;padding:4px 0;border-bottom:1px dashed var(--border)">
    <span style="width:150px;color:var(--muted)">${k}</span><span>${esc(String(v))}</span></div>`;
}
async function loadModels(){
  const box = $('modelBody');
  box.innerHTML = '<div style="color:var(--muted)">加载中…</div>';
  try{
    const m = await (await api('/api/admin/models')).json();
    const g = m.gpu || {};
    const gpu = g.available ? `${g.device} · 显存 ${g.memory_used_gb} / ${g.memory_total_gb} GB` : (g.reason || '不可用');
    const chain = m.llm.chain || [];
    const opts = chain.map(c => `<option value="${esc(c.key)}"${c.is_primary ? ' selected' : ''}>${
      esc(c.key)}${c.is_primary ? '（当前主模型）' : c.is_fallback ? '（备用）' : ''}</option>`).join('');
    box.innerHTML =
      _cfgRow('LLM 主模型', m.llm.primary_key || '-') +
      _cfgRow('向量模型', `${m.embedding.model}（dim=${m.embedding.dim}）`) +
      _cfgRow('意图分类模型', m.intent.model || '-') +
      _cfgRow('OCR 引擎', m.ocr.engine) +
      _cfgRow('GPU / 显存', gpu) +
      `<div style="display:flex;align-items:center;gap:8px;margin-top:8px">
         <span style="color:var(--muted)">主模型切换</span>
         <select id="modelSel" style="height:30px;padding:0 8px;font-size:12px">${opts || '<option>无可用模型</option>'}</select>
         <button class="btn primary" id="btnModelSwitch" style="height:30px;padding:0 12px;font-size:12px">切换</button>
       </div>
       <div style="font-size:11.5px;color:var(--muted);margin-top:6px">${esc(m.switch_note || '')}</div>`;
    $('btnModelSwitch').onclick = doSwitchModel;
  }catch(e){ box.innerHTML = `<div style="color:var(--danger)">加载失败：${esc(e.message)}</div>`; }
}
async function doSwitchModel(){
  const sel = $('modelSel'), msg = $('modelMsg');
  const key = sel && sel.value;
  if(!key){ return; }
  if(!await uiConfirm({ title:'切换主模型',
    message:`确认将主模型切换为「${key}」？切换立即生效，原主模型转为备用。`, okText:'切换' })) return;
  msg.textContent = '切换中…';
  try{
    await api('/api/admin/models/switch', { method:'POST', body: JSON.stringify({ key }) });
    msg.textContent = '✓ 已切换为 ' + key;
    await loadModels();
  }catch(e){ msg.textContent = '切换失败：' + e.message; }
}
async function statCardJump(key, card){
  const num = card ? (card.querySelector('.num')||{}).textContent||'' : '';
  switch(key){
    case 'users':       switchAdminTab('audit'); _clickChip('auditFilter','t','login_ok'); _scrollFlash('pane-audit'); break;
    case 'sessions':
    case 'questions':
    case 'unique':
    case 'satisfaction':switchAdminTab('audit'); _clickChip('auditFilter','t','chat');      _scrollFlash('pane-audit'); break;
    case 'leave':
      await openLeave(); switchLeaveTab('pending'); break;
    case 'reimburse':
      await openRb(); switchRbTab('pending'); break;
    case 'gaps':        switchAdminTab('gaps');  _clickChip('gapStatusFilter','s','pending'); _scrollFlash('pane-gaps'); break;
    case 'store':
      await uiConfirm({ title:'存储模式', message:`知识向量采用「${num}」，全部文档与检索数据均保存在您的内网服务器，不出公网。`, okText:'知道了', cancelText:'关闭' }); break;
    case 'llm':
      await uiConfirm({ title:'LLM 后端', message:`问答生成由「${num}」在本地完成，数据不出内网，满足私有化部署的合规要求。`, okText:'知道了', cancelText:'关闭' }); break;
    case 'cache':
      await uiConfirm({ title:'问答缓存', message:`命中缓存时直接返回已生成的答案（${num}），未命中才重新检索与生成，兼顾响应速度与内容新鲜度。`, okText:'知道了', cancelText:'关闭' }); break;
  }
}
/* ---- 知识缺口台账：后端记的是 department_id + 时间戳，这里换成中文部门名再展示 ---- */
let __gapReason = '';
let __gapStatus = '';
let __deptNames = null;
async function ensureDeptNames(){
  if(__deptNames) return __deptNames;
  try{
    // 用 /api/knowledge/departments 而不是 /api/admin/departments：
    // 缺口台账对"部门主管 / 知识审核员"同样开放，但后者不是 admin，
    // 调管理端接口会 403 并导致部门列退化成裸 id。
    const deps = await (await api('/api/knowledge/departments')).json();
    __deptNames = Object.fromEntries((deps||[]).map(d => [d.id, d.name]));
  }catch(e){ __deptNames = {}; }
  return __deptNames;
}
async function renderGaps(){
  const box = $('adminGaps');
  if(!box) return;
  try{
    const qs = new URLSearchParams({ top: '20' });
    if(__gapReason) qs.set('reason', __gapReason);
    if(__gapStatus) qs.set('status', __gapStatus);
    const d = await (await api('/api/admin/knowledge-gaps?' + qs.toString())).json();
    const names = await ensureDeptNames();
    const sum = d.summary || {};
    const items = d.items || [];
    const filtered = __gapReason || __gapStatus;
    if(!items.length){
      box.innerHTML = `<div class="empty" style="padding:16px 0">暂无知识缺口工单${filtered?'（当前筛选条件下）':''} —— 说明知识库目前接得住用户的提问</div>`;
      return;
    }
    const head = `<div style="font-size:12px;color:var(--muted);margin-bottom:6px">
      累计 <b style="color:var(--text)">${sum.total_unique||0}</b> 个问题被问 <b style="color:var(--text)">${sum.total_asks||0}</b> 次 ·
      待处理 <b style="color:#b45309">${sum.pending||0}</b> · 已补充 <b style="color:#047857">${sum.supplemented||0}</b> · 已关闭 ${sum.closed||0} ·
      收到用户反馈 <b style="color:var(--text)">${sum.feedback_total||0}</b> 条</div>`;
    box.innerHTML = head + items.map((g,i) => {
      const t = g.last_asked_at ? new Date(g.last_asked_at * 1000).toLocaleString('zh-CN') : '';
      const rlabel = g.reason === 'no_hit' ? '完全没命中' : g.reason === 'low_evidence' ? '命中但不相关' : '用户反馈';
      const st = g.status || 'pending';
      const fbs = g.feedbacks || [];
      // 反馈原因分布：同一原因被多人勾选时计数展示
      const tally = {};
      fbs.forEach(f => (f.reasons||[]).forEach(r => { tally[r] = (tally[r]||0) + 1; }));
      const reasonChips = Object.entries(tally).map(([r,n]) =>
        `<span class="gap-chip-sm">${esc(GAP_REASON_LABELS[r]||r)}${n>1?` ×${n}`:''}</span>`).join('');
      const lastNote = fbs.length && fbs[fbs.length-1].note
        ? `<div class="meta">最新补充说明：${esc(fbs[fbs.length-1].note)}</div>` : '';
      const res = g.resolution;
      const doneInfo = res ? `<div class="meta" style="color:#047857">处置人 ${esc(res.operator||'-')} · ${esc(res.resolved_at?new Date(res.resolved_at*1000).toLocaleString('zh-CN'):'')}${res.docs&&res.docs.length?' · 补充文档：'+esc((res.docs||[]).join('、')):''}${res.note?' · '+esc(res.note):''}</div>` : '';
      const actions = st === 'pending'
        ? `<div class="gap-ops">
             <button class="btn" style="padding:2px 8px;font-size:11px" onclick="assignGapById('${esc(g.id)}')">👤 指派</button>
             <button class="btn primary" style="padding:2px 8px;font-size:11px" onclick="resolveGapById('${esc(g.id)}')">✅ 标记已补充</button>
             <button class="btn" style="padding:2px 8px;font-size:11px" onclick="closeGapById('${esc(g.id)}')">🚫 关闭</button>
           </div>`
        : `<div class="gap-ops"><button class="btn" style="padding:2px 8px;font-size:11px" onclick="reopenGapById('${esc(g.id)}')">↩ 重新打开</button></div>`;
      return `<div class="gap-item ticket-${esc(st)}">
        <span class="gap-count">× ${g.count}</span>
        <div class="q">${i+1}. ${esc(g.question)}
          <span class="gap-reason ${esc(g.reason)}">${rlabel}</span>
          <span class="gap-status ${esc(st)}">${esc(g.status_label||st)}</span>
          ${fbs.length?`<span class="gap-count">反馈 ${fbs.length}</span>`:''}
          ${g.assignee?`<span class="gap-chip-sm">👤 ${esc(g.assignee)}</span>`:''}
          <div class="meta">${esc(names[g.department_id] || g.department_id)} · 最近由 ${esc(g.last_user||'未知用户')} 提问 · ${esc(t)}</div>
          ${reasonChips?`<div class="reason-line">${reasonChips}</div>`:''}
          ${lastNote}${doneInfo}${actions}
        </div>
      </div>`;
    }).join('');
  }catch(e){
    box.innerHTML = `<div class="empty" style="padding:16px 0">加载失败：${esc(e.message)}</div>`;
  }
}
// 工单状态英文 → 中文（与后端 gap_store.FEEDBACK_REASONS / STATUS_LABELS 对应）
const GAP_REASON_LABELS = { inaccurate:'答案不准确', not_found:'没有找到资料',
  irrelevant_cite:'引用不相关', outdated:'内容已过期' };
async function _gapOp(id, action, payloadBuilder, okMsg){
  try{
    await api(`/api/admin/knowledge-gaps/${encodeURIComponent(id)}/${action}`,
      { method:'POST', body: JSON.stringify(payloadBuilder ? payloadBuilder() : {}) });
    toast(okMsg, 'ok');
    await renderGaps(); await renderAudit(); await refreshAdminStats();
  }catch(e){ toast('操作失败：' + e.message, 'no'); }
}
function resolveGapById(id){
  // 两个输入合并为一个 antd Modal+Form 弹窗，一次填完
  AntdUI.formModal({
    title: '补充知识缺口工单',
    okText: '标记为已补充',
    validate: v => (!String(v.docs || '').trim() && !String(v.note || '').trim())
      ? '「已补充文档」与「处理说明」至少填写一项' : null,
    fields: [
      { name: 'docs', label: '已补充的文档名', type: 'text',
        placeholder: '多个用「、」分隔，可留空' },
      { name: 'note', label: '处理说明', type: 'textarea', rows: 3,
        placeholder: '可留空，会展示给提问用户' }
    ]
  }).then(v => {
    if(!v) return;
    const docs = String(v.docs || ''), note = String(v.note || '');
    _gapOp(id, 'resolve', () => ({ docs, note }), '已标记为「已补充」，提问人会收到通知');
  });
}
function assignGapById(id){
  AntdUI.formModal({
    title: '指派工单',
    okText: '确认指派',
    fields: [{ name: 'who', label: '指派给', type: 'text', required: true,
      requiredMsg: '请填写被指派人', placeholder: '填用户名或姓名' }]
  }).then(v => {
    const who = (v && v.who || '').trim();
    if(!who) return;
    _gapOp(id, 'assign', () => ({ assignee: who }), `已指派给 ${who}`);
  });
}
function closeGapById(id){
  AntdUI.formModal({
    title: '关闭工单',
    okText: '确认关闭',
    fields: [{ name: 'note', label: '关闭原因', type: 'textarea', rows: 3,
      placeholder: '如：不属于知识库范围 / 已线下解答' }]
  }).then(v => {
    if(!v) return;
    const note = String(v.note || '').trim() || '不在范围内';
    _gapOp(id, 'close', () => ({ note }), '工单已关闭');
  });
}
function reopenGapById(id){
  _gapOp(id, 'reopen', () => ({}), '工单已回到待处理');
}
// 只重绘"知识缺口"那张统计卡，避免整体刷新打断管理员正在看的位置
async function refreshAdminStats(){
  try{
    const st = await (await api('/api/admin/stats')).json();
    const g = st.knowledge_gaps || {};
    document.querySelectorAll('#adminStats .stat-card').forEach(c => {
      if(((c.querySelector('.lbl')||{}).textContent || '').includes('知识缺口')){
        const n = c.querySelector('.num');
        if(n){
          const pend = g.pending || 0;
          n.innerHTML = `${esc(String(g.total_unique||0))}<span class="num-sub">${esc(`（${g.total_asks||0} 次）`)}</span>` +
            (pend > 0 ? `<span class="pend-badge" title="待处理 ${pend} 条">${pend}</span>` : '');
        }
        const pend = g.pending || 0;
        let b = c.querySelector('.pend-badge');
        if(pend > 0){
          if(!b){ b = document.createElement('span'); b.className = 'pend-badge'; c.appendChild(b); }
          b.textContent = pend; b.title = `待处理 ${pend} 条`;
        }else if(b){ b.remove(); }
      }
    });
  }catch(e){}
}

async function renderAudit(){
  try{
    const q = __auditType ? `&type=${encodeURIComponent(__auditType)}` : '';
    const d = await (await api('/api/admin/audit-logs?limit=50' + q)).json();
    const rows = (d.items||[]).map(i => {
      const t = new Date(i.ts * 1000).toLocaleString('zh-CN');
      return `<tr><td>${esc(t)}</td><td>${esc(TYPE_LABELS[i.type]||i.type)}</td><td>${esc(i.user_name||i.user_id||'')}</td><td>${esc(i.detail||'')}</td></tr>`;
    }).join('');
    $('auditBody').innerHTML = rows || '<tr><td colspan="4" style="color:var(--muted)">暂无日志</td></tr>';
  }catch(e){
    $('auditBody').innerHTML = `<tr><td colspan="4">加载失败：${esc(e.message)}</td></tr>`;
  }
}

/* ============ 登录后初始化（会话 / 角标 / 轮询） ============ */
function finishLogin(){
  // 切换/登录账号：会话区整体重置为该账号自己的列表，收起所有功能弹窗并复位导航高亮
  S.sessionId = null; S.sessions = [];
  __NAV.forEach(([m]) => { $(m).style.display = 'none'; });
  syncNavActive();
  renderSessions(); emptyState();
  if(S.user) applyRoleMenu(S.user.role);
  loadSessions().catch(()=>{});
  // 请假角标 + 未读审批通知 + 每 15s 轮询
  refreshLeaveBadge(); checkLeaveNotify();
  refreshRbBadge(); checkRbNotify();
  checkGapNotify();
  if(!window.__leavePoll){
    window.__leavePoll = setInterval(() => {
      refreshLeaveBadge(); checkLeaveNotify();
      refreshRbBadge(); checkRbNotify();
      checkGapNotify();
      refreshNotifyBadge();
    }, 15000);
  }
  $('notifyWrap').style.display = '';
  refreshNotifyBadge();
  fetchHealth();   // 登录后刷新右上角知识健康度环形图
}

/* ============ 消息通知中心（铃铛 + 未读角标 + 下拉列表） ============ */
let __notifyOpen = false;
async function refreshNotifyBadge(){
  if(!S.token) return;
  try{
    const d = await (await api('/api/notifications')).json();
    const b = $('notifyBadge');
    if(d.unread > 0){ b.textContent = d.unread > 99 ? '99+' : d.unread; b.style.display = ''; }
    else b.style.display = 'none';
    if(__notifyOpen) renderNotifyList(d.items || []);
  }catch(e){ /* 静默：通知失败不打断主流程 */ }
}
function renderNotifyList(items){
  const box = $('notifyList');
  box.innerHTML = items.length ? items.map(n=>`
    <div class="notify-item${n.read?'':' unread'}" data-nid="${esc(n.id)}">
      <div class="nt-title">${n.read?'':'<span class="notify-dot"></span>'}${esc(n.title||'通知')}</div>
      <div class="nt-msg">${esc(n.message||'')}</div>
      <div class="nt-time">${n.created_at ? new Date(n.created_at*1000).toLocaleString('zh-CN') : ''}</div>
    </div>`).join('')
    : '<div style="padding:20px;text-align:center;font-size:12px;color:var(--muted)">暂无通知</div>';
  box.querySelectorAll('.notify-item.unread').forEach(el => {
    el.onclick = () => markNotifyRead([el.dataset.nid]);
  });
}
async function markNotifyRead(ids){
  try{
    await api('/api/notifications/read', { method:'POST', body: JSON.stringify({ ids }) });
    await refreshNotifyBadge();
  }catch(e){}
}
async function toggleNotifyPanel(){
  const p = $('notifyPanel');
  __notifyOpen = p.style.display === 'none';
  p.style.display = __notifyOpen ? 'flex' : 'none';
  if(__notifyOpen){
    $('notifyList').innerHTML = '<div style="padding:16px;font-size:12px;color:var(--muted)">加载中…</div>';
    await refreshNotifyBadge();
  }
}

/* ============ 知识目录 卡片流（首页 / 知识服务入口） ============ */
// 复用既有只读接口 /api/knowledge/docs（不改动任何接口），前端就地派生
// 摘要、状态与分页/筛选/排序，零后端改动。
// 状态色标：已发布(绿) / 审核中(黄) / 已过期(红) / 无权访问(灰)。
// 当前只读接口不返回 status（只返回本人有权文档），故统一判定为「已发布」；
// 生产环境可由后端在 docs 响应中补充 status 字段（新增字段，向后兼容），前端已就绪。
const __CAT_PAGE_SIZE = 8;
let __catalogAll = [];
let __catalogPage = 1;
function openCatalog(){
  $('catalogView').style.display = 'flex';
  $('adminView').style.display = 'none';   // 从管理看板直接切目录时，收起看板页
  $('chat') && ($('chat').style.display = 'none');
  fetchCatalog();
}
function closeCatalog(){
  $('catalogView').style.display = 'none';
  $('chat').style.display = 'flex';
}
// 根据筛选/排序条件算出当前视图列表
function catalogView(){
  const dept = $('catDeptFilter').value;
  const sort = $('catSort').value;
  let list = __catalogAll.filter(d => !dept || (d.department_id === dept) || (d.department_name === dept));
  if(sort === 'dept') list = list.slice().sort((a,b)=> (a.department_name||a.department_id).localeCompare(b.department_name||b.department_id,'zh'));
  else if(sort === 'name') list = list.slice().sort((a,b)=> a.doc_name.localeCompare(b.doc_name,'zh'));
  else if(sort === 'time') list = list.slice().sort((a,b)=> (b.updated_at||0) - (a.updated_at||0));   // 最近更新在前
  return list;
}
function renderCatalogGrid(){
  const grid = $('catalogGrid');
  const list = catalogView();
  const shown = list.slice(0, __catalogPage * __CAT_PAGE_SIZE);
  if(!list.length){ grid.innerHTML = '<div class="empty" style="padding:40px 0">暂无可访问的文档</div>'; $('catMoreWrap').style.display='none'; return; }
  const tones = ['tone-1','tone-2','tone-3','tone-4'];
  grid.innerHTML = shown.map((d,i)=>{
    const dept = d.department_name || d.department_id;
    // 四色状态：后端 /docs 已返回 status 与 updated_at（无数据时不伪造时间）
    const ST = { published:{t:'已发布', c:'ok'}, reviewing:{t:'审核中', c:'review'},
                 expired:{t:'已过期', c:'expired'}, forbidden:{t:'无权访问', c:'nodep'} };
    const status = ST[d.status] || ST.published;
    const upd = d.updated_at ? new Date(d.updated_at*1000).toLocaleDateString('zh-CN') : '';
    const summary = `《${esc(d.doc_name)}》· ${esc(dept)} 部门知识库文档${upd ? '，更新于 ' + upd : ''}。${d.status==='expired' ? ' 内容已超过有效期，建议责任部门复核更新。' : ''}`;
    return `
    <div class="kc-card ${tones[i % tones.length]}">
      <div class="kc-top">
        <span class="kc-icon">📄</span>
        <span class="kc-title">${esc(d.doc_name)}</span>
      </div>
      <p class="kc-summary">${summary}</p>
      <div class="kc-meta">
        <span class="kc-chip">${esc(dept)}</span>
        ${upd ? `<span class="kc-chip">更新 ${upd}</span>` : ''}
        <span class="kc-status ${status.c}">${status.t}</span>
      </div>
    </div>`;
  }).join('');
  $('catMoreWrap').style.display = shown.length < list.length ? 'block' : 'none';
  $('catCount').textContent = `共 ${list.length} 份${shown.length < list.length ? `，已显示 ${shown.length}` : ''}`;
}
async function fetchCatalog(){
  const grid = $('catalogGrid');
  grid.innerHTML = '<div class="empty" style="padding:40px 0">加载中…</div>';
  try{
    const docs = await (await api('/api/knowledge/docs')).json();
    __catalogAll = docs || [];
    __catalogPage = 1;
    // 部门筛选下拉：取当前用户可访问的部门去重
    const depts = {};
    __catalogAll.forEach(d => { const k = d.department_id; depts[k] = d.department_name || d.department_id; });
    $('catDeptFilter').innerHTML = '<option value="">全部部门</option>' +
      Object.keys(depts).map(k => `<option value="${esc(k)}">${esc(depts[k])}</option>`).join('');
    renderCatalogGrid();
  }catch(e){
    grid.innerHTML = `<div class="empty" style="padding:40px 0">加载失败：${esc(e.message)}</div>`;
  }
}

/* ============ 知识健康度 环形图（调用新增只读接口 /api/knowledge/health） ============ */
const __HR_CIRC = 2 * Math.PI * 17.5;   // 与 SVG r=17.5 对应
let __healthData = null;                // 缓存最近一次健康度数据，供归因弹窗使用
async function fetchHealth(){
  const fill = $('hrFill'), score = $('hrScore'), ring = $('healthRing');
  if(!fill) return;
  try{
    const d = await (await api('/api/knowledge/health')).json();
    __healthData = d;
    const s = Math.max(0, Math.min(100, d.score || 0));
    fill.style.strokeDasharray = __HR_CIRC;
    fill.style.strokeDashoffset = __HR_CIRC * (1 - s/100);
    fill.style.stroke = s >= 80 ? 'var(--primary)' : (s >= 60 ? 'var(--gold)' : 'var(--danger)');
    score.textContent = s;
    ring.classList.remove('lv-good','lv-warn','lv-risk');
    ring.classList.add(s >= 80 ? 'lv-good' : (s >= 60 ? 'lv-warn' : 'lv-risk'));
    ring.title = '知识健康度 ' + s + ' 分 · 点击查看归因';
    ring.style.cursor = 'pointer';
    ring.onclick = openHealthDetail;
  }catch(e){
    score.textContent = '--';
  }
}
function openHealthDetail(){
  const d = __healthData; const box = $('healthBody');
  if(!d){ box.innerHTML = '<div class="empty">健康度数据加载中，请稍后重试。</div>'; }
  else {
    const comps = d.components || {};
    const bd = d.breakdown || {};
    const pct = v => Math.round((v||0) * 100);
    const compRows = Object.values(comps).map(c => `
      <div class="hb-row">
        <span class="hb-name">${c.label}<i class="hb-w">权重 ${(c.weight*100)|0}%</i></span>
        <span class="hb-bar"><i style="width:${pct(c.value)}%;background:${c.value>=0.8?'var(--success)':(c.value>=0.6?'var(--gold)':'var(--danger)')}"></i></span>
        <span class="hb-val">${pct(c.value)}%</span>
      </div>`).join('');
    const miss = (bd.missing_departments||[]).length
      ? (bd.missing_departments||[]).map(esc).join('、') : '无（全部部门已覆盖）';
    box.innerHTML = `
      <div class="hb-score">综合健康度 <b>${d.score}</b> / 100 <span class="hb-level ${d.level}">${d.level==='good'?'良好':d.level==='warn'?'需关注':'风险'}</span></div>
      <div class="hb-formula">综合分 = 用户满意度×40% + 知识缺口闭环率×30% + 部门文档覆盖率×30%</div>
      <div class="hb-comp">${compRows}</div>
      <div class="hb-attr">
        <div class="hb-attr-title">扣分项归因</div>
        <ul>
          <li>未覆盖部门：<b>${miss}</b></li>
          <li>待处理知识缺口：<b>${bd.pending_gaps ?? d.pending_gaps ?? 0}</b> 条（用户反馈但管理员尚未补充资料）</li>
          <li>不满意反馈：<b>${bd.negative ?? 0}</b> 次（点「没帮助」的回答）</li>
          <li>已入库文档：<b>${d.docs_total ?? 0}</b> 份 · 覆盖 <b>${d.depts_covered ?? 0}/${d.depts_total ?? 0}</b> 个部门</li>
        </ul>
      </div>
      <div class="hb-tip">提示：提升健康度的有效动作 —— 补充未覆盖部门文档、及时响应知识缺口工单、优化低满意度回答。</div>`;
  }
  openMask('healthMask');
}

/* ============ 强制修改初始密码（FR-AUTH-01） ============ */
function openPwdModal(){
  $('pwdMask').style.display = 'flex';
  $('pwdErr').textContent = '';
  $('pwdOld').value = ''; $('pwdNew').value = ''; $('pwdNew2').value = '';
}
async function submitPwd(){
  const oldP = $('pwdOld').value, newP = $('pwdNew').value, newP2 = $('pwdNew2').value;
  $('pwdErr').textContent = '';
  if(newP.length < 8){ $('pwdErr').textContent = '新密码至少 8 位'; return; }
  if(newP !== newP2){ $('pwdErr').textContent = '两次输入的新密码不一致'; return; }
  try{
    const r = await fetch(`${$('apiBase').value}/api/auth/change-password`, {
      method:'POST', headers:{'Content-Type':'application/json','Authorization':'Bearer '+S.token},
      body: JSON.stringify({ old_password:oldP, new_password:newP })
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.message || d.detail || '修改失败');
    S.user.must_change_password = false;
    $('pwdMask').style.display = 'none';
    toast('密码修改成功，已可正常使用系统', 'ok');
    finishLogin();   // 改密完成后才真正进入系统
  }catch(e){ $('pwdErr').textContent = e.message; }
}

/* ============ 文档管理（上传 / 删除 / 版本回滚，FR-KB-05） ============ */
// 可管理角色：管理员、总经理、总监(经理)、主管 —— 与后端 _assert_can_upload 的 can_upload 校验一致
const __DOC_SCOPE_ADM = 'adm', __DOC_SCOPE_DLG = 'dlg';
function _docCards(box, scope){
  return docs => {
    if(!docs || !docs.length){ box.innerHTML = '<div class="empty" style="padding:16px 0">暂无可管理文档</div>'; return; }
    /* 表格化：文件名 | 版本 | 所属部门 | 操作，一屏 10+ 条；版本历史展开在文档行下方 */
    box.innerHTML = `<table class="doc-table">
      <thead><tr><th>文件名</th><th style="width:76px">版本</th><th style="width:120px">所属部门</th><th style="width:230px">操作</th></tr></thead>
      <tbody>${docs.map(d => `
        <tr>
          <td class="dt-name" title="${esc(d.doc_name)}">${esc(d.doc_name)}</td>
          <td><span class="lv-tag lv-wait">v${d.version}</span></td>
          <td>${esc(d.department_name||d.department_id)}</td>
          <td class="dt-ops">
            <button class="btn" style="padding:4px 10px;font-size:12px" onclick="showVersions('${esc(d.doc_name)}','${esc(d.department_id)}','${scope}')">查看版本</button>
            <button class="btn" style="padding:4px 10px;font-size:12px;margin-left:6px" onclick="rollbackPrev('${esc(d.doc_name)}','${esc(d.department_id)}',${d.version})">回滚</button>
            <button class="btn" style="padding:4px 10px;font-size:12px;margin-left:6px;color:#c0392b" onclick="deleteDoc('${esc(d.doc_name)}','${esc(d.department_id)}')">删除</button>
          </td>
        </tr>
        <tr class="ver-tr"><td colspan="4"><div id="ver-${scope}-${esc(d.department_id)}_${esc(d.doc_name)}" style="display:none"></div></td></tr>`).join('')}
      </tbody></table>`;
  };
}
async function renderDocs(){
  try{ _docCards($('adminDocs'), __DOC_SCOPE_ADM)(await (await api('/api/knowledge/docs')).json()); }
  catch(e){ $('adminDocs').innerHTML = `<div class="empty" style="padding:16px 0">加载失败：${esc(e.message)}</div>`; }
}
/* 文档删除审批（管理员）：通过后才真正删除 */
async function renderDelReqs(){
  const box = $('adminDelReqs');
  try{
    const d = await (await api('/api/admin/doc-deletions?status=pending')).json();
    const items = d.items || [];
    if(!items.length){ box.innerHTML = '<div class="empty" style="padding:12px 0">暂无待审批的删除申请</div>'; return; }
    box.innerHTML = items.map(r => `
      <div class="lv-card">
        <div class="lv-head"><b>${esc(r.doc_name)}</b>
          <span class="lv-id">${esc(r.department_id)}</span></div>
        <div style="font-size:12px;color:var(--muted);margin:2px 0 6px">申请人：${esc(r.requested_by_name || r.requested_by)} · ${new Date(r.requested_at*1000).toLocaleString('zh-CN')}</div>
        <div class="lv-actions">
          <button class="btn primary" style="padding:3px 12px;font-size:12px" onclick="reviewDocDel('${esc(r.id)}',true)">同意删除</button>
          <button class="btn" style="padding:3px 12px;font-size:12px" onclick="reviewDocDel('${esc(r.id)}',false)">驳回</button>
        </div>
      </div>`).join('');
  }catch(e){ box.innerHTML = `<div class="empty" style="padding:12px 0">加载失败：${esc(e.message)}</div>`; }
}
async function reviewDocDel(rid, approve){
  if(!approve && !await uiConfirm({ title:'驳回删除申请', message:'驳回该删除申请？文档将保持不变。', okText:'驳回' })) return;
  try{
    const r = await (await api('/api/admin/doc-deletions/' + encodeURIComponent(rid) + (approve?'/approve':'/reject'), { method:'POST' })).json();
    toast(approve ? `已删除「${r.doc_name}」` : '已驳回该申请', 'ok');
    await renderDelReqs();
    await renderDocs();
    renderTodo().catch(()=>{});
    if($('docMask').style.display === 'flex') await renderDocList();
  }catch(e){ toast('操作失败：' + e.message, 'no'); }
}
async function renderDocList(){
  try{ _docCards($('docList'), __DOC_SCOPE_DLG)(await (await api('/api/knowledge/docs')).json()); }
  catch(e){ $('docList').innerHTML = `<div class="empty" style="padding:16px 0">加载失败：${esc(e.message)}</div>`; }
}
async function openDocs(){
  openMask('docMask');
  $('docMsg').textContent = '';
  // 部门下拉只列出当前用户有上传权限的部门（无权限的部门直接隐藏；
  // 拥有全部门上传权限的管理员/总经理会看到全部）
  try{
    const deps = await (await api('/api/knowledge/uploadable-departments')).json();
    $('docDept').innerHTML = (deps||[]).map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  }catch(e){ $('docDept').innerHTML = '<option value="">部门加载失败</option>'; }
  await renderDocList();
}
async function doUpload(){
  const f = $('docFile').files[0];
  if(!f){ $('docMsg').textContent = '请先选择要上传的文件'; return; }
  if(!$('docDept').value){ $('docMsg').textContent = '请选择目标部门'; return; }
  const fd = new FormData();
  fd.append('file', f);
  fd.append('department_id', $('docDept').value);
  $('docMsg').textContent = '正在上传并解析入库…';
  try{
    const r = await (await api('/api/knowledge/upload', { method:'POST', body: fd })).json();
    $('docMsg').textContent = r.skipped
      ? `内容未变化，已跳过：${r.doc_name} 当前仍为 v${r.version}`
      : `已入库：${r.doc_name} v${r.version}（${r.chunk_count} 个片段）`;
    $('docFile').value = '';
    await renderDocList();
  }catch(e){ $('docMsg').textContent = '上传失败：' + e.message; }
}
async function deleteDoc(doc, dept){
  const isAdmin = S.user && S.user.role === 'admin';
  if(isAdmin){
    if(!await uiConfirm({ title:'删除文档', message:`确认删除文档「${doc}」？其向量与版本历史将一并移除，不可恢复。`, okText:'删除' })) return;
  }else{
    if(!await uiConfirm({ title:'提交删除申请', message:`删除文档「${doc}」需管理员审批。确认提交删除申请？`, okText:'提交申请' })) return;
  }
  try{
    const r = await (await api('/api/knowledge/docs/' + encodeURIComponent(doc) + '?department_id=' + encodeURIComponent(dept), { method:'DELETE' })).json();
    if(r.pending_approval){
      toast('已提交删除申请，等待管理员审批', 'ok');
      $('docMsg').textContent = `「${doc}」的删除申请已提交，管理员审批通过后才会真正删除。`;
    }else{
      toast(`已删除「${doc}」`, 'ok');
    }
    await renderDocList();
    if($('adminView').style.display === 'flex'){ await renderDocs(); await renderDelReqs(); }
  }catch(e){ toast('删除失败：' + e.message, 'no'); }
}
async function showVersions(doc, dept, scope){
  scope = scope || __DOC_SCOPE_ADM;
  const box = $('ver-' + scope + '-' + dept + '_' + doc);
  if(box.style.display === 'block'){ box.style.display = 'none'; return; }
  box.style.display = 'block';
  box.innerHTML = '加载中…';
  try{
    const v = await (await api('/api/knowledge/versions/'+encodeURIComponent(doc)+'?department_id='+encodeURIComponent(dept))).json();
    const hist = (v.history||[]).map(h => `
      <div style="font-size:12px;padding:4px 0;border-bottom:1px dashed var(--border)">
        v${h.version} · ${esc(h.uploaded_by)} · ${new Date(h.uploaded_at*1000).toLocaleString('zh-CN')}
        ${h.change_note?(' · '+esc(h.change_note)):''}
        <button class="btn" style="padding:3px 10px;font-size:12px;margin-left:6px" onclick="rollbackDoc('${esc(doc)}','${esc(dept)}',${h.version})">回滚到此版</button>
      </div>`).join('');
    box.innerHTML = hist || '<div style="font-size:12px;color:var(--muted)">暂无历史版本</div>';
  }catch(e){ box.innerHTML = '加载失败：'+esc(e.message); }
}
/* 「回滚」按钮：直接回滚到上一个版本（v-1），避免用户要点进历史才能操作；
   真正的版本选择仍在「查看版本」展开的历史列表里，两步都带二次确认。 */
async function rollbackPrev(doc, dept, cur){
  const target = (cur || 0) - 1;
  if(target < 1){ toast('该文档当前为 v1，暂无历史版本可回滚', 'warn'); return; }
  await rollbackDoc(doc, dept, target);
}
async function rollbackDoc(doc, dept, ver){
  if(!await uiConfirm({ title:'版本回滚', message:`确认将「${doc}」回滚到 v${ver}？将生成新的递增版本号，原始历史保留。`, okText:'回滚' })) return;
  try{
    await api('/api/knowledge/versions/'+encodeURIComponent(doc)+'/rollback', {method:'POST',
      body: JSON.stringify({ department_id:dept, version:ver })});
    toast(`已回滚「${doc}」到 v${ver}`, 'ok');
    if($('docMask').style.display === 'flex') await renderDocList();
    if($('adminView').style.display === 'flex') await renderDocs();
  }catch(e){ toast('回滚失败：'+e.message, 'no'); }
}

/* ============ 权限申请弹窗（我的申请 / 待我审批 / 已处理 / 权限配置） ============ */
// 四个页签按角色显隐，与后端校验保持一致：
//   员工      → 我的申请（可发起、可撤回）
//   主管/高管  → + 待我审批（通过 / 驳回）+ 已处理（复盘批过什么）
//   管理员    → + 权限配置（直接分配跨部门授权，不走审批流）
const PERM_TAB = { mine:'permPaneMine', approve:'permPaneApprove',
                   history:'permPaneHistory', config:'permPaneConfig' };
function switchPermTab(key){
  for(const [k, paneId] of Object.entries(PERM_TAB)){
    $(paneId).style.display = (k === key) ? '' : 'none';
  }
  ['permTabMine','permTabApprove','permTabHistory','permTabConfig'].forEach(id => {
    const el = $(id);
    if(el) el.classList.toggle('active', el.dataset.pane === PERM_TAB[key]);
  });
  if(key === 'mine') renderPermMine();
  if(key === 'approve') renderPermPending();
  if(key === 'history') renderPermHistory();
  if(key === 'config') loadGrantsConfig();
}
const PERM_STATUS = {
  pending:'<span class="lv-tag lv-wait">待审批</span>',
  approved:'<span class="lv-tag lv-ok">已通过</span>',
  rejected:'<span class="lv-tag lv-no">已驳回</span>',
  canceled:'<span class="lv-tag" style="background:#f1f5f9;color:var(--muted)">已撤回</span>',
};
// 权限类型与有效期：企业里"能看"和"能把文件带走"是两件事；授权必须有期限，不能一次永久
const PERM_TYPE_CN = { read:'只读', download:'可下载' };
function permTypeTag(t){
  const cn = PERM_TYPE_CN[t] || '只读';
  const isDl = t === 'download';
  return `<span class="lv-tag" style="background:${isDl?'#fff7ed':'#eff6ff'};color:${isDl?'#b45309':'#1d4ed8'}">${cn}</span>`;
}
function permExpiryText(exp){
  if(!exp) return '长期有效';
  const d = new Date(exp * 1000).toLocaleDateString('zh-CN');
  return (Date.now() / 1000 >= exp) ? `已于 ${d} 到期` : `有效期至 ${d}`;
}
// 「提交 → 审批 → 生效」三步进度，让员工知道申请走到哪一步了
function permSteps(status){
  const step = (n, label, done, bad) =>
    `<span class="perm-step">${done ? '✅' : bad ? '❌' : '⬜'} ${n}. ${label}</span>`;
  if(status === 'approved') return step(1,'提交',true)+step(2,'审批通过',true)+step(3,'权限已开通',true);
  if(status === 'rejected') return step(1,'提交',true)+step(2,'已驳回',false,true)+step(3,'未生效',false,true);
  if(status === 'canceled') return step(1,'提交',true)+step(2,'已撤回',false,true)+step(3,'未生效',false,true);
  return step(1,'提交',true)+step(2,'等待主管审批',false)+step(3,'通过后自动开通',false);
}
const DEPT_NAME = id => ({ public:'公共知识库' }[id] || id);

async function openPerm(){
  // 部门下拉：只列出还没权限的部门（已有权限的无需重复申请）
  const sel = $('permDept');
  const owned = new Set(S.user?.accessible_depts || []);
  try{
    const depts = await (await api('/api/knowledge/departments')).json();
    const list = (Array.isArray(depts) ? depts : depts.items || []).filter(d => !owned.has(d.id));
    sel.innerHTML = list.length
      ? '<option value="">请选择目标部门</option>'
        + list.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('')
      : '<option value="">您已可访问全部部门，无需申请</option>';
    sel.disabled = !list.length;
  }catch(e){ sel.innerHTML = '<option value="">加载失败</option>'; }

  // 页签按角色显隐：无权限的页签连入口都不给，避免出现"看得见点不动"
  const isAdmin = S.user?.role === 'admin';
  const canApprove = ['admin','executive','dept_director','sub_manager'].includes(S.user?.role);
  $('permTabApprove').style.display = canApprove ? '' : 'none';
  // 「已处理」给审批角色复盘用；员工的已结束单在「我的申请」里已能看到，不再重复给一个页签
  $('permTabHistory').style.display = canApprove ? '' : 'none';
  $('permTabConfig').style.display  = isAdmin ? '' : 'none';
  $('permMsg').textContent = '';
  $('permReason').value = '';
  openMask('permMask');
  switchPermTab('mine');
}

async function renderPermMine(){
  const box = $('permMineList');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">加载中…</div>';
  try{
    const d = await (await api('/api/permission/my')).json();
    const mine = d.items || [];
    box.innerHTML = mine.length ? mine.map(r=>`
      <div class="perm-row">
        <span><b>${esc(DEPT_NAME(r.department_id))}</b>
          ${permTypeTag(r.permission_type)}
          <span class="pr-meta">· ${esc(r.reason||'')}
            · ${permExpiryText(r.expires_at)}
            · ${new Date((r.created_at||Date.now()/1000)*1000).toLocaleString('zh-CN')}</span></span>
        <span style="margin-left:auto;display:flex;align-items:center;gap:8px">
          ${PERM_STATUS[r.status] || r.status}
          ${r.status === 'pending' ? `<button class="btn btn-danger" style="padding:2px 8px;font-size:11px"
               onclick="cancelPerm('${r.id}')">撤回</button>` : ''}
        </span>
      </div>
      <div style="padding:2px 4px 6px">${permSteps(r.status)}</div>
    `).join('') : '<div style="font-size:12px;color:var(--muted)">暂无申请记录。填上面的申请理由即可发起。</div>';
  }catch(e){ box.innerHTML = '加载失败：'+esc(e.message); }
}

async function renderPermHistory(){
  const box = $('permHistoryList');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">加载中…</div>';
  try{
    const d = await (await api('/api/permission/history')).json();
    const items = d.items || [];
    $('permHistoryMsg').textContent = items.length
      ? `共 ${items.length} 条已归档申请（通过 / 驳回 / 撤回）。`
      : '暂无已处理的申请记录。';
    box.innerHTML = items.length ? items.map(r=>`
      <div class="perm-row">
        <span><b>${esc(r.display_name||r.username)}</b> 申请 <b>${esc(DEPT_NAME(r.department_id))}</b>
          ${permTypeTag(r.permission_type)}
          <div class="pr-meta" style="margin-top:2px">${esc(r.reason||'（未填写）')}
            · ${permExpiryText(r.expires_at)}
            · ${new Date((r.created_at||Date.now()/1000)*1000).toLocaleString('zh-CN')}</div></span>
        <span style="margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0">
          ${PERM_STATUS[r.status] || r.status}
          <span class="pr-meta">${esc(r.decided_by||'')}</span>
        </span>
      </div>`).join('')
      : '<div style="font-size:12px;color:var(--muted)">暂无已处理记录。</div>';
  }catch(e){ box.innerHTML = '加载失败：'+esc(e.message); }
}

async function renderPermPending(){
  const box = $('permPendingList');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">加载中…</div>';
  try{
    const d = await (await api('/api/permission/pending')).json();
    const items = d.items || [];
    $('permPendingMsg').textContent = items.length
      ? `有 ${items.length} 条待审批申请，通过后权限立即生效。`
      : '当前没有待审批的申请。';
    box.innerHTML = items.length ? items.map(r=>`
      <div class="perm-row" style="align-items:flex-start">
        <span><b>${esc(r.display_name||r.username)}</b> 申请访问 <b>${esc(DEPT_NAME(r.department_id))}</b>
          ${permTypeTag(r.permission_type)}
          <div class="pr-meta" style="margin-top:2px">理由：${esc(r.reason||'（未填写）')}
            · ${permExpiryText(r.expires_at)}
            · ${new Date((r.created_at||Date.now()/1000)*1000).toLocaleString('zh-CN')}</div></span>
        <span style="margin-left:auto;display:flex;gap:6px;flex-shrink:0">
          <button class="btn primary" style="padding:2px 10px;font-size:11px"
            onclick="decidePerm('${r.id}','approve')">通过并开通</button>
          <button class="btn btn-danger" style="padding:2px 10px;font-size:11px"
            onclick="decidePerm('${r.id}','reject')">驳回</button>
        </span>
      </div>`).join('')
      : '<div style="font-size:12px;color:var(--muted)">暂无待审批申请。</div>';
  }catch(e){ box.innerHTML = '加载失败：'+esc(e.message); }
}

async function submitPerm(){
  const sel = $('permDept');
  const dept = sel.value;
  const reason = $('permReason').value.trim();
  const msg = $('permMsg');
  if(!dept){ msg.textContent = '请选择要申请的部门'; msg.style.color='var(--danger)'; return; }
  if(!reason){ msg.textContent = '请填写申请理由（审批人需要知道你为什么要用）'; msg.style.color='var(--danger)'; return; }
  try{
    await api('/api/permission/request', { method:'POST', body: JSON.stringify({
      department_id:dept, reason,
      permission_type: $('permType').value || 'read',
      days: $('permDays').value || '',      // 留空 = 长期有效；选了天数则到期自动失效
    }) });
    msg.textContent = '✓ 已提交，等待主管审批；审批通过后权限立即生效。';
    msg.style.color='var(--success)';
    $('permReason').value = '';
    await renderPermMine();
    toast('权限申请已提交', 'ok');
  }catch(e){ msg.textContent = e.message; msg.style.color='var(--danger)'; }
}

window.cancelPerm = async function(rid){
  try{
    if(!await uiConfirm({ title:'撤回申请', message:'撤回后该申请不再进入审批流，可重新发起。确定吗？', okText:'撤回' })) return;
    await api(`/api/permission/${rid}/cancel`, { method:'POST' });
    toast('已撤回申请', 'ok');
    await renderPermMine();
  }catch(e){ toast('撤回失败：'+e.message, 'no'); }
};

/* ============ 管理员的权限配置页（不走审批流，直接分配跨部门授权） ============ */
let __grantsMatrix = null;
async function loadGrantsConfig(){
  try{
    const d = await (await api('/api/admin/grants-matrix')).json();
    __grantsMatrix = d;
    const sel = $('permCfgUser');
    sel.innerHTML = (d.users||[]).map(u=>
      `<option value="${esc(u.username)}">${esc(u.display_name)}（${esc(u.role)} · ${esc(u.dept_name||'')}）</option>`).join('');
    if(sel.options.length) sel.value = sel.options[0].value;
    await renderPermConfig();
  }catch(e){ $('permCfgGrid').innerHTML = '加载失败：'+esc(e.message); }
}
async function renderPermConfig(){
  const uname = $('permCfgUser').value;
  const u = (__grantsMatrix?.users||[]).find(x => x.username === uname);
  if(!u){ $('permCfgGrid').innerHTML = ''; return; }
  const roleSet = new Set(u.builtin_departments || []);
  const granted = new Set(u.granted_departments || []);
  const meta = u.grant_meta || {};
  // 授权明细：谁给的、什么时候到期——合规检查必问"这个权限为什么有、什么时候失效"
  const expiryTxt = [...granted].map(d => {
    const m = meta[d] || {};
    const who = m.granted_by ? `（由 ${m.granted_by} 授予）` : '';
    return `${esc(DEPT_NAME(d))} ${permExpiryText(m.expires_at)}${esc(who)}`;
  }).join('；');
  $('permCfgHint').innerHTML =
    `「${esc(u.display_name)}」角色自带 ${roleSet.size} 个部门（<b>不可取消</b>，含公共库与本部门）；`
    + `下方勾选的是<b>额外授权</b>，当前共 ${granted.size} 个。保存只增减额外授权部分，不会误删角色自带权限。`
    + (expiryTxt ? `<div style="margin-top:4px;font-size:11px">现有授权：${expiryTxt}</div>` : '')
    + `<div style="margin-top:4px;font-size:11px">新勾选的授权有效期：<b>${esc($('permCfgDays')?.value ? $('permCfgDays').value + ' 天' : '长期有效')}</b>`
    + `，类型：<b>${esc($('permCfgType')?.value === 'download' ? '可下载' : '只读')}</b>（到期自动失效，无需人工回收）</div>`;
  $('permCfgGrid').innerHTML = (__grantsMatrix.departments||[]).map(d=>{
    const locked = roleSet.has(d.id);
    const checked = locked || granted.has(d.id);
    return `<label title="${locked ? '角色自带权限，不可取消' : '额外授权'}">
      <input type="checkbox" data-dept="${esc(d.id)}" ${checked?'checked':''} ${locked?'disabled':''}>
      <span class="${locked?'p-locked':''}">${esc(d.name)}${locked?'（角色自带）':''}</span></label>`;
  }).join('');
}
async function savePermConfig(){
  const uname = $('permCfgUser').value;
  const u = (__grantsMatrix?.users||[]).find(x => x.username === uname);
  if(!u){ toast('请先选择用户', 'warn'); return; }
  const roleSet = new Set(u.builtin_departments || []);
  const checked = [...document.querySelectorAll('#permCfgGrid input[data-dept]')]
    .filter(i => i.checked && !roleSet.has(i.dataset.dept))
    .map(i => i.dataset.dept);
  try{
    const d = await api('/api/admin/grants/bulk', { method:'POST',
      body: JSON.stringify({ username:uname, departments:checked,
        permission_type: $('permCfgType').value || 'read',
        days: $('permCfgDays').value || '' })}).then(r=>r.json());
    toast(d.changed && d.changed.length ? `已保存：${d.changed.join(' ')}` : '授权无变化', 'ok');
    await loadGrantsConfig();
  }catch(e){ toast('保存失败：'+e.message, 'no'); }
}

window.decidePerm = async function(rid, action){
  try{
    if(action === 'approve'){
      await api(`/api/permission/${rid}/approve`, { method:'POST' });
      toast('已通过并自动开通权限', 'ok');
    }else{
      await api(`/api/permission/${rid}/reject`, { method:'POST', body: JSON.stringify({ note:'' }) });
      toast('已驳回该申请', 'no');
    }
    await renderPermMine();
    if($('permPaneApprove').style.display !== 'none') await renderPermPending();
    if($('permPaneHistory').style.display !== 'none') await renderPermHistory();
  }catch(e){ toast('操作失败：'+e.message, 'no'); }
}

/* ============ 合规审计弹窗（只读：日志 + 异常检测 + 合规报告） ============ */
const AUDIT_TAB = { logs:'auditPaneLogs', risk:'auditPaneRisk', report:'auditPaneReport' };
function switchAuditTab(key){
  for(const [k, paneId] of Object.entries(AUDIT_TAB)){
    $(paneId).style.display = (k === key) ? '' : 'none';
  }
  ['auditTabLogs','auditTabRisk','auditTabReport'].forEach(
    id => $(id).classList.toggle('active', $(id).dataset.pane === AUDIT_TAB[key]));
  if(key === 'risk') loadRiskAlerts();
  if(key === 'report') loadComplianceReport();
}
const RISK_CN = { high:'高', medium:'中', low:'低' };
function riskTag(level){
  const cn = RISK_CN[level] || '低';
  const style = level === 'high'  ? 'background:#fee2e2;color:#b91c1c'
              : level === 'medium'? 'background:#fff7ed;color:#b45309'
              :                     'background:#f1f5f9;color:var(--muted)';
  return `<span class="lv-tag" style="${style}">${cn}风险</span>`;
}
// 时间范围 → 秒级时间戳，交给后端过滤（不把全量日志拉到浏览器再筛）
function auditSinceParam(){
  const v = $('auditRange').value;
  if(!v) return '';
  const days = parseFloat(v);
  if(!days) return '';
  const d = new Date(Date.now() - days*86400*1000);
  const fmt = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${fmt(d.getMonth()+1)}-${fmt(d.getDate())} ${fmt(d.getHours())}:${fmt(d.getMinutes())}:${fmt(d.getSeconds())}`;
}
function auditQuery(){
  const qs = new URLSearchParams();
  const since = auditSinceParam();
  if(since) qs.set('since', since);
  if($('auditAction').value) qs.set('type', $('auditAction').value);
  if($('auditUser').value.trim()) qs.set('user_id', $('auditUser').value.trim());
  if($('auditDept').value.trim()) qs.set('dept', $('auditDept').value.trim());
  if($('auditRisk').value) qs.set('risk', $('auditRisk').value);
  return qs.toString();
}
async function openCompliance(){
  openMask('complianceMask');
  const d = await api('/api/compliance/event-types').then(r=>r.json()).catch(()=>({items:[]}));
  const sel = $('auditAction');
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部操作</option>'
    + (d.items||[]).map(i=>`<option value="${esc(i.value)}">${esc(i.label)}</option>`).join('');
  sel.value = cur;
  // 合规报告默认统计当前月，避免管理员进来看到空月份
  const now = new Date();
  $('reportMonth').value = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  switchAuditTab('logs');
}
async function loadAuditLogs(){
  const box = $('auditList');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">加载中…</div>';
  try{
    const d = await (await api('/api/compliance/audit-logs?'+auditQuery())).json();
    const items = d.items || [];
    const riskHigh = (d.risk_high_24h ?? 0);
    $('auditStats').innerHTML = `共命中 <b>${d.total||0}</b> 条（显示最近 ${items.length} 条）`
      + (riskHigh ? ` · ⚠ 检出 <b style="color:var(--danger)">${riskHigh}</b> 项高风险行为`
                  : ' · 未检出高风险行为')
      + ' · <span style="color:var(--muted)">只读视图，不可修改或删除</span>';
    box.innerHTML = items.length ? items.map(a=>{
      const t = a.ts ? new Date(a.ts*1000).toLocaleString('zh-CN') : (a.time||'');
      return `
      <div style="font-size:12px;padding:5px 4px;border-bottom:1px dashed var(--border)">
        <span style="color:var(--muted);font-size:11px">${esc(t)}</span>
        · <b>${esc(a.user_name||a.user_id||'-')}</b>
        · <span class="tag">${esc(a.type||'')}</span>
        ${a.dept ? `· <span style="color:var(--muted)">${esc(a.dept)}</span>` : ''}
        ${riskTag(a.risk_level)}
        · ${esc(a.detail||'')}
      </div>`;}).join('')
      : '<div style="font-size:12px;color:var(--muted)">暂无匹配记录，试着放宽筛选条件。</div>';
  }catch(e){ box.innerHTML = '加载失败：'+esc(e.message); $('auditStats').textContent=''; }
}
// 导出：直接在浏览器侧触发下载，服务端返回文件流，不在服务器上留审计副本
// pdf 出的是带统计结论的合规报告（概览 + 类型分布 + 异常检测 + 日志明细），而非表格打印
function exportAudit(kind){
  const qs = auditQuery();
  const period = ($('auditPeriod') && $('auditPeriod').value) || '';
  const pe = period ? `&period=${period}` : '';
  const url = kind === 'json'
    ? `/api/compliance/export?format=json&limit=5000&${qs}`
    : kind === 'pdf'
      ? `/api/compliance/export?format=pdf&limit=5000${pe}&${qs}`
      : `/api/compliance/export?format=csv&limit=5000&${qs}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = kind === 'json' ? `audit_logs_${Date.now()}.json`
    : kind === 'pdf' ? `compliance_report_${Date.now()}.pdf`
      : `audit_logs_${Date.now()}.csv`;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  toast(`已导出${kind === 'json' ? ' JSON' : kind === 'pdf' ? ' PDF 合规报告' : ' CSV'}文件`, 'ok');
}
async function loadRiskAlerts(){
  const box = $('riskList');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">检测中…</div>';
  const win = $('riskWindow').value || '86400';
  try{
    const d = await (await api(`/api/compliance/risk-alerts?window=${win}`)).json();
    const risks = d.items || [];
    $('riskSummary').innerHTML = `扫描 <b>${d.scanned||0}</b> 条日志，检出 <b style="color:${d.high_alerts?'var(--danger)':'var(--success)'}">${d.total_alerts||0}</b> 项异常`
      + `（高风险 ${d.high_alerts||0} 项）`
      + `<div style="margin-top:4px;font-size:11px;color:var(--muted)">规则：`
      + (d.rules||[]).map(r=>`${r.code} ${r.name}（${r.default}）`).join('；') + '</div>';
    box.innerHTML = risks.length ? risks.map(a=>`
      <div class="risk-row ${a.level==='medium'?'medium':''}">
        <div class="rk-name">
          <span class="risk-lv ${a.level}">${a.level==='high'?'高':'中'}风险</span>
          ${esc(a.rule)} ${esc(a.rule_name)}
          <span class="pr-meta">· ${esc(a.user_name||a.user_id||'未知用户')}
            · ${a.ts ? new Date(a.ts*1000).toLocaleString('zh-CN') : ''}</span>
        </div>
        <div class="rk-detail">${esc(a.detail||'')}</div>
      </div>`).join('')
      : '<div style="font-size:12px;color:var(--success)">该时间窗口内未检出异常行为。</div>';
  }catch(e){ box.innerHTML = '检测失败：'+esc(e.message); $('riskSummary').textContent=''; }
}

/* ---- 合规报告（按月统计，供月度 / 季度合规报送归档） ---- */
let __report = null;
async function loadComplianceReport(){
  const box = $('reportBox');
  box.innerHTML = '<div style="color:var(--muted);font-size:12px">统计中…</div>';
  const m = $('reportMonth').value || '';
  try{
    const d = await (await api('/api/compliance/report' + (m ? `?month=${encodeURIComponent(m)}` : ''))).json();
    __report = d;
    const rd = d.risk_distribution || {};
    $('reportSummary').innerHTML = `<b>${esc(d.month||'')}</b> 合规报告`
      + ` · 统计区间 ${esc(d.from||'')} ~ ${esc(d.to||'')}`
      + ` · 由 ${esc(d.generated_by||'')} 于 ${esc(String(d.generated_at||'').replace('T',' '))} 生成`;
    const cells = [
      ['总操作数', d.total_operations], ['活跃用户', d.active_users],
      ['跨部门查询', d.cross_dept_queries], ['敏感部门访问', d.sensitive_access],
      ['权限变更', d.permission_changes], ['越权拦截', d.access_denied],
      ['异常项', d.alerts_total], ['高风险项', d.alerts_high],
    ];
    const top = Object.entries(d.by_type || {}).slice(0, 10);
    box.innerHTML = `
      <div class="report-grid">
        ${cells.map(([k,v])=>`<div class="report-cell"><div class="rc-num">${v??0}</div>
          <div class="rc-label">${k}</div></div>`).join('')}
      </div>
      <div style="margin-top:10px;font-size:12px">
        <b>风险分布</b>：${riskTag('high')} ${rd.high||0} · ${riskTag('medium')} ${rd.medium||0} · ${riskTag('low')} ${rd.low||0}
      </div>
      <div style="margin-top:6px;font-size:12px"><b>操作类型分布（前 10）</b><br>
        ${top.length ? top.map(([k,v])=>`<span class="tag" style="margin:2px 4px 2px 0">${esc(k)} · ${v}</span>`).join('')
                     : '<span style="color:var(--muted)">本月无记录</span>'}
      </div>
      ${(d.cross_dept_departments||[]).length
        ? `<div style="margin-top:6px;font-size:12px"><b>查询涉及部门</b>：${esc((d.cross_dept_departments||[]).join('、'))}</div>` : ''}
      ${(d.alert_items||[]).length ? `<div style="margin-top:8px;font-size:12px"><b>异常明细（最多 20 条）</b>
        ${(d.alert_items||[]).map(a=>`
          <div class="risk-row ${a.level==='medium'?'medium':''}" style="margin-top:4px">
            <div class="rk-name">${riskTag(a.level)} ${esc(a.rule)} ${esc(a.rule_name)}
              <span class="pr-meta">· ${esc(a.user_name||a.user_id||'')}${a.ts ? ' · '+new Date(a.ts*1000).toLocaleString('zh-CN') : ''}</span></div>
            <div class="rk-detail">${esc(a.detail||'')}</div>
          </div>`).join('')}</div>` : ''}
      <div style="margin-top:8px;font-size:11px;color:var(--muted)">
        说明：报告为只读统计，不含原文内容；可导出 CSV 交审计部门归档。
      </div>`;
  }catch(e){ box.innerHTML = '生成失败：'+esc(e.message); $('reportSummary').textContent=''; }
}
function exportReportCsv(){
  const d = __report;
  if(!d){ toast('请先生成报告', 'warn'); return; }
  const rd = d.risk_distribution || {};
  const rows = [
    ['项目', '数值'],
    ['统计月份', d.month],
    ['统计区间', `${d.from} ~ ${d.to}`],
    ['总操作数', d.total_operations],
    ['活跃用户', d.active_users],
    ['跨部门查询次数', d.cross_dept_queries],
    ['敏感部门访问次数', d.sensitive_access],
    ['权限变更次数', d.permission_changes],
    ['越权拦截次数', d.access_denied],
    ['异常项', d.alerts_total],
    ['高风险项', d.alerts_high],
    ['高风险日志数', rd.high || 0],
    ['中风险日志数', rd.medium || 0],
    ['低风险日志数', rd.low || 0],
    ['生成人', d.generated_by || ''],
    ['生成时间', d.generated_at || ''],
    [],
    ['操作类型', '次数'],
    ...Object.entries(d.by_type || {}).map(([k, v]) => [k, v]),
  ];
  const csv = '﻿' + rows.map(r => r.map(c => `"${String(c ?? '').replace(/"/g, '""')}"`).join(',')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `compliance_report_${d.month || 'current'}.csv`;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
  toast('合规报告已导出', 'ok');
}

/* ============ 事件绑定 ============ */
$('btnLogin').onclick = doLogin;
$('btnCatalog').onclick = openCatalog;
$('btnCatalogBack').onclick = closeCatalog;
$('catDeptFilter').onchange = () => { __catalogPage = 1; renderCatalogGrid(); };
$('catSort').onchange = () => { __catalogPage = 1; renderCatalogGrid(); };
$('catMore').onclick = () => { __catalogPage++; renderCatalogGrid(); };
$('loginPwd').onkeydown = e => { if(e.key==='Enter') doLogin(); };
$('btnSwitch').onclick = () => doLogout();
$('btnLogout').onclick = () => doLogout();
$('btnRegenerate').onclick = () => { if(!S.sending && S.lastQuestion) send(S.lastQuestion); };
$('btnClear').onclick = async () => {
  if(!await uiConfirm({ title:'清空对话', message:'将清空当前会话的全部消息并开始新会话，确定吗？', okText:'清空' })) return;
  await newSession();
  toast('已开始新会话', 'ok');
};
$('btnNewSession').onclick = () => newSession();
$('btnSend').onclick = () => { const v = $('input').value; $('input').value=''; send(v); };
$('btnStop').onclick = stopGeneration;
$('input').onkeydown = e => {
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); const v=$('input').value; $('input').value=''; $('input').style.height='auto'; send(v); }
};
$('input').oninput = function(){ this.style.height='auto'; this.style.height=Math.min(Math.max(this.scrollHeight,64),140)+'px'; };
document.querySelectorAll('.quick-tag').forEach(b=>{
  b.onclick = () => { $('input').value = b.dataset.q; send(b.dataset.q); $('input').value=''; };
});
$('btnHealth').onclick = async () => {
  openMask('sysHealthMask');
  const box = $('sysHealthBody');
  box.innerHTML = '<div style="font-size:12.5px;color:var(--muted);padding:8px 2px">正在检测各组件状态…</div>';
  try{
    const r = await fetch(`${$('apiBase').value}/api/health`);
    const d = await r.json();
    const lv = d.status === 'ok' ? 'ok' : (d.status === 'down' ? 'no' : 'wait');
    const lvText = { ok:'运行正常', degraded:'降级运行', down:'服务异常' }[d.status] || d.status;
    box.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span class="lv-tag lv-${lv}" style="font-size:12.5px">● ${esc(lvText)}</span>
        <span style="font-size:11.5px;color:var(--muted);margin-left:auto">检测时间 ${new Date().toLocaleTimeString('zh-CN')}</span>
      </div>`
      + (d.components||[]).map(c => {
          const s = c.status === 'ok' ? 'ok' : (c.status === 'down' ? 'no' : 'wait');
          return `<div style="padding:7px 2px;border-bottom:1px dashed var(--border);font-size:12.5px">
            <div style="display:flex;align-items:center;gap:8px">
              <span class="lv-tag lv-${s}" style="min-width:64px;text-align:center">${esc(c.status)}</span>
              <b>${esc(c.name)}</b>
              <span style="margin-left:auto;color:var(--muted)">${c.latency_ms} ms</span>
            </div>
            ${c.detail ? `<div style="font-size:11.5px;color:var(--muted);margin-top:3px;padding-left:72px">${esc(c.detail)}</div>` : ''}
          </div>`;
        }).join('')
      + ((d.notes && d.notes.length) ? `
        <div style="margin-top:10px;padding:8px 10px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px">
          <div style="font-size:12px;font-weight:600;color:#92400e;margin-bottom:4px">降级原因说明</div>
          ${d.notes.map(n=>`<div style="font-size:12px;color:#92400e;line-height:1.6">· ${esc(n)}</div>`).join('')}
        </div>` : '')
      + (d.models ? `
        <div style="margin-top:10px">
          <div style="font-size:12px;font-weight:600;margin-bottom:5px">模型清单</div>
          <div style="font-size:12px;color:var(--muted);line-height:1.8">
            · 大语言模型：${esc(d.models.llm?.model || '-')}（${esc(d.models.llm?.backend || '-')}）· 备用 ${esc(d.models.llm?.fallback || '-')}<br>
            · 向量模型：${esc(d.models.embedding?.model || '-')}（dim=${esc(String(d.models.embedding?.dim || '-'))}）<br>
            · 意图识别：${esc(d.models.intent?.model || '-')}<br>
            · OCR：${esc(d.models.ocr?.engine || '未启用')}
          </div>
        </div>` : '')
      + `<div style="font-size:11.5px;color:var(--muted);margin-top:10px;line-height:1.6">
          状态说明：ok = 全部正常；degraded = 服务可用但存在降级项（原因见上方说明）；down = 核心组件不可用。</div>`;
  }catch(e){
    box.innerHTML = `<div style="font-size:12.5px;color:var(--danger)">后端不可达：${esc(e.message)}。请确认服务已启动（start.bat）。</div>`;
  }
};
$('btnLeave').onclick = openLeave;
$('btnLeaveClose').onclick = () => closeMask('leaveMask');
$('leaveMask').onclick = e => { if(e.target === $('leaveMask')) closeMask('leaveMask'); };
$('leaveTabMy').onclick = () => switchLeaveTab('my');
$('leaveTabPending').onclick = () => switchLeaveTab('pending');
$('btnReimburse').onclick = openRb;
$('btnRbClose').onclick = () => closeMask('rbMask');
$('rbMask').onclick = e => { if(e.target === $('rbMask')) closeMask('rbMask'); };
$('rbTabMy').onclick = () => switchRbTab('my');
$('rbTabPending').onclick = () => switchRbTab('pending');
$('btnAdmin').onclick = openAdmin;
$('btnAdminClose').onclick = closeAdmin;
$('btnDocs').onclick = openDocs;
$('btnDocClose').onclick = () => closeMask('docMask');
$('docMask').onclick = e => { if(e.target === $('docMask')) closeMask('docMask'); };
$('btnHealthClose').onclick = () => closeMask('healthMask');
$('healthMask').onclick = e => { if(e.target === $('healthMask')) closeMask('healthMask'); };
$('btnSysHealthClose').onclick = () => closeMask('sysHealthMask');
$('sysHealthMask').onclick = e => { if(e.target === $('sysHealthMask')) closeMask('sysHealthMask'); };
$('btnHelp').onclick = () => openMask('helpMask');
$('btnHelpClose').onclick = () => closeMask('helpMask');
$('helpMask').onclick = e => { if(e.target === $('helpMask')) closeMask('helpMask'); };
$('btnUpload').onclick = doUpload;
$('btnPwdSubmit').onclick = submitPwd;
$('pwdNew2').onkeydown = e => { if(e.key === 'Enter') submitPwd(); };
document.querySelectorAll('#auditFilter .chip').forEach(c => {
  c.onclick = () => {
    document.querySelectorAll('#auditFilter .chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    __auditType = c.dataset.t || '';
    renderAudit();
  };
});
document.querySelectorAll('#gapFilter .chip').forEach(c => {
  c.onclick = () => {
    document.querySelectorAll('#gapFilter .chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    __gapReason = c.dataset.r || '';
    renderGaps();
  };
});
document.querySelectorAll('#gapStatusFilter .chip').forEach(c => {
  c.onclick = () => {
    document.querySelectorAll('#gapStatusFilter .chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    __gapStatus = c.dataset.s || '';
    renderGaps();
  };
});
/* ---- 管理看板 Tab：点击切换，一次只显示一个模块 ---- */
document.querySelectorAll('#adminTabs .admin-tab').forEach(t => {
  t.onclick = () => switchAdminTab(t.dataset.tab);
});
/* ---- 数据看板：趋势回溯天数 ---- */
document.querySelectorAll('#anDays .chip').forEach(c => {
  c.onclick = () => {
    document.querySelectorAll('#anDays .chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    __anDays = c.dataset.d || 14;
    loadAnalytics();
  };
});
$('btnUmCreate').onclick = createUser;
$('btnBackup').onclick = doBackup;
/* ---- 通知中心 ---- */
$('btnNotify').onclick = e => { e.stopPropagation(); toggleNotifyPanel(); };
$('btnNotifyReadAll').onclick = async e => { e.stopPropagation(); await markNotifyRead([]); };
document.addEventListener('click', e => {
  if(__notifyOpen && !$('notifyWrap').contains(e.target)){
    $('notifyPanel').style.display = 'none'; __notifyOpen = false;
  }
});
/* ---- 权限申请 / 合规审计 弹窗 ---- */
$('btnPerm').onclick = openPerm;
$('btnPermClose').onclick = () => closeMask('permMask');
$('permMask').onclick = e => { if(e.target === $('permMask')) closeMask('permMask'); };
$('btnPermSubmit').onclick = submitPerm;
$('permReason').onkeydown = e => { if(e.key==='Enter') submitPerm(); };
$('permTabMine').onclick    = () => switchPermTab('mine');
$('permTabApprove').onclick = () => switchPermTab('approve');
$('permTabHistory').onclick = () => switchPermTab('history');
$('permTabConfig').onclick  = () => switchPermTab('config');
// 授权有效期 / 类型变化只影响说明文字与保存参数，不必重新拉矩阵
$('permCfgType').onchange = renderPermConfig;
$('permCfgDays').onchange = renderPermConfig;
$('permCfgUser').onchange   = renderPermConfig;
$('btnPermCfgRefresh').onclick = loadGrantsConfig;
$('btnPermCfgSave').onclick = savePermConfig;
$('btnCompliance').onclick = openCompliance;
$('btnComplianceClose').onclick = () => closeMask('complianceMask');
$('complianceMask').onclick = e => { if(e.target === $('complianceMask')) closeMask('complianceMask'); };
$('auditTabLogs').onclick = () => switchAuditTab('logs');
$('auditTabRisk').onclick = () => switchAuditTab('risk');
$('auditTabReport').onclick = () => switchAuditTab('report');
$('btnAuditRefresh').onclick = loadAuditLogs;
$('btnAuditCsv').onclick  = () => exportAudit('csv');
$('btnAuditJson').onclick = () => exportAudit('json');
$('btnAuditPdf').onclick  = () => exportAudit('pdf');
$('btnReportGen').onclick = loadComplianceReport;
$('btnReportCsv').onclick = exportReportCsv;
/* 月度报告的 PDF：直接走服务端报告接口，口径 = 报告里选的月份，与页面所见一致 */
$('btnReportPdf').onclick = () => {
  const m = $('reportMonth').value;
  const url = m ? `/api/compliance/export?format=pdf&limit=5000&since=${m}-01 00:00:00&until=${m}-31 23:59:59`
                : '/api/compliance/export?format=pdf&limit=5000&period=month';
  const a = document.createElement('a');
  a.href = url;
  a.download = `compliance_report_${m || 'current'}.pdf`;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  toast('PDF 合规报告已导出', 'ok');
};
$('reportMonth').onchange = loadComplianceReport;
$('auditAction').onchange = loadAuditLogs;
$('auditRange').onchange  = loadAuditLogs;
$('auditRisk').onchange   = loadAuditLogs;
$('auditUser').onkeydown = e => { if(e.key==='Enter') loadAuditLogs(); };
$('auditDept').onkeydown = e => { if(e.key==='Enter') loadAuditLogs(); };
$('btnRiskRefresh').onclick = loadRiskAlerts;
$('riskWindow').onchange = loadRiskAlerts;
/* 通用确认弹窗已由 Ant Design Modal.confirm 接管（见 uiConfirm），无需本地事件绑定 */
