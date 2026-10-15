/* ==========================================================================
   Margin · 交互层
   纯原生 JS，无构建步骤。

   数据有两个来源，由 scripts/live.js 在启动时决定走哪一个：
   - **实时模式**：页面由 backend/harness/server.py 托管时，空间/会话/审计流
     全部来自后端真实接口；
   - **演示模式**：用 file:// 直接双击打开时，数据来自 scripts/mock-data.js。
   两个模式共用下面这套渲染原语，所以视觉与交互不会有第二套实现。
   ========================================================================== */

(function () {
  'use strict';

  var DATA = window.MARGIN_DATA || {};

  /* 可变的数据源：演示模式取 mock，实时模式由 live.js 灌入 */
  var SPACES = (DATA.spaces || []).slice();
  var MODELS = (DATA.models || []).slice();

  /* 由 live.js 注入的实时模式钩子；为空即表示当前是演示模式 */
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
  var sendLabel = sendBtn.querySelector('span');
  var stopBtn = document.getElementById('stopBtn');
  var runbar = document.getElementById('runbar');
  var runbarLabel = document.getElementById('runbarLabel');
  var runbarStep = document.getElementById('runbarStep');
  var runbarStop = document.getElementById('runbarStop');

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

  function buildPlan(steps) {
    var root = el('section', 'plan is-animated');
    root.setAttribute('aria-label', '审查计划');

    var head = el('header', 'plan__head');
    head.appendChild(el('h3', 'plan__title', '审查计划'));
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

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
    stopBtn.hidden = !state;
    runbar.hidden = !state;
    if (state) {
      sendLabel.textContent = '运行中';
    } else {
      sendLabel.textContent = sentOnce ? '继续追问' : '开始审查';
    }
  }

  function finishTurn(turn) {
    if (cancelled) {
      turn.appendChild(
        el('p', 'run__note', '已停止。上面是停止前已完成的部分，可以继续追问。')
      );
    }
    setBusy(false);
    follow();
  }

  async function playRun(run, options) {
    var speed = (options && options.speed) || 1;
    cancelled = false;
    setBusy(true);
    removeFollowUps();

    var turn = el('article', 'turn turn--agent');
    thread.appendChild(turn);

    /* --- 开场白 --- */
    if (run.intro) {
      var intro = el('p', 'run__intro is-animated', run.intro);
      turn.appendChild(intro);
      runbarLabel.textContent = DATA.phases.reading;
      runbarStep.textContent = '';
      follow(true);
      await sleep(380 * speed);
      if (cancelled) {
        finishTurn(turn);
        return;
      }
    }

    /* --- 计划：逐步推进 --- */
    var plan = buildPlan(run.plan);
    turn.appendChild(plan.root);
    plan.total.textContent = run.plan.length + ' 步';
    follow(true);
    await sleep(260 * speed);

    for (var i = 0; i < run.plan.length; i += 1) {
      if (cancelled) {
        finishTurn(turn);
        return;
      }
      var item = plan.items[i];
      setStepState(item, 'active');
      runbarLabel.textContent = DATA.phases.running;
      runbarStep.textContent = i + 1 + ' / ' + run.plan.length;
      follow();

      await sleep(340 * speed);
      if (cancelled) {
        finishTurn(turn);
        return;
      }

      setStepState(item, 'done', run.plan[i].duration);
      await sleep(110 * speed);
    }

    plan.total.textContent =
      run.plan.length + ' 步 · ' + sumDurations(run.plan.map(function (s) { return s.duration; }));

    /* --- 工具调用 --- */
    if (run.tools && run.tools.length) {
      var tools = el('div', 'tools');
      turn.appendChild(tools);
      for (var t = 0; t < run.tools.length; t += 1) {
        if (cancelled) {
          finishTurn(turn);
          return;
        }
        var card = buildTool(run.tools[t]);
        card.classList.add('is-animated');
        tools.appendChild(card);
        follow();
        await sleep(400 * speed);
      }
    }

    /* --- 发现：逐条列出 --- */
    if (run.findings && run.findings.length) {
      var section = buildFindings(run.findings);
      turn.appendChild(section.root);
      for (var f = 0; f < run.findings.length; f += 1) {
        if (cancelled) {
          finishTurn(turn);
          return;
        }
        var finding = buildFinding(run.findings[f]);
        finding.classList.add('is-animated');
        section.list.appendChild(finding);
        follow();
        await sleep(460 * speed);
      }
    }

    /* --- 结语 --- */
    if (run.summary) {
      if (cancelled) {
        finishTurn(turn);
        return;
      }
      var summary = el('p', 'run__summary is-animated', run.summary);
      turn.appendChild(summary);
      follow();
      await sleep(200 * speed);
    }

    finishTurn(turn);
  }

  function sumDurations(list) {
    var total = list.reduce(function (sum, item) {
      return sum + (parseFloat(item) || 0);
    }, 0);
    return total.toFixed(1) + 's';
  }

  /* ==========================================================================
     追问快捷入口
     ========================================================================== */

  function renderFollowUps() {
    if (!DATA.followUps || thread.querySelector('.followups')) {
      return;
    }
    var wrap = el('div', 'followups is-animated');
    wrap.setAttribute('aria-label', '继续追问');

    DATA.followUps.forEach(function (item) {
      var chip = el('button', 'followup');
      chip.type = 'button';
      chip.textContent = item.label;
      chip.addEventListener('click', function () {
        if (item.action === 'filter') {
          var target = thread.querySelector('.findings');
          if (target) {
            applyFilter(target, item.value);
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
          return;
        }
        if (busy) {
          return;
        }
        send(item.value);
      });
      wrap.appendChild(chip);
    });

    thread.appendChild(wrap);
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
    /* 实时模式：这句话就是一次真实审计的审计目标 */
    if (liveSend) {
      await liveSend(text);
      return;
    }

    renderUserTurn(text);
    sentOnce = true;

    var run = DATA.replies[replyIndex % DATA.replies.length];
    replyIndex += 1;

    await playRun(run, { speed: 1 });
    renderFollowUps();
  }

  var replyIndex = 0;

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
  runbarStop.addEventListener('click', stopAll);

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

  function renderRail() {
    var spaces = SPACES;
    spaceCount.textContent = String(spaces.length);
    spaceList.textContent = '';

    if (!spaces.length) {
      var empty = el('li', 'space-empty');
      empty.appendChild(el('p', null, '还没有可审查的仓库。'));
      empty.appendChild(
        el('p', 'space-empty__hint', '把仓库放到 backend/examples/ 下，或重启服务时用 --repo 指定。')
      );
      spaceList.appendChild(empty);
      updateRailNote();
      return;
    }

    spaces.forEach(function (space) {
      var li = el('li', 'space');
      li.dataset.open = String(!!space.open);
      li.dataset.space = space.id;

      var row = el('button', 'space__row');
      row.type = 'button';
      row.setAttribute('aria-expanded', String(!!space.open));
      row.setAttribute('aria-controls', 'space-' + space.id);
      row.insertAdjacentHTML('beforeend', ICON.chevronRight);
      row.insertAdjacentHTML('beforeend', ICON.folder);

      var name = el('span', 'space__name', space.name);
      name.setAttribute('translate', 'no');
      row.appendChild(name);
      row.appendChild(el('span', 'space__count num', String(space.sessions.length)));
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

      row.addEventListener('click', function () {
        var isOpen = li.dataset.open === 'true';
        li.dataset.open = String(!isOpen);
        row.setAttribute('aria-expanded', String(!isOpen));
        ul.hidden = isOpen;
      });

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
    var note = spaces.length + ' 个空间 · ' + total + ' 次审查';
    if (liveSend) {
      note += ' · 已连接后端';
    }
    railUserNote.textContent = note;
  }

  function selectSession(space, session) {
    applyContext(space, session);

    // 只有「用户点击」这条路径才通知外部，程序化选中走 applyContext
    if (onSessionSelect) {
      onSessionSelect(space, session);
    }
    closeDrawer();
  }

  var onSessionSelect = null;

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
    input.focus();
    closeDrawer();
  });

  /* 退出登录：清掉本地标记后回到登录页 */
  var signOut = document.getElementById('signOut');
  if (signOut) {
    signOut.addEventListener('click', function () {
      try {
        window.sessionStorage.removeItem('margin.demo.user');
      } catch (error) {
        /* file:// 下可能不可用，忽略 */
      }
      window.location.href = 'login.html';
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

    /* 运行状态 */
    setBusy: setBusy,
    isBusy: function () {
      return busy;
    },
    markSent: function () {
      sentOnce = true;
    },
    setStatus: function (label, step) {
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
    getSpaces: function () {
      return SPACES;
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

    /* 实时模式钩子 */
    setLiveHandlers: function (handlers) {
      liveSend = (handlers && handlers.send) || null;
      liveStop = (handlers && handlers.stop) || null;
    },
    setPlaceholder: function (text) {
      input.placeholder = text;
    },
    replaceThread: function (node) {
      thread.textContent = '';
      if (node) {
        thread.appendChild(node);
      }
    },
    thread: thread,
    closeDrawer: closeDrawer,
  };

  window.MARGIN_UI = MARGIN_UI;

  /* ==========================================================================
     启动
     ========================================================================== */

  async function boot() {
    /* 优先尝试实时模式：页面由后端托管、且后端可达时才成立 */
    if (window.MARGIN_LIVE && typeof window.MARGIN_LIVE.tryBoot === 'function') {
      try {
        var tookOver = await window.MARGIN_LIVE.tryBoot(MARGIN_UI);
        if (tookOver) {
          return;
        }
      } catch (error) {
        /* 后端不可用或中途出错：安静降级到演示数据，不把用户卡在白屏 */
        if (window.console && console.warn) {
          console.warn('[margin] 实时模式启动失败，已降级到演示数据：', error);
        }
      }
    }

    renderRail();

    /* 顶栏上下文一律由数据驱动：HTML 里不再留静态默认值，
       所以两种模式都必须显式选中一条记录，否则顶栏是空的。 */
    var bootSpace = SPACES[0];
    if (bootSpace && bootSpace.sessions.length) {
      var bootSession =
        bootSpace.sessions.filter(function (item) {
          return item.current;
        })[0] || bootSpace.sessions[0];
      applyContext(bootSpace, bootSession);
    }

    // 配置中心里改过的模型选择，回到工作台继续沿用
    try {
      var savedModel = window.localStorage.getItem('margin.demo.model');
      if (
        savedModel &&
        MODELS.some(function (model) {
          return model.id === savedModel;
        })
      ) {
        MODELS.forEach(function (model) {
          model.current = model.id === savedModel;
        });
      }
    } catch (error) {
      /* file:// 下可能不可用，忽略 */
    }

    renderModels();

    if (NARROW.matches) {
      railToggle.setAttribute('aria-expanded', 'false');
    }

    var opening = DATA.opening;

    if (opening.reviewedAt) {
      var fmt = new Intl.DateTimeFormat('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
      runbarStep.textContent = '';
      input.placeholder =
        '最近一次审查 ' + fmt.format(new Date(opening.reviewedAt)) + '，继续追问或描述新的审查重点…';
    }

    renderUserTurn(opening.user);
    await sleep(160);
    await playRun(opening, { speed: 0.42 });
    renderFollowUps();
  }

  boot().catch(function (error) {
    setBusy(false);
    thread.appendChild(el('p', 'blank__text', '演示数据加载失败：' + error.message));
  });
})();
