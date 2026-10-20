/* ==========================================================================
   Margin · 交互层
   纯原生 JS，无构建步骤。

   **数据只有一处来源：后端接口。** 2026-10-19 移除了 mock-data.js 与整套演示
   回放链路 —— 之前用 file:// 打开会看到一份「跑完的审查」，很容易被当成真结果。
   现在后端不可达时，界面会明确说「未连接」并给出启动服务的命令，不再假装有数据。
   ========================================================================== */

(function () {
  'use strict';

  /* 可变的数据源：全部由 live.js 从后端灌入。启动时是空的，
     渲染出来就是「还没有仓库」的空态，而不是一份假清单。 */
  var SPACES = [];
  var MODELS = [];

  /* 由 live.js 注入的钩子；为空即表示后端还没接上 */
  var liveSend = null;
  var liveStop = null;

  /* 当前选中的空间与记录，供实时模式发起审计时取仓库路径 */
  var activeSpace = null;
  var activeSession = null;

  /* ---------- DOM 引用 ---------- */
  var thread = document.getElementById('thread');
  var scroller = document.getElementById('scroller');
  var form = document.getElementById('composer');
  var input = document.getElementById('prompt');
  var sendBtn = document.getElementById('sendBtn');
  var sendLabel = document.getElementById('sendLabel');
  var stopBtn = document.getElementById('stopBtn');
  var dock = document.querySelector('.dock');
  var themeBtn = document.getElementById('themeBtn');
  var themeIcon = document.getElementById('themeIcon');
  var diffBtn = document.getElementById('diffBtn');

  /* 运行条不再是 HTML 里的固定块，而是按需创建的浮层胶囊：
     它只在真的有运行在跑的时候存在，静态 HTML 里留一个空壳只会让
     aria-live 区域在页面加载时先念一句「正在运行」。 */
  var runbar = null;
  var runbarLabel = null;
  var runbarStep = null;
  var runbarStop = null;
  var diffuse = false;

  /* 左侧空间栏 */
  var shell = document.getElementById('shell');
  var rail = document.getElementById('rail');
  var scrim = document.getElementById('scrim');
  var railToggle = document.getElementById('railToggle');
  var spaceList = document.getElementById('spaceList');
  var spaceCount = document.getElementById('spaceCount');
  var newReview = document.getElementById('newReview');
  var railUserNote = document.getElementById('railUserNote');

  /* 顶栏上下文 */
  var wsRepo = document.getElementById('wsRepo');
  var wsPr = document.getElementById('wsPr');
  var wsBranch = document.getElementById('wsBranch');
  var wsFiles = document.getElementById('wsFiles');
  var wsAdd = document.getElementById('wsAdd');
  var wsDel = document.getElementById('wsDel');

  var NARROW = window.matchMedia('(max-width: 900px)');

  var SEV_LABEL = { blocker: '阻断', warn: '警告', note: '建议', pass: '通过' };
  var SEV_ORDER = ['blocker', 'warn', 'note', 'pass'];

  var ICON = {
    check:
      '<svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M2.7 6.3 5 8.6l4.3-5.2" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    chevron:
      '<svg class="tool__chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M3 4.75 6 7.75l3-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    caret:
      '<svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M4.5 3 8 6l-3.5 3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    chevronRight:
      '<svg class="space__chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M4.5 3 8 6l-3.5 3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    folder:
      '<svg class="space__icon" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2 3.5A1.5 1.5 0 0 1 3.5 2h3.2c.4 0 .78.16 1.06.44l1.3 1.3c.28.28.66.44 1.06.44h2.38A1.5 1.5 0 0 1 14 5.68v6.82A1.5 1.5 0 0 1 12.5 14h-9A1.5 1.5 0 0 1 2 12.5z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
    branch:
      '<svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M3 2.5v5.2M9 2.5v2.1c0 1-.8 1.8-1.8 1.8H4.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><circle cx="3" cy="9.4" r="1.4" fill="currentColor"/><circle cx="9" cy="2.4" r="1.4" fill="currentColor"/></svg>',
    plus:
      '<svg width="13" height="13" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M6 2.2v7.6M2.2 6h7.6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
    close:
      '<svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true" focusable="false"><path d="M2 2l6 6M8 2l-6 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  };

  /* ==========================================================================
     工具函数
     ========================================================================== */

  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text != null) {
      node.textContent = text;
    }
    return node;
  }

  /* ---------- 可中断的等待 ---------- */
  var pending = new Map();
  var cancelled = false;

  function sleep(ms) {
    return new Promise(function (resolve) {
      var id = setTimeout(function () {
        pending.delete(id);
        resolve();
      }, ms);
      pending.set(id, resolve);
    });
  }

  function stopAll() {
    /* 实时模式：协作式中断，服务端会在轮次边界停下 */
    if (liveStop) {
      liveStop();
    }
    cancelled = true;
    pending.forEach(function (resolve, id) {
      clearTimeout(id);
      resolve();
    });
    pending.clear();
  }

  /* ---------- 滚动：只在用户没有离开底部时跟随 ---------- */
  var scrollQueued = false;

  function follow(force) {
    if (scrollQueued) {
      return;
    }
    scrollQueued = true;
    window.requestAnimationFrame(function () {
      scrollQueued = false;
      var distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      if (force || distance < 140) {
        scroller.scrollTop = scroller.scrollHeight;
      }
    });
  }

  /* ==========================================================================
     渲染
     ========================================================================== */

  function renderUserTurn(text) {
    var turn = el('article', 'turn turn--user is-animated');
    var bubble = el('div', 'bubble');
    bubble.appendChild(el('p', null, text));
    turn.appendChild(bubble);
    thread.appendChild(turn);
    follow(true);
  }

  function makeStep(step) {
    var li = el('li', 'step is-pending');
    var mark = el('span', 'step__mark');
    mark.setAttribute('aria-hidden', 'true');
    var label = el('span', 'step__label', step.label);
    var meta = el('span', 'step__meta', step.meta || '');
    var dur = el('span', 'step__dur num', '');
    li.appendChild(mark);
    li.appendChild(label);
    li.appendChild(meta);
    li.appendChild(dur);
    return { el: li, mark: mark, dur: dur, meta: meta };
  }

  /* outcome 可选：回放一条历史记录时传本次运行的形态，
     不传就按当前选中的助手。 */
  function buildPlan(steps, outcome) {
    // 计划面板的标题随助手变：审查叫「审查计划」，架构叫「设计计划」。
    // 不跟着变的话，同一个面板会一边说「开始设计」一边说「审查计划」。
    var planTitle = wordsFor(outcome).planTitle || '执行计划';
    var root = el('section', 'plan is-animated');
    root.setAttribute('aria-label', planTitle);

    var head = el('header', 'plan__head');
    head.appendChild(el('h3', 'plan__title', planTitle));
    var total = el('span', 'plan__meta num');
    total.textContent = steps.length + ' 步';
    head.appendChild(total);
    root.appendChild(head);

    var list = el('ol', 'plan__list');
    var items = [];

    steps.forEach(function (step) {
      var item = makeStep(step);
      list.appendChild(item.el);
      items.push(item);
    });

    root.appendChild(list);

    return { root: root, items: items, total: total, list: list };
  }

  function setStepState(item, state, duration) {
    item.el.className = 'step is-' + state;
    if (state === 'done') {
      item.mark.innerHTML = ICON.check;
      if (duration) {
        item.dur.textContent = duration;
      }
    } else if (state === 'active') {
      item.mark.innerHTML = '<span class="step__spinner"></span>';
    } else {
      item.mark.innerHTML = '';
    }
  }

  function buildTool(tool) {
    var details = el('details', 'tool');
    var head = el('summary', 'tool__head');

    var dot = el('span', 'tool__dot');
    dot.setAttribute('aria-hidden', 'true');
    head.appendChild(dot);

    var name = el('code', 'tool__name', tool.name);
    name.setAttribute('translate', 'no');
    head.appendChild(name);

    var arg = el('span', 'tool__arg', tool.arg);
    arg.setAttribute('translate', 'no');
    head.appendChild(arg);

    head.appendChild(el('span', 'tool__meta', tool.meta));
    head.appendChild(el('span', 'tool__dur num', tool.duration));
    head.insertAdjacentHTML('beforeend', ICON.chevron);

    details.appendChild(head);

    var out = el('pre', 'tool__out');
    var code = el('code', null, tool.output);
    code.setAttribute('translate', 'no');
    out.appendChild(code);
    details.appendChild(out);

    return details;
  }

  function diffRow(line) {
    var cls = 'diff__row';
    var sign = '';
    if (line.type === 'add') {
      cls += ' diff__row--add';
      sign = '+';
    } else if (line.type === 'del') {
      cls += ' diff__row--del';
      sign = '-';
    }
    return (
      '<div class="' +
      cls +
      '"><span class="diff__num num">' +
      line.num +
      '</span><span class="diff__sign" aria-hidden="true">' +
      sign +
      '</span><code class="diff__code">' +
      esc(line.text || ' ') +
      '</code></div>'
    );
  }

  function buildFinding(finding) {
    var card = el('article', 'finding');
    card.dataset.sev = finding.severity;

    var lane = el('span', 'finding__lane');
    lane.setAttribute('aria-hidden', 'true');
    card.appendChild(lane);

    var gutter = el('div', 'finding__gutter');
    var lineno = el('span', 'finding__lineno num', String(finding.line || '—'));
    var fid = el('span', 'finding__id', finding.id);
    fid.setAttribute('translate', 'no');
    gutter.appendChild(lineno);
    gutter.appendChild(fid);
    card.appendChild(gutter);

    var body = el('div', 'finding__body');

    var head = el('header', 'finding__head');
    var sev = el('span', 'sev sev--' + finding.severity, SEV_LABEL[finding.severity] || finding.severity);
    head.appendChild(sev);
    head.appendChild(el('h4', 'finding__title', finding.title));
    body.appendChild(head);

    var loc = el('span', 'finding__loc');
    var locCode = el('code', null, finding.file);
    locCode.setAttribute('translate', 'no');
    var locLine = el('span', 'num', ':' + finding.line);
    loc.appendChild(locCode);
    loc.appendChild(locLine);
    body.appendChild(loc);

    body.appendChild(el('p', 'finding__detail', finding.detail));

    var facts = el('dl', 'finding__facts');
    [
      ['复现', finding.evidence],
      finding.fix ? ['建议', finding.fix] : null,
    ].forEach(function (pair) {
      if (!pair) {
        return;
      }
      var row = el('div', 'fact');
      row.appendChild(el('dt', null, pair[0]));
      row.appendChild(el('dd', null, pair[1]));
      facts.appendChild(row);
    });
    body.appendChild(facts);

    if (finding.diff) {
      var wrap = el('details', 'diff-wrap');
      var sum = el('summary');
      sum.insertAdjacentHTML('beforeend', ICON.caret);
      sum.appendChild(el('span', null, '查看建议改动'));
      wrap.appendChild(sum);

      var diff = el('div', 'diff');
      var dhead = el('div', 'diff__head');
      var dfile = el('span', null, finding.diff.file);
      dfile.setAttribute('translate', 'no');
      dhead.appendChild(dfile);
      diff.appendChild(dhead);

      var html = finding.diff.lines.map(diffRow).join('');
      diff.insertAdjacentHTML('beforeend', html);
      wrap.appendChild(diff);
      body.appendChild(wrap);
    }

    card.appendChild(body);
    return card;
  }

  function countSeverities(findings) {
    var counts = { blocker: 0, warn: 0, note: 0, pass: 0 };
    findings.forEach(function (f) {
      counts[f.severity] = (counts[f.severity] || 0) + 1;
    });
    return counts;
  }

  /* 表头（标题 + 筛选 chip）单独成函数：实时模式下发现是逐条推来的，
     计数必须跟着涨，不能只在建面板那一刻算一次。 */
  function buildFindingsHead(findings) {
    var head = el('header', 'findings__head');
    head.dataset.head = 'findings';
    head.appendChild(el('h3', 'findings__title', '发现'));

    var counts = countSeverities(findings);
    var filter = el('div', 'sev-filter');
    filter.setAttribute('role', 'group');
    filter.setAttribute('aria-label', '按严重级别筛选');

    var options = [{ key: 'all', label: '全部', count: findings.length }];
    SEV_ORDER.forEach(function (key) {
      if (counts[key]) {
        options.push({ key: key, label: SEV_LABEL[key], count: counts[key] });
      }
    });

    var active = 'all';
    if (head.parentNode && head.parentNode.dataset) {
      /* 重建时沿用当前筛选 */
    }

    options.forEach(function (option, index) {
      var chip = el('button', 'sev-chip');
      chip.type = 'button';
      chip.dataset.sev = option.key;
      chip.setAttribute('aria-pressed', index === 0 ? 'true' : 'false');
      chip.appendChild(el('span', null, option.label));
      chip.appendChild(el('span', 'num', String(option.count)));
      filter.appendChild(chip);
    });

    filter.addEventListener('click', function (event) {
      var chip = event.target.closest('.sev-chip');
      if (!chip) {
        return;
      }
      applyFilter(head.parentNode, chip.dataset.sev);
    });

    head.appendChild(filter);
    return head;
  }

  /* 发现数变化时重建表头，并保留用户已经选中的筛选级别 */
  function refreshFindingsHead(root, findings) {
    var previous = root.querySelector('[data-head="findings"] .sev-chip[aria-pressed="true"]');
    var keep = previous ? previous.dataset.sev : 'all';
    var old = root.querySelector('[data-head="findings"]');
    var fresh = buildFindingsHead(findings);
    if (old) {
      root.replaceChild(fresh, old);
    } else {
      root.insertBefore(fresh, root.firstChild);
    }
    if (keep && keep !== 'all') {
      applyFilter(root, keep);
    }
  }

  function buildFindings(findings) {
    var root = el('section', 'findings is-animated');
    root.setAttribute('aria-label', '审查发现');
    root.appendChild(buildFindingsHead(findings));

    var list = el('div', 'findings__list');
    root.appendChild(list);

    return { root: root, list: list };
  }

  function applyFilter(root, severity) {
    root.querySelectorAll('.sev-chip').forEach(function (chip) {
      chip.setAttribute('aria-pressed', String(chip.dataset.sev === severity));
    });
    root.querySelectorAll('.finding').forEach(function (card) {
      card.hidden = severity !== 'all' && card.dataset.sev !== severity;
    });
    follow();
  }

  /* ==========================================================================
     运行一个 agent 回合
     ========================================================================== */

  var busy = false;
  var sentOnce = false;

  /* 输入坞的措辞随助手变：审查说「开始审查」，架构说「开始设计」。
     取不到清单时用审查的话术，不要留空。 */
  function words() {
    if (window.MARGIN_AGENTS && window.MARGIN_AGENTS.dialect) {
      return window.MARGIN_AGENTS.dialect();
    }
    return { send: '开始审查', follow: '继续追问', running: '正在运行…' };
  }

  /* 按产出形态取方言（不是按当前选中的助手）。
     回放一条历史记录时要按「这条记录的形态」取词，否则切到审查去看
     一条旧的架构记录，面板标题会自相矛盾。 */
  function wordsFor(outcome) {
    if (outcome && window.MARGIN_AGENTS && window.MARGIN_AGENTS.dialectFor) {
      return window.MARGIN_AGENTS.dialectFor(outcome);
    }
    return words();
  }

  function setBusy(state) {
    busy = state;
    /* 运行中把发送按钮**收起来**，而不是摆一个禁用的「运行中」：
       ① 运行中本来就发不出去，一个点不动的按钮不提供任何信息；
       ② 窄屏（320~430px）上「运行中」+「停止」两个按钮一起挤，
          submit 区会顶出输入坞右边 —— 实测溢出 74~185px，
          在 320px 下发送按钮完全在屏幕外。 */
    sendBtn.disabled = state;
    sendBtn.hidden = state;
    stopBtn.hidden = !state;

    if (state) {
      var bar = ensureRunbar();
      bar.hidden = false;
      runbarLabel.textContent = words().running || '正在运行…';
      runbarStep.textContent = '';
    } else if (runbar) {
      /* 收起来而不是删掉：删了会丢焦点（停止按钮刚被点过），
         下一次运行还要重建一遍节点。 */
      runbar.hidden = true;
      runbarStep.textContent = '';
      sendLabel.textContent = sentOnce ? words().follow : words().send;
    } else {
      sendLabel.textContent = sentOnce ? words().follow : words().send;
    }
  }

  /* 方言刷新：切换助手后按钮文案、输入提示、页签标题都要跟着变。
     之前这段逻辑散在启动流程里，切助手时漏刷新「开始审查」（有测试守着），
     现在收拢成一个函数，任何地方改助手都调它。 */
  function applyDialect() {
    var d = words();
    sendLabel.textContent = sentOnce ? d.follow || d.send : d.send;
    if (d.placeholder) {
      input.placeholder = d.placeholder;
    }
    var hint = document.getElementById('composerHintText');
    if (hint && d.hint) {
      hint.textContent = d.hint;
    }
    var label = document.querySelector('label[for="prompt"]');
    if (label && d.inputLabel) {
      label.textContent = d.inputLabel;
    }
    if (stateDirty()) {
      /* 有正在跑的运行：只刷新文案，不动运行条 */
      return;
    }
    if (d.noRepo) {
      offlineReason = d.noRepo;
    }
  }

  /* 当前是否已经有内容（决定 applyDialect 要不要覆盖正文） */
  function stateDirty() {
    return thread.childElementCount > 0;
  }

  var offlineReason = '';

  function finishTurn(turn) {
    if (cancelled) {
      turn.appendChild(
        el('p', 'run__note', '已停止。上面是停止前已完成的部分，可以继续追问。')
      );
    }
    setBusy(false);
    follow();
  }
  function removeFollowUps() {
    var node = thread.querySelector('.followups');
    if (node) {
      node.remove();
    }
  }

  /* ==========================================================================
     发送
     ========================================================================== */

  async function send(text) {
    /* 这句话就是一次真实运行的任务目标。没有 send 钩子说明后端没接上 ——
       这时不能装作发出去了，要如实告诉用户去启动服务。 */
    if (liveSend) {
      await liveSend(text);
      return;
    }
    renderOffline({
      reason: '后端还没有连上，这句话没有发出去。',
      retry: true,
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text || busy) {
      return;
    }
    input.value = '';
    autoGrow();
    send(text);
  });

  stopBtn.addEventListener('click', stopAll);
  /* 运行条里的停止按钮由 ensureRunbar() 创建时自己绑，这里不再绑 ——
     它在首次创建前是 null，直接绑会抛。 */

  /* ---------- 输入框：自动增高 + Enter 发送 ---------- */
  var MAX_HEIGHT = 168;

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, MAX_HEIGHT) + 'px';
  }

  input.addEventListener('input', autoGrow);

  input.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) {
      return;
    }
    event.preventDefault();
    form.requestSubmit();
  });

  /* ==========================================================================
     左侧空间栏：空间 → 审查记录
     ========================================================================== */

  /* 空仓库列表的提示：按当前助手的产出形态给对应的目录。
     审查读 examples/，架构读 arch-cases/ —— 后者需要先跑 fetch-cases.sh。 */
  function emptyHint() {
    var outcome =
      window.MARGIN_AGENTS && window.MARGIN_AGENTS.outcome
        ? window.MARGIN_AGENTS.outcome()
        : 'findings';
    if (outcome === 'blueprint') {
      return '架构分析的案例在上游仓库里，先运行 backend/arch-cases/fetch-cases.sh 拉取，或从「添加仓库」挂一个本地目录。';
    }
    return '把仓库放到 backend/examples/ 下，或用输入坞左侧的回形针「添加仓库」。';
  }

  function renderRail() {
    var spaces = SPACES;
    spaceCount.textContent = String(spaces.length);
    spaceList.textContent = '';

    if (!spaces.length) {
      var empty = el('li', 'space-empty');
      empty.appendChild(el('p', null, '还没有可用的仓库。'));
      /* 空态提示必须按助手给 —— 审查看 examples/，架构看 arch-cases/。
         写死 examples 会让架构助手的使用者去翻一个与它无关的目录。 */
      empty.appendChild(el('p', 'space-empty__hint', emptyHint()));
      spaceList.appendChild(empty);
      updateRailNote();
      return;
    }

    spaces.forEach(function (space) {
      var li = el('li', 'space');
      li.dataset.open = String(!!space.open);
      li.dataset.space = space.id;

      /* 行是容器而不是按钮：主按钮负责「选中 / 展开」，右侧加号负责
         「在这个仓库下开一次新审查」。HTML 不允许按钮嵌套，所以拆成两个。 */
      var row = el('div', 'space__row');
      row.dataset.space = space.id;

      var main = el('button', 'space__main');
      main.type = 'button';
      main.setAttribute('aria-expanded', String(!!space.open));
      main.setAttribute('aria-controls', 'space-' + space.id);
      main.insertAdjacentHTML('beforeend', ICON.chevronRight);
      main.insertAdjacentHTML('beforeend', ICON.folder);

      var name = el('span', 'space__name', space.name);
      name.setAttribute('translate', 'no');
      main.appendChild(name);
      main.appendChild(el('span', 'space__count num', String(space.sessions.length)));

      main.addEventListener('click', function () {
        selectSpace(space);
      });

      var add = el('button', 'space__add');
      add.type = 'button';
      add.setAttribute('aria-label', '在 ' + space.name + ' 新建审查');
      add.title = '在 ' + space.name + ' 新建审查';
      add.insertAdjacentHTML('beforeend', ICON.plus);
      add.addEventListener('click', function (event) {
        event.stopPropagation();
        startDraft(space);
      });

      row.appendChild(main);
      row.appendChild(add);
      li.appendChild(row);

      var ul = el('ul', 'sessions');
      ul.id = 'space-' + space.id;
      ul.hidden = !space.open;

      space.sessions.forEach(function (session) {
        var item = el('li');
        var btn = el('button', 'session');
        btn.type = 'button';
        btn.dataset.session = session.id;
        if (session.current) {
          btn.setAttribute('aria-current', 'true');
        }

        var text = el('span', 'session__text');
        text.appendChild(el('span', 'session__title', session.title));
        text.appendChild(el('span', 'session__time num', session.time));
        btn.appendChild(text);

        btn.addEventListener('click', function () {
          selectSession(space, session);
        });

        item.appendChild(btn);
        ul.appendChild(item);
      });

      li.appendChild(ul);

      if (holdsCurrent(space)) {
        row.classList.add('is-current');
      }

      spaceList.appendChild(li);
    });

    updateRailNote();
  }

  function holdsCurrent(space) {
    return space.sessions.some(function (session) {
      return session.current;
    });
  }

  function updateRailNote() {
    var spaces = SPACES;
    var total = spaces.reduce(function (sum, space) {
      return sum + space.sessions.length;
    }, 0);
    var note = spaces.length + ' 个空间 · ' + total + ' 次运行';
    if (liveSend) {
      note += ' · 已连接后端';
    }
    railUserNote.textContent = note;
  }

  function selectSession(space, session) {
    draftSpace = null;
    applyContext(space, session);

    if (onSessionSelect) {
      // 用户点击才通知外部去回放；程序化选中走 applyContext
      onSessionSelect(space, session);
    }
    closeDrawer();
  }

  function replaceThread(node) {
    /* 先释放旧视图再清空：蓝图视图的 ResizeObserver 强引用已脱离文档的 canvas，
       不清就会连同整棵树一起泄漏（实时模式每回放一条架构会话漏一份）。 */
    if (window.MARGIN_BLUEPRINT && window.MARGIN_BLUEPRINT.destroyAll) {
      window.MARGIN_BLUEPRINT.destroyAll();
    }
    thread.textContent = '';
    if (node) {
      thread.appendChild(node);
    }
  }

  /* 点击空间行：切换到这个仓库并展开；再点一次已选中的行则收起。
     之前这里只做展开/收起，于是「点了别的空间但顶栏没反应」——
     因为顶栏只由 session 点击驱动，而大部分空间下一条记录都没有。 */
  function selectSpace(space) {
    var isCurrent = activeSpace && activeSpace.id === space.id;

    if (isCurrent) {
      space.open = !space.open;
      renderRail();
      applyContext(activeSpace, activeSession);
      return;
    }

    space.open = true;
    draftSpace = null;
    var latest = space.sessions[0] || null;
    renderRail();
    applyContext(space, latest);

    if (latest && onSessionSelect) {
      onSessionSelect(space, latest);
    } else if (!latest && onSpaceSelect) {
      onSpaceSelect(space);
    }
    closeDrawer();
  }

  /* 在指定仓库下新建一次审查：把它设为当前目标并把焦点交给输入框。
     这是「没法在单个仓库下添加新任务」的直接解法。 */
  function startDraft(space) {
    if (!space) {
      return;
    }
    space.open = true;
    draftSpace = space;
    renderRail();
    applyContext(space, null);
    if (onDraft) {
      onDraft(space);
    }
    try {
      input.focus();
    } catch (error) {
      /* 有些环境不允许程序化聚焦，忽略 */
    }
    closeDrawer();
  }

  var onSessionSelect = null;
  var onSpaceSelect = null;
  var onDraft = null;
  var draftSpace = null;

  /* 顶栏上下文：字段是**按可用性**渲染的。
     本地仓库没有 owner / PR / 分支 / 增删统计，这些位置必须消失而不是
     显示 "undefined" —— 宁可少显示，也不要显示假数据。
     注意：这里也是顶栏唯一的写入点，HTML 里刻意不留静态默认值，
     否则一旦取数失败，界面会显示一个看起来很真实的假仓库。 */
  function show(id, value, formatter) {
    var node = document.getElementById(id);
    if (!node) {
      return;
    }
    var missing = value === null || value === undefined || value === '';
    node.hidden = missing;
    if (!missing) {
      node.textContent = formatter ? formatter(value) : String(value);
    }
  }

  function updateContext(space, session) {
    var label = space ? (space.owner ? space.owner + '/' + space.name : space.name) : '';
    show('wsRepo', label);
    show('wsPr', session && session.pr ? '#' + session.pr : '');
    show('wsFiles', (session && session.files) || (space && space.files) || '', function (v) {
      return v + ' 个文件';
    });
    show('wsAdd', (session && session.add) || '', function (v) {
      return '+' + v;
    });
    show('wsDel', (session && session.del) || '', function (v) {
      return '\u2212' + v;
    });

    var branch = document.getElementById('wsBranch');
    if (branch) {
      if (session && session.branch) {
        branch.hidden = false;
        branch.textContent = '';
        branch.insertAdjacentHTML('beforeend', ICON.branch);
        branch.appendChild(document.createTextNode(session.branch));
      } else {
        branch.hidden = true;
      }
    }

    activeSpace = space || null;
    activeSession = session || null;

    // 审查范围是「相对某个仓库」的路径，换了仓库就必须清空 ——
    // 否则会把上一个仓库的 app/x.py 提交给这一仓库，后端只能整批忽略。
    var spacePath = activeSpace ? activeSpace.path || activeSpace.id : '';
    if (spacePath !== scopeRepo) {
      scopeRepo = spacePath;
      scope = [];
      renderScopeChips();
    }

    renderComposerTarget();
  }

  /* 只更新展示层（顶栏 + 侧栏高亮），不触发 onSessionSelect ——
     供实时模式在「程序化选中」时使用，避免和用户的点击路径互相递归。 */
  function applyContext(space, session) {
    (SPACES || []).forEach(function (item) {
      item.sessions.forEach(function (entry) {
        entry.current = false;
      });
    });
    if (session) {
      session.current = true;
    }

    spaceList.querySelectorAll('.session').forEach(function (node) {
      if (session && node.dataset.session === session.id) {
        node.setAttribute('aria-current', 'true');
      } else {
        node.removeAttribute('aria-current');
      }
    });

    spaceList.querySelectorAll('.space__row').forEach(function (node) {
      node.classList.remove('is-current');
    });
    if (space) {
      var spaceNode = spaceList.querySelector('[data-space="' + space.id + '"] .space__row');
      if (spaceNode) {
        spaceNode.classList.add('is-current');
      }
    }

    updateContext(space, session);
  }

  /* ==========================================================================
     输入坞：目标仓库提示 / 审查范围 / 两个浮层

     这三样对应三条真实反馈：
     ① 不知道新审查会落到哪个仓库；
     ② 没法在单个仓库下开新审查（配合侧栏的加号）；
     ③ 回形针与筛选按钮此前是纯装饰，点了没反应。
     ========================================================================== */

  var composerTarget = document.getElementById('composerTarget');
  var scopeChips = document.getElementById('scopeChips');
  var attachBtn = document.getElementById('attachBtn');
  var attachPop = document.getElementById('attachPop');
  var scopeBtn = document.getElementById('scopeBtn');
  var scopePop = document.getElementById('scopePop');

  /* 本次审查限定的路径（仓库内相对路径）。换仓库时清空。 */
  var scope = [];
  var scopeRepo = '';
  /* 由 live.js 注入：仓库候选、加入/移出、文件清单。未连接时为空。 */
  var workspaceProvider = null;

  function renderComposerTarget() {
    if (!composerTarget) {
      return;
    }
    if (!activeSpace) {
      composerTarget.hidden = true;
      return;
    }

    composerTarget.hidden = false;
    composerTarget.classList.toggle('is-draft', !!draftSpace);
    composerTarget.textContent = '';

    // 「新建审查 → X」/「新建设计 → X」：措辞随助手，免得用户不知道这一发会落到哪
    composerTarget.appendChild(
      document.createTextNode(draftSpace ? words().draftPrefix : '当前目标 → ')
    );
    var strong = el('b', null, activeSpace.name);
    strong.setAttribute('translate', 'no');
    composerTarget.appendChild(strong);

    var bits = [];
    if (draftSpace) {
      bits.push('尚未开始');
    } else if (activeSession) {
      bits.push('已有记录');
    }
    if (scope.length) {
      bits.push('限定 ' + scope.length + ' 项');
    }
    if (bits.length) {
      composerTarget.appendChild(document.createTextNode('（' + bits.join(' · ') + '）'));
    }

    var title = '本次任务提交到：' + (activeSpace.path || activeSpace.name);
    if (scope.length) {
      title += '\n限定范围：\n' + scope.join('\n');
    }
    composerTarget.title = title;
  }

  function renderScopeChips() {
    if (!scopeChips) {
      return;
    }
    scopeChips.textContent = '';
    scopeChips.hidden = !scope.length;

    scope.forEach(function (path) {
      var chip = el('span', 'chip');
      chip.appendChild(el('span', 'chip__text', path));

      var remove = el('button', 'chip__x');
      remove.type = 'button';
      remove.setAttribute('aria-label', '从审查范围移除 ' + path);
      remove.insertAdjacentHTML('beforeend', ICON.close);
      remove.addEventListener('click', function () {
        scope = scope.filter(function (item) {
          return item !== path;
        });
        renderScopeChips();
        renderComposerTarget();
        if (scopePop && !scopePop.hidden) {
          renderScopePop();
        }
      });

      chip.appendChild(remove);
      scopeChips.appendChild(chip);
    });
  }

  /* ---------- 浮层的开合与通用零件 ---------- */

  function closePop(pop, btn) {
    if (!pop) {
      return;
    }
    pop.hidden = true;
    if (btn) {
      btn.setAttribute('aria-expanded', 'false');
    }
  }

  function openPop(pop, btn) {
    if (!pop) {
      return;
    }
    // 两个浮层互斥：否则会叠在一起，看起来像渲染错乱
    if (pop === attachPop) {
      closePop(scopePop, scopeBtn);
    } else {
      closePop(attachPop, attachBtn);
    }
    pop.hidden = false;
    if (btn) {
      btn.setAttribute('aria-expanded', 'true');
    }
  }

  function popHead(title, hint) {
    var head = el('div', 'pop__head');
    head.appendChild(el('div', 'pop__title', title));
    if (hint) {
      head.appendChild(el('div', 'pop__hint', hint));
    }
    return head;
  }

  function popEmpty(text) {
    return el('div', 'pop__empty', text);
  }

  /* ---------- 回形针：浏览电脑目录并加入工作区 ---------- */

  /* 浏览器的状态。路径必须是**服务端给的绝对路径** —— 浏览器读不到本机
     文件系统，只能由同源的 harness 代列目录（它只列，不返回任何文件内容）。 */
  var attachState = {
    path: '',
    data: null,
    /* 快捷位置只在「起点」（空路径）时由后端返回，进目录后就没了 ——
       但用户随时想跳去另一个盘，所以拿到一次就记住，之后一直显示。 */
    shortcuts: [],
    loading: false,
    error: '',
    repos: [],
  };

  var ICON_DIR =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
    '<path d="M1.6 4.2c0-.6.4-1 1-1h3l1.2 1.4h6.6c.6 0 1 .4 1 1v6.2c0 .6-.4 1-1 1H2.6c-.6 0-1-.4-1-1V4.2z" ' +
    'fill="none" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round"/></svg>';
  var ICON_FILE =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
    '<path d="M4 1.8h5L12.2 5v9.2H4z" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round"/>' +
    '<path d="M9 1.8V5h3.2" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round"/></svg>';

  function attachBrowse(path) {
    if (!workspaceProvider) {
      renderAttachPop();
      return;
    }
    attachState.loading = true;
    attachState.error = '';
    renderAttachPop();
    workspaceProvider.browse(path).then(function (data) {
      attachState.loading = false;
      if (data && data.error) {
        attachState.error = data.error;
        renderAttachPop();
        return;
      }
      attachState.data = data || {};
      attachState.path = (data && data.path) || '';
      if (data && data.shortcuts && data.shortcuts.length) {
        attachState.shortcuts = data.shortcuts;
      }
      renderAttachPop();
    }).catch(function () {
      attachState.loading = false;
      attachState.error = '读取目录失败：后端没有响应。';
      renderAttachPop();
    });
  }

  function renderAttachPop() {
    if (!attachPop) {
      return;
    }
    attachPop.textContent = '';
    attachPop.appendChild(
      popHead(
        '添加到工作区',
        '浏览电脑上的目录，选中后加入左侧工作区。加入的目录会作为一个可审查的仓库。'
      )
    );

    if (!workspaceProvider) {
      /* 这个提示要能把人送到能用的状态上，所以写成两步可照做的动作。
         「没有后端」有两种成因：双击打开了 index.html（file://），
         或者服务确实没在跑 —— 对使用者来说处理方式是一样的。 */
      var guide = el('div', 'pop__empty');
      guide.appendChild(
        document.createTextNode('文件浏览器需要后端。当前没有连上服务，所以列不出目录。')
      );
      var steps = el('ol', 'pop__steps');
      steps.appendChild(el('li', null, '启动服务：双击项目根目录下的 start-margin.bat'));
      steps.appendChild(
        el('li', null, '用浏览器打开 http://127.0.0.1:8000/（不要双击 index.html）')
      );
      guide.appendChild(steps);
      attachPop.appendChild(guide);
      return;
    }

    var data = attachState.data || {};

    /* 快捷位置：常用目录与盘符，任何时候都在，方便来回切换 */
    var spots = data.shortcuts || attachState.shortcuts || [];
    if (spots.length) {
      var chips = el('div', 'pop__chips');
      spots.forEach(function (spot) {
        var chip = el('button', 'pop__chip', spot.name);
        chip.type = 'button';
        chip.setAttribute('translate', 'no');
        chip.setAttribute('title', spot.path);
        chip.addEventListener('click', function () {
          attachBrowse(spot.path);
        });
        chips.appendChild(chip);
      });
      attachPop.appendChild(chips);
    }

    /* 导航：上一级 + 当前位置 */
    if (attachState.path) {
      var nav = el('div', 'pop__nav');
      if (data.parent) {
        var up = el('button', 'pop__up');
        up.type = 'button';
        up.setAttribute('aria-label', '上一级');
        up.appendChild(el('span', 'pop__up-arrow', '↑'));
        up.appendChild(el('span', null, '上一级'));
        up.addEventListener('click', function () {
          attachBrowse(data.parent);
        });
        nav.appendChild(up);
      }
      var crumb = el('span', 'pop__crumb', attachState.path);
      crumb.setAttribute('translate', 'no');
      crumb.setAttribute('title', attachState.path);
      nav.appendChild(crumb);
      attachPop.appendChild(nav);
    }

    if (attachState.loading) {
      attachPop.appendChild(popEmpty('正在读取…'));
      return;
    }

    /* 把「当前所在目录」加入工作区 */
    if (attachState.path && data.can_add) {
      var bar = el('div', 'pop__row');
      var addHere = el(
        'button',
        'pop__btn pop__btn--wide',
        data.in_workspace ? '当前目录已在工作区' : '把当前目录加入工作区'
      );
      addHere.type = 'button';
      if (data.in_workspace) {
        addHere.disabled = true;
      } else {
        addHere.addEventListener('click', function () {
          addWorkspacePath(attachState.path);
        });
      }
      bar.appendChild(addHere);
      attachPop.appendChild(bar);
    }

    /* 条目：目录可进入或直接就加入；文件只列出（工作区加的是目录） */
    var entries = data.entries || [];
    var list = el('div', 'pop__list');
    if (!entries.length && attachState.path) {
      list.appendChild(popEmpty('这个目录下没有可选的子目录。'));
    }
    entries.forEach(function (item) {
      var row = el('div', 'pop__entry' + (item.is_dir ? '' : ' is-file'));

      var main = el('button', 'pop__entry-main');
      main.type = 'button';
      var icon = el('span', 'pop__entry-icon');
      icon.innerHTML = item.is_dir ? ICON_DIR : ICON_FILE;
      main.appendChild(icon);
      var name = el('span', 'pop__name', item.name);
      name.setAttribute('translate', 'no');
      main.appendChild(name);
      if (item.is_dir) {
        main.setAttribute('title', item.path);
        main.addEventListener('click', function () {
          attachBrowse(item.path);
        });
      } else {
        main.disabled = true; // 目录才是可加入的单位，文件只作为上下文出现
      }
      row.appendChild(main);

      if (item.is_dir) {
        if (item.in_workspace) {
          row.appendChild(el('span', 'pop__meta', '已在工作区'));
        } else {
          var quick = el('button', 'pop__quick', '加入');
          quick.type = 'button';
          quick.setAttribute('aria-label', '把 ' + item.name + ' 加入工作区');
          quick.addEventListener('click', function (event) {
            event.stopPropagation();
            addWorkspacePath(item.path);
          });
          row.appendChild(quick);
        }
      }
      list.appendChild(row);
    });
    attachPop.appendChild(list);

    /* 手动输入：浏览不到的路径（网络盘、很深的目录）仍可直接粘贴。
       这里是 div 不是 form —— 它嵌在输入坞的 <form> 里，嵌套表单会在回车时
       连带提交一次审查。 */
    var row2 = el('div', 'pop__row');
    var text = el('input', 'pop__input');
    text.type = 'text';
    text.placeholder = '或粘贴路径，例如 D:/code/app';
    text.setAttribute('aria-label', '仓库路径');
    text.spellcheck = false;
    var submit = el('button', 'pop__btn', '加入');
    submit.type = 'button';
    row2.appendChild(text);
    row2.appendChild(submit);

    function commit() {
      addWorkspacePath(text.value);
    }
    submit.addEventListener('click', commit);
    text.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        commit();
      }
    });
    attachPop.appendChild(row2);

    if (attachState.error) {
      attachPop.appendChild(el('div', 'pop__error', attachState.error));
    }

    var removable = (attachState.repos || []).filter(function (item) {
      return item.removable;
    });
    if (removable.length) {
      var actions = el('div', 'pop__actions');
      removable.forEach(function (item) {
        var btn = el('button', 'pop__ghost', '移除 ' + item.name);
        btn.type = 'button';
        btn.addEventListener('click', function () {
          removeWorkspacePath(item.path);
        });
        actions.appendChild(btn);
      });
      attachPop.appendChild(actions);
    }
  }

  /* 「移除」区列的是已加入的工作区 —— 与浏览目录无关，单独拉一次，
     加入/移除之后再拉，保证按钮列表跟得上。 */
  function attachRefreshRepos() {
    if (!workspaceProvider) {
      return;
    }
    workspaceProvider.list().then(function (data) {
      attachState.repos = (data && data.repos) || [];
      renderAttachPop();
    });
  }

  function loadAttachPop() {
    attachState.data = null;
    attachState.error = '';
    attachRefreshRepos();
    attachBrowse('');
  }

  function addWorkspacePath(path) {
    if (!workspaceProvider || !path) {
      return;
    }
    attachState.loading = true;
    renderAttachPop();
    workspaceProvider.add(path).then(function (result) {
      if (result && result.error) {
        attachState.loading = false;
        attachState.error = result.error;
        renderAttachPop();
        return;
      }
      attachState.loading = false;
      attachRefreshRepos();
      attachBrowse(attachState.path); // 刷新标记（停在当前目录，不跳回起点）
      // 侧栏要立刻出现新空间，所以让 live.js 重新拉一次仓库列表，
      // 并把「刚加入的仓库」告诉它，好让 refresh 完自动选中
      if (onWorkspaceChange) {
        onWorkspaceChange((result && result.repo && result.repo.path) || '');
      }
    });
  }

  function removeWorkspacePath(path) {
    if (!workspaceProvider) {
      return;
    }
    attachState.loading = true;
    renderAttachPop();
    workspaceProvider.remove(path).then(function (result) {
      if (result && result.error) {
        attachState.loading = false;
        attachState.error = result.error;
        renderAttachPop();
        return;
      }
      attachState.loading = false;
      attachRefreshRepos();
      attachBrowse(attachState.path);
      if (onWorkspaceChange) {
        onWorkspaceChange('');
      }
    });
  }

  var onWorkspaceChange = null;

  /* ---------- 筛选图标：限定本次审查的文件范围 ---------- */

  function renderScopePop(state) {
    if (!scopePop) {
      return;
    }
    scopePop.textContent = '';
    scopePop.appendChild(
      popHead(
        '本次审查的范围',
        '勾选后，本次审查只覆盖这些路径；不选则审整个仓库。'
      )
    );

    if (!activeSpace) {
      scopePop.appendChild(popEmpty('请先在左侧选择一个仓库。'));
      return;
    }
    if (!workspaceProvider) {
      scopePop.appendChild(
        popEmpty('后端未连接，列不出文件。启动服务后重试。')
      );
      return;
    }
    if (state.loading) {
      scopePop.appendChild(popEmpty('正在读取文件清单…'));
      return;
    }

    var files = state.files || [];
    if (!files.length) {
      scopePop.appendChild(
        popEmpty(state.error || '这个仓库下没有列出任何文件。')
      );
      return;
    }

    var list = el('div', 'pop__list');
    files.forEach(function (path) {
      var picked = scope.indexOf(path) !== -1;
      var row = el('button', 'pop__item');
      row.type = 'button';
      row.setAttribute('aria-pressed', String(picked));

      var name = el('span', 'pop__name', path);
      name.setAttribute('translate', 'no');
      row.appendChild(name);
      row.appendChild(el('span', 'pop__meta', picked ? '已选' : ''));

      row.addEventListener('click', function () {
        scope = picked
          ? scope.filter(function (item) {
              return item !== path;
            })
          : scope.concat([path]);
        renderScopePop({ files: files });
        renderScopeChips();
        renderComposerTarget();
      });

      list.appendChild(row);
    });
    scopePop.appendChild(list);

    var actions = el('div', 'pop__actions');
    var all = el('button', 'pop__ghost', '全选');
    all.type = 'button';
    all.addEventListener('click', function () {
      scope = files.slice();
      renderScopePop({ files: files });
      renderScopeChips();
      renderComposerTarget();
    });
    var none = el('button', 'pop__ghost', '清空（审整个仓库）');
    none.type = 'button';
    none.addEventListener('click', function () {
      scope = [];
      renderScopePop({ files: files });
      renderScopeChips();
      renderComposerTarget();
    });
    actions.appendChild(all);
    actions.appendChild(none);
    scopePop.appendChild(actions);
  }

  function loadScopePop() {
    if (!activeSpace || !workspaceProvider) {
      renderScopePop({});
      return;
    }
    renderScopePop({ loading: true });
    workspaceProvider.files(activeSpace.path || activeSpace.name).then(function (data) {
      renderScopePop({
        files: (data && data.files) || [],
        error: data && data.error,
      });
    });
  }

  if (attachBtn) {
    attachBtn.addEventListener('click', function () {
      if (attachPop.hidden) {
        openPop(attachPop, attachBtn);
        loadAttachPop();
      } else {
        closePop(attachPop, attachBtn);
      }
    });
  }

  if (scopeBtn) {
    scopeBtn.addEventListener('click', function () {
      if (scopePop.hidden) {
        openPop(scopePop, scopeBtn);
        loadScopePop();
      } else {
        closePop(scopePop, scopeBtn);
      }
    });
  }

  /* 浮层容器上截断冒泡。
     原因是实测出来的：点浮层里的选项 → 处理函数里重绘整个列表 →
     event.target 变成脱离文档的旧节点 → 冒泡到 document 时
     `pop.contains(event.target)` 判定为 false → 浮层被当成「点了外面」误关。
     表现就是「点一次选项浮层就没了，没法连续勾选」。 */
  [attachPop, scopePop].forEach(function (pop) {
    if (!pop) {
      return;
    }
    pop.addEventListener('click', function (event) {
      event.stopPropagation();
    });
  });

  document.addEventListener('click', function (event) {
    if (attachPop && !attachPop.hidden && !attachPop.contains(event.target) && event.target !== attachBtn) {
      closePop(attachPop, attachBtn);
    }
    if (scopePop && !scopePop.hidden && !scopePop.contains(event.target) && event.target !== scopeBtn) {
      closePop(scopePop, scopeBtn);
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') {
      return;
    }
    if (attachPop && !attachPop.hidden) {
      closePop(attachPop, attachBtn);
      attachBtn.focus();
    }
    if (scopePop && !scopePop.hidden) {
      closePop(scopePop, scopeBtn);
      scopeBtn.focus();
    }
  });

  /* ---------- 抽屉（窄屏）/ 折叠（宽屏） ---------- */

  function openDrawer() {
    rail.classList.add('is-open');
    scrim.hidden = false;
    railToggle.setAttribute('aria-expanded', 'true');
  }

  function closeDrawer() {
    if (!rail.classList.contains('is-open')) {
      return;
    }
    rail.classList.remove('is-open');
    scrim.hidden = true;
    if (NARROW.matches) {
      railToggle.setAttribute('aria-expanded', 'false');
    }
  }

  railToggle.addEventListener('click', function () {
    if (NARROW.matches) {
      if (rail.classList.contains('is-open')) {
        closeDrawer();
      } else {
        openDrawer();
      }
      return;
    }
    var collapsed = shell.classList.toggle('is-collapsed');
    railToggle.setAttribute('aria-expanded', String(!collapsed));
    railToggle.setAttribute('aria-label', collapsed ? '展开空间栏' : '收起空间栏');
  });

  scrim.addEventListener('click', closeDrawer);

  NARROW.addEventListener('change', closeDrawer);

  newReview.addEventListener('click', function () {
    /* 之前这里只做 input.focus()，于是「新建审查」和「切换仓库」是脱钩的，
       用户不知道这次审查会落到哪个仓库。现在明确绑到一个仓库：
       优先当前选中的，没有就取第一个。 */
    var target = activeSpace || SPACES[0] || null;
    if (target) {
      startDraft(target);
      return;
    }
    input.focus();
    closeDrawer();
  });

  /* 退出登录：让服务端吊销会话（清 Cookie），再回登录页。
     只清本地标记是不够的 —— 会话在服务端，不吊销它下次打开仍然是登录态。 */
  var signOut = document.getElementById('signOut');
  if (signOut) {
    signOut.addEventListener('click', function () {
      try {
        window.sessionStorage.removeItem('margin.user');
      } catch (error) {
        /* 隐私模式下不可用，忽略 */
      }
      var api = window.MARGIN_API;
      if (api && api.served) {
        api.logout().catch(function () {
          /* 网络失败也要放人走：本地已经当自己退出了 */
        }).then(function () {
          window.location.href = 'login.html';
        });
        return;
      }
      window.location.href = 'login.html';
    });
  }

  /* ==========================================================================
     账号态：侧栏底部显示当前登录的用户
     未登录不编一个假名字出来，显示「登录」入口。
     ========================================================================== */

  function runAuth() {
    var avatar = document.getElementById('railAvatar');
    var nameNode = document.getElementById('railUserName');
    var out = document.getElementById('signOut');
    var api = window.MARGIN_API;

    function showGuest() {
      if (avatar) {
        avatar.textContent = '?';
      }
      if (nameNode) {
        nameNode.textContent = '未登录';
        nameNode.setAttribute('href', 'login.html');
        nameNode.removeAttribute('title');
      }
      if (out) {
        out.hidden = true;
      }
    }

    if (!api || !api.served) {
      // 没有后端也就没有账号，如实说明
      showGuest();
      if (nameNode) {
        nameNode.textContent = '未连接';
        nameNode.removeAttribute('href');
      }
      return;
    }

    api.me().then(function (payload) {
      var user = payload && payload.user;
      if (!user) {
        showGuest();
        return;
      }
      if (avatar) {
        // 中文名取首字，英文名取首字母大写；都没有就用 @ 前的字符
        var label = (user.name || user.email || '?').trim();
        avatar.textContent = label.slice(0, 1).toUpperCase();
      }
      if (nameNode) {
        nameNode.textContent = user.name || user.email;
        nameNode.removeAttribute('href');
        nameNode.setAttribute('title', user.email);
      }
      if (out) {
        out.hidden = false;
      }
    }).catch(function () {
      showGuest();
    });
  }

  /* ==========================================================================
     模型选择器
     ========================================================================== */

  var modelBtn = document.getElementById('modelBtn');
  var modelBtnName = document.getElementById('modelBtnName');
  var modelMenu = document.getElementById('modelMenu');
  var railModelName = document.getElementById('railModelName');

  var MARK_WHALE =
    '<svg class="model-item__mark" width="17" height="17" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M2.4 12.1c0-3.4 2.8-6.2 6.2-6.2 2.6 0 4.8 1.6 5.7 3.9l2.6-1c.5-.2 1 .2.9.7l-.5 2.5c-.1.5-.5.8-1 .8h-2.1c-1 2.2-3.2 3.7-5.8 3.7-1.8 0-3.4-.7-4.6-1.9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="7.5" cy="10.5" r="1" fill="currentColor"/></svg>';

  var MARK_CHECK =
    '<svg class="model-item__check" width="15" height="15" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3.2 8.4 6.4 11.6 12.8 4.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function currentModel() {
    var found = null;
    MODELS.forEach(function (model) {
      if (model.current) {
        found = model;
      }
    });
    return found || MODELS[0] || null;
  }

  /* 当前模型名同时出现在输入坞和侧栏入口上 */
  function syncModelLabel() {
    var active = currentModel();
    if (!active) {
      return;
    }
    if (modelBtnName) {
      modelBtnName.textContent = active.name;
    }
    if (railModelName) {
      railModelName.textContent = active.name;
    }
  }

  function renderModels() {
    var models = MODELS;
    if (!modelMenu || !modelBtnName || !models.length) {
      return;
    }

    syncModelLabel();

    modelMenu.textContent = '';
    modelMenu.appendChild(el('p', 'model-menu__title', '选择模型'));

    models.forEach(function (model) {
      var item = el('button', 'model-item');
      item.type = 'button';
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(!!model.current));
      item.dataset.model = model.id;
      item.insertAdjacentHTML('beforeend', MARK_WHALE);

      var text = el('span', 'model-item__text');
      var name = el('span', 'model-item__name', model.name);
      name.setAttribute('translate', 'no');
      text.appendChild(name);
      text.appendChild(el('span', 'model-item__desc', model.desc));
      item.appendChild(text);
      item.insertAdjacentHTML('beforeend', MARK_CHECK);

      item.addEventListener('click', function () {
        selectModel(model.id);
        closeModelMenu();
        modelBtn.focus();
      });

      modelMenu.appendChild(item);
    });

    var foot = el('div', 'model-menu__foot');
    var link = el('a', 'model-menu__link');
    link.href = 'settings.html';
    link.textContent = '模型配置…';
    foot.appendChild(link);
    modelMenu.appendChild(foot);
  }

  function selectModel(id) {
    MODELS.forEach(function (model) {
      model.current = model.id === id;
    });

    syncModelLabel();

    if (modelMenu) {
      modelMenu.querySelectorAll('.model-item').forEach(function (node) {
        node.setAttribute('aria-selected', String(node.dataset.model === id));
      });
    }

    // 带到配置中心 / 下次进入时沿用
    try {
      window.localStorage.setItem('margin.demo.model', id);
    } catch (error) {
      /* file:// 下可能不可用，忽略 */
    }

    if (onModelChange) {
      onModelChange(id);
    }
  }

  var onModelChange = null;

  function closeModelMenu() {
    if (!modelMenu || modelMenu.hidden) {
      return;
    }
    modelMenu.hidden = true;
    modelBtn.setAttribute('aria-expanded', 'false');
  }

  if (modelBtn && modelMenu) {
    modelBtn.addEventListener('click', function () {
      var open = modelBtn.getAttribute('aria-expanded') === 'true';
      modelBtn.setAttribute('aria-expanded', String(!open));
      modelMenu.hidden = open;
      if (!open) {
        var first = modelMenu.querySelector('.model-item');
        if (first) {
          first.focus();
        }
      }
    });

    document.addEventListener('click', function (event) {
      if (
        !modelMenu.hidden &&
        !modelMenu.contains(event.target) &&
        !modelBtn.contains(event.target)
      ) {
        closeModelMenu();
      }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        closeModelMenu();
      }
    });
  }

  /* ==========================================================================
     对外接口（给 scripts/live.js 用的渲染原语）
     刻意只暴露原语与状态开关，不暴露内部实现细节 ——
     两个数据源共用同一套渲染，视觉与交互不会出现第二套实现。
     ========================================================================== */

  var MARGIN_UI = {
    /* 渲染原语 */
    esc: esc,
    el: el,
    ICON: ICON,
    SEV_LABEL: SEV_LABEL,
    makeStep: makeStep,
    buildPlan: buildPlan,
    setStepState: setStepState,
    buildTool: buildTool,
    buildFindings: buildFindings,
    buildFinding: buildFinding,
    refreshFindingsHead: refreshFindingsHead,
    renderUserTurn: renderUserTurn,
    follow: follow,

    /* 产出渲染器由 outcome 决定。findings 走上面那套（有严重级别筛选）；
       blueprint 走蓝图；不认识的 outcome 走通用渲染器 ——
       没见过这个助手也必须能把它的产出显示出来，而不是白屏。 */
    createOutcome: function (outcome) {
      if (!window.MARGIN_BLUEPRINT) {
        return null;
      }
      if (outcome === 'blueprint') {
        return window.MARGIN_BLUEPRINT.create({ el: el, esc: esc });
      }
      return window.MARGIN_BLUEPRINT.createGeneric({ el: el, esc: esc });
    },
    agentId: function () {
      return window.MARGIN_AGENTS ? window.MARGIN_AGENTS.currentId() : 'review';
    },
    dialect: words,

    /* 运行状态 */
    setBusy: setBusy,
    isBusy: function () {
      return busy;
    },
    markSent: function () {
      sentOnce = true;
    },
    setStatus: function (label, step) {
      /* 运行条是按需创建的：live.js 在收到 step 事件时先调 setStatus、
         并不保证 setBusy(true) 已经先跑过（回放历史更是直接写状态）。
         所以这里要自己保证节点存在，否则会对着 null 写 textContent。 */
      var bar = ensureRunbar();
      bar.hidden = false;
      runbarLabel.textContent = label;
      runbarStep.textContent = step || '';
    },

    /* 侧栏与模型 */
    renderRail: renderRail,
    renderModels: renderModels,
    syncModelLabel: syncModelLabel,
    currentModel: currentModel,
    selectSession: selectSession,
    applyContext: applyContext,
    setSpaces: function (list) {
      SPACES = list || [];
    },
    setModels: function (list) {
      MODELS = list || [];
    },
    getActive: function () {
      return { space: activeSpace, session: activeSession };
    },
    selectModel: selectModel,
    onModelChange: function (fn) {
      onModelChange = fn;
    },
    onSessionSelect: function (fn) {
      onSessionSelect = fn;
    },
    onSpaceSelect: function (fn) {
      onSpaceSelect = fn;
    },
    onDraft: function (fn) {
      onDraft = fn;
    },
    onWorkspaceChange: function (fn) {
      onWorkspaceChange = fn;
    },
    startDraft: startDraft,
    selectSpace: selectSpace,

    /* 本次审查的范围（仓库内相对路径）。live.js 提交时带上。 */
    getScope: function () {
      return scope.slice();
    },

    /* 是否处于增量审查模式。live.js 据此给 /api/audit 加 diff 参数。 */
    isDiff: function () {
      return !!diffuse;
    },
    clearScope: function () {
      scope = [];
      renderScopeChips();
      renderComposerTarget();
    },
    setWorkspaceProvider: function (provider) {
      workspaceProvider = provider || null;
    },
    renderComposerTarget: renderComposerTarget,

    /* 实时模式钩子 */
    setLiveHandlers: function (handlers) {
      liveSend = (handlers && handlers.send) || null;
      liveStop = (handlers && handlers.stop) || null;
    },
    setPlaceholder: function (text) {
      input.placeholder = text;
    },
    replaceThread: replaceThread,
    thread: thread,
    closeDrawer: closeDrawer,
    /* 是否已经发送过（决定按钮显示「开始 X」还是「继续追问」） */
    sentOnce: function () {
      return sentOnce;
    },
  };

  window.MARGIN_UI = MARGIN_UI;

  /* ==========================================================================
     运行条：按需创建的玻璃胶囊
     --------------------------------------------------------------------------
     之前它是 HTML 里的固定块，用 hidden 控制显隐。问题有两个：
     ① 静态存在会让 aria-live 区域在加载时先播报一句「正在运行」；
     ② 它被塞在输入坞里当一行，占掉 40px，窄屏下把输入框压得很矮。
     现在改成浮在输入坞上方的胶囊：只有真的在跑才存在。
     ========================================================================== */

  function ensureRunbar() {
    if (runbar) {
      return runbar;
    }
    runbar = el('div', 'runbar');
    runbar.setAttribute('role', 'status');
    runbar.setAttribute('aria-live', 'polite');

    var pulse = el('span', 'runbar__pulse');
    pulse.setAttribute('aria-hidden', 'true');
    runbar.appendChild(pulse);

    /* 文案由方言写入。这里刻意留中性占位：静态写死「审查计划」会让
       架构助手在启动的一瞬间显示成审查。 */
    runbarLabel = el('span', 'runbar__label', '正在运行…');
    runbar.appendChild(runbarLabel);

    runbarStep = el('span', 'runbar__step num');
    runbar.appendChild(runbarStep);

    runbarStop = el('button', 'runbar__stop', '停止');
    runbarStop.type = 'button';
    runbarStop.addEventListener('click', stopAll);
    runbar.appendChild(runbarStop);

    if (dock) {
      dock.appendChild(runbar);
    }
    return runbar;
  }

  /* ==========================================================================
     未连接：唯一的「没有数据」状态
     --------------------------------------------------------------------------
     这一块替代了原来那套演示回放。区别很关键：
     演示回放会渲染一份**看起来像真结果**的审查（15 条发现、5 层架构图），
     而这里只说明「没连上、怎么连上」。后者不会让人误判系统能力。
     ========================================================================== */

  function renderOffline(options) {
    var opts = options || {};
    setBusy(false);
    var wrap = el('div', 'turn turn--agent');
    var box = el('section', 'offline');
    box.setAttribute('aria-label', '后端未连接');

    var mark = el('span', 'offline__mark');
    mark.innerHTML =
      '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
      '<path d="M3.4 6.3a6.4 6.4 0 0 1 9.2 0M5.4 8.6a3.5 3.5 0 0 1 5.2 0" fill="none" ' +
      'stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
      '<circle cx="8" cy="11.4" r="1.1" fill="currentColor"/>' +
      '<path d="M2.4 2.4l11.2 11.2" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round"/></svg>';

    box.appendChild(el('h2', 'offline__title', '后端没有连上'));

    var reason = opts.reason ? ' ' + opts.reason : '';
    box.appendChild(
      el(
        'p',
        'offline__body',
        'Margin 的仓库清单、运行记录和产出全部来自后端接口，界面本身不携带任何样例数据。' +
          '所以连不上后端时，这里不会显示一份「已经跑完的审查」—— 那会让人误判系统能力。' +
          reason
      )
    );

    var steps = el('div', 'offline__steps');

    var s1 = el('div', 'offline__step');
    s1.appendChild(
      spanWithCode('在项目根目录启动服务：', 'python -m harness.server --port 8765')
    );
    steps.appendChild(s1);

    var s2 = el('div', 'offline__step');
    var t2 = document.createElement('span');
    t2.appendChild(document.createTextNode('用服务地址打开工作台：'));
    var code2 = el('code', null, 'http://127.0.0.1:8765/');
    t2.appendChild(code2);
    t2.appendChild(
      document.createTextNode('。用 file:// 双击打开会因跨域被浏览器拦住，这是设计如此。')
    );
    s2.appendChild(t2);
    steps.appendChild(s2);

    var s3 = el('div', 'offline__step');
    s3.appendChild(
      spanWithCode('在配置中心填入模型密钥：', 'DEEPSEEK_API_KEY')
    );
    steps.appendChild(s3);

    box.appendChild(steps);

    box.appendChild(
      el(
        'p',
        'offline__note',
        '已经启动了却还是连不上，看两点：端口是不是被别的进程占了；' +
          '浏览器用的地址与终端里打印的地址是不是同一个（127.0.0.1 与 localhost 算两个源）。'
      )
    );

    finishOfflineBox(box, opts);
    wrap.appendChild(box);
    replaceThread(wrap);
  }

  function spanWithCode(text, code) {
    var node = document.createElement('span');
    node.appendChild(document.createTextNode(text));
    node.appendChild(el('code', 'offline__code', code));
    return node;
  }

  function finishOfflineBox(box, opts) {
    var actions = el('div', 'offline__actions');

    if (opts.retry) {
      var again = el('button', 'offline__btn offline__btn--primary', '重新连接');
      again.type = 'button';
      again.addEventListener('click', function () {
        again.disabled = true;
        again.textContent = '正在重连…';
        boot().catch(function () {
          again.disabled = false;
          again.textContent = '重新连接';
        });
      });
      actions.appendChild(again);
    }

    var cfg = el('a', 'offline__btn', '打开配置中心');
    cfg.href = 'settings.html';
    actions.appendChild(cfg);

    box.appendChild(actions);
  }

  /* ==========================================================================
     主题：深色为默认，浅色是同构覆盖
     ========================================================================== */

  function applyTheme(theme) {
    var next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try {
      window.localStorage.setItem('margin.theme', next);
    } catch (error) {
      /* 隐私模式下写不进去，主题这次有效、刷新回默认，可接受 */
    }
    if (themeBtn) {
      /* aria-pressed 表示「当前是否浅色」，与按钮图标一致 */
      themeBtn.setAttribute('aria-pressed', String(next === 'light'));
      themeBtn.setAttribute(
        'aria-label',
        next === 'light' ? '切换到深色主题' : '切换到浅色主题'
      );
    }
    if (themeIcon) {
      themeIcon.innerHTML =
        next === 'light'
          ? '<circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
            '<path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1" ' +
            'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'
          : '<path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8Z" fill="none" ' +
            'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>';
    }
    /* 蓝图连线是按像素测出来的（corridorX 读 DOM 宽度），主题切换改变的是
       颜色不是尺寸，不需要重画；但字体加载完会变，所以顺手重画一次更稳。 */
    if (window.MARGIN_BLUEPRINT && window.MARGIN_BLUEPRINT.redrawAll) {
      window.MARGIN_BLUEPRINT.redrawAll();
    }
  }

  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var now = document.documentElement.getAttribute('data-theme');
      applyTheme(now === 'light' ? 'dark' : 'light');
    });
  }

  /* ==========================================================================
     增量审查开关
     --------------------------------------------------------------------------
     只对声明了 capabilities.diff 的助手显示。这个值来自后端清单，
     不在前端按 agent id 硬编码 —— 接第三个助手时不用改这里。
     ========================================================================== */

  function applyDiffCapability() {
    if (!diffBtn) {
      return;
    }
    var agent =
      window.MARGIN_AGENTS && window.MARGIN_AGENTS.current
        ? window.MARGIN_AGENTS.current()
        : null;
    var caps = (agent && agent.capabilities) || {};
    diffBtn.hidden = !caps.diff;
    if (!caps.diff) {
      diffuse = false;
      diffBtn.setAttribute('aria-pressed', 'false');
    }
  }

  if (diffBtn) {
    diffBtn.addEventListener('click', function () {
      diffuse = !diffuse;
      diffBtn.setAttribute('aria-pressed', String(diffuse));
      diffBtn.title = diffuse
        ? '只审 HEAD 到工作树的改动（点击关闭，回到全仓审查）'
        : '只审 BASE 到工作树的改动';
      diffBtn.textContent = diffuse ? '增量 · 开' : '增量';
    });
  }

  /* ==========================================================================
     启动
     ========================================================================== */

  async function boot() {
    /* 账号态先填上：侧栏底部的用户名不该由静态 HTML 决定 */
    runAuth();

    /* 助手清单：必须来自后端。拿不到就说明后端没连上，
       这是唯一可信的判断依据（/api/agents 是公开接口，不需要登录）。 */
    var agentsReady = false;
    if (window.MARGIN_AGENTS && typeof window.MARGIN_AGENTS.boot === 'function') {
      try {
        await window.MARGIN_AGENTS.boot();
        agentsReady = true;
      } catch (error) {
        agentsReady = false;
      }
    }

    /* 切换助手：实时模式重新取这一位助手的历史；没连上就只换外壳。 */
    if (window.MARGIN_AGENTS) {
      window.MARGIN_AGENTS.onChange(function () {
        applyDiffCapability();
        applyDialect();
        if (window.MARGIN_LIVE && window.MARGIN_LIVE.isLive && window.MARGIN_LIVE.isLive()) {
          window.MARGIN_LIVE.refocus().catch(function (error) {
            if (window.console && console.warn) {
              console.warn('[margin] 切换助手失败：', error);
            }
          });
          return;
        }
        renderOffline({ reason: '切换助手时需要后端在线才能取到它的历史。', retry: true });
      });
    }

    applyDiffCapability();

    if (window.MARGIN_LIVE && typeof window.MARGIN_LIVE.tryBoot === 'function') {
      try {
        var tookOver = await window.MARGIN_LIVE.tryBoot(MARGIN_UI);
        if (tookOver) {
          return;
        }
      } catch (error) {
        if (window.console && console.warn) {
          console.warn('[margin] 实时模式启动失败：', error);
        }
      }
    }

    /* 到这里说明后端不可达。不降级到假数据，直接如实说。 */
    renderRail();
    renderModels();
    renderOffline({
      reason: agentsReady
        ? '助手清单读到了，但空间与运行记录没取到，可能是会话已过期或权限被拒。'
        : '',
      retry: true,
    });
  }

  boot().catch(function (error) {
    setBusy(false);
    replaceThread(null);
    thread.appendChild(el('p', 'blank__text', '界面初始化失败：' + (error && error.message)));
  });
})();
