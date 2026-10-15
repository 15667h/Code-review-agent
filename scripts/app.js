/* ==========================================================================
   Margin · 交互层
   纯原生 JS，无构建步骤。演示数据来自 scripts/mock-data.js。
   设计约束：动效只用于状态变化；所有可交互元素具备完整状态；键盘可达。
   ========================================================================== */

(function () {
  'use strict';

  var DATA = window.MARGIN_DATA;
  if (!DATA) {
    return;
  }

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
      var li = el('li', 'step is-pending');
      var mark = el('span', 'step__mark');
      mark.setAttribute('aria-hidden', 'true');
      var label = el('span', 'step__label', step.label);
      var meta = el('span', 'step__meta', step.meta);
      var dur = el('span', 'step__dur num', '');
      li.appendChild(mark);
      li.appendChild(label);
      li.appendChild(meta);
      li.appendChild(dur);
      list.appendChild(li);
      items.push({ el: li, mark: mark, dur: dur, meta: meta });
    });

    root.appendChild(list);

    return { root: root, items: items, total: total };
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

  function buildFindings(findings) {
    var root = el('section', 'findings is-animated');
    root.setAttribute('aria-label', '审查发现');

    var counts = { blocker: 0, warn: 0, note: 0, pass: 0 };
    findings.forEach(function (f) {
      counts[f.severity] = (counts[f.severity] || 0) + 1;
    });

    var head = el('header', 'findings__head');
    head.appendChild(el('h3', 'findings__title', '发现'));

    var filter = el('div', 'sev-filter');
    filter.setAttribute('role', 'group');
    filter.setAttribute('aria-label', '按严重级别筛选');

    var chips = [];
    var options = [{ key: 'all', label: '全部', count: findings.length }];
    SEV_ORDER.forEach(function (key) {
      if (counts[key]) {
        options.push({ key: key, label: SEV_LABEL[key], count: counts[key] });
      }
    });

    options.forEach(function (option, index) {
      var chip = el('button', 'sev-chip');
      chip.type = 'button';
      chip.dataset.sev = option.key;
      chip.setAttribute('aria-pressed', index === 0 ? 'true' : 'false');
      chip.appendChild(el('span', null, option.label));
      var count = el('span', 'num', String(option.count));
      chip.appendChild(count);
      filter.appendChild(chip);
      chips.push(chip);
    });

    filter.addEventListener('click', function (event) {
      var chip = event.target.closest('.sev-chip');
      if (!chip) {
        return;
      }
      applyFilter(root, chip.dataset.sev);
    });

    head.appendChild(filter);
    root.appendChild(head);

    var list = el('div', 'findings__list');
    root.appendChild(list);

    return { root: root, list: list, chips: chips };
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
    var spaces = DATA.spaces || [];
    spaceCount.textContent = String(spaces.length);
    spaceList.textContent = '';

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
    var spaces = DATA.spaces || [];
    var total = spaces.reduce(function (sum, space) {
      return sum + space.sessions.length;
    }, 0);
    railUserNote.textContent = spaces.length + ' 个空间 · ' + total + ' 次审查';
  }

  function selectSession(space, session) {
    (DATA.spaces || []).forEach(function (item) {
      item.sessions.forEach(function (entry) {
        entry.current = false;
      });
    });
    session.current = true;

    spaceList.querySelectorAll('.session').forEach(function (node) {
      node.removeAttribute('aria-current');
    });
    var active = spaceList.querySelector('[data-session="' + session.id + '"]');
    if (active) {
      active.setAttribute('aria-current', 'true');
    }

    spaceList.querySelectorAll('.space__row').forEach(function (node) {
      node.classList.remove('is-current');
    });
    var spaceNode = spaceList.querySelector('[data-space="' + space.id + '"] .space__row');
    if (spaceNode) {
      spaceNode.classList.add('is-current');
    }

    updateContext(space, session);
    closeDrawer();
  }

  function updateContext(space, session) {
    wsRepo.textContent = space.owner + '/' + space.name;
    wsPr.textContent = '#' + session.pr;

    wsBranch.innerHTML = ICON.branch;
    wsBranch.appendChild(document.createTextNode(session.branch));

    wsFiles.textContent = session.files + ' 个文件';
    wsAdd.textContent = '+' + session.add;
    wsDel.textContent = '\u2212' + session.del;
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
    var list = DATA.models || [];
    var found = null;
    list.forEach(function (model) {
      if (model.current) {
        found = model;
      }
    });
    return found || list[0] || null;
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
    var models = DATA.models || [];
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
    (DATA.models || []).forEach(function (model) {
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
  }

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
     启动
     ========================================================================== */

  async function boot() {
    renderRail();

    // 配置中心里改过的模型选择，回到工作台继续沿用
    try {
      var savedModel = window.localStorage.getItem('margin.demo.model');
      if (
        savedModel &&
        (DATA.models || []).some(function (model) {
          return model.id === savedModel;
        })
      ) {
        (DATA.models || []).forEach(function (model) {
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
