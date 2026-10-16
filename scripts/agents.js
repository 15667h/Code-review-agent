/* ==========================================================================
   Margin · 助手（Agent）清单与切换

   产品形态是「一个工作台 + 多个研发辅助 Agent」，所以**哪些助手存在**是运行时
   从后端 `/api/agents` 取的数据，不是写死在前端的。这份文件负责四件事：

   1. 拉清单（取不到就退回内置声明，绝不显示一个凭空编出来的助手）；
   2. 渲染左侧 56px 助手轨道；
   3. 切换时把 `--agent` 强调色、输入坞文案、产出渲染器一起换掉；
   4. 记住选择（localStorage）+ URL 深链（?agent=）。

   第 3 条里的**强调色是 agent 声明的一个值**，不是一套样式表：
   拿到清单后按 id 注入 `body[data-agent="x"] { --agent: <accent> }`，
   深色与浅色变体由 CSS `color-mix` 从这一个值派生。
   接一个新助手只多一个色值，组件样式一行都不用改。
   ========================================================================== */

(function () {
  'use strict';

  var STORE_KEY = 'margin.agent';

  /* 内置声明：仅在拿不到后端清单时兜底（file:// 演示模式、后端未启动）。
     内容与 backend/harness/agents.py 的注册表保持一致；
     一旦接口可用就以接口为准 —— 否则用户装了新助手却看不到。 */
  var FALLBACK = [
    {
      id: 'review',
      name: '代码审查',
      tagline: '在仓库里找出真实存在的缺陷，每条都带可核验的证据',
      accent: '#0f6b5c',
      room: '校样台',
      outcome: 'findings',
      artifact_kinds: ['finding'],
      can_edit: true,
    },
    {
      id: 'architect',
      name: '架构设计',
      tagline: '针对现状产出可评审的方案：分层、组件、决策与未定的问题',
      accent: '#1c3f6e',
      room: '制图室',
      outcome: 'blueprint',
      artifact_kinds: ['layer', 'component', 'decision', 'open_question'],
      can_edit: false,
    },
  ];

  /* 每个助手的「方言」：同一个输入坞，不同助手说不同的话。
     按 outcome 组织而不是按 id —— 将来第三个审查类助手自动继承这套话术。 */
  var DIALECT = {
    findings: {
      placeholder: '描述这次审查的重点，回车开始真实审计…',
      send: '开始审查',
      follow: '继续追问',
      running: '正在运行审查计划…',
      hint: '发现均为静态分析结果，合并前请人工确认',
      newLabel: '新建审查',
      draftPrefix: '新建审查 → ',
      empty: '还没有审查记录。在下面写明审查重点并回车，就会为 %s 新建一次审查。',
      noRepo: '请先在左侧选择一个仓库，再描述审查重点。',
      unit: '条',
      planTitle: '审查计划',
      done: '审查完成',
      doneWith: '已完成',
    },
    blueprint: {
      placeholder: '描述设计目标与约束（量级、SLA、团队），回车开始…',
      send: '开始设计',
      follow: '继续追问',
      running: '正在推进设计…',
      hint: '产出的是可评审的方案草稿：分层、组件、决策与未定的问题',
      newLabel: '新建设计',
      draftPrefix: '新建设计 → ',
      empty: '还没有设计记录。在下面写明目标与约束并回车，就会为 %s 新建一次设计。',
      noRepo: '请先在左侧选择一个仓库，再描述设计目标。',
      unit: '项',
      planTitle: '设计计划',
      done: '设计完成',
      doneWith: '已完成',
    },
  };

  var GENERIC_DIALECT = {
    placeholder: '描述这次任务，回车开始…',
    send: '开始',
    follow: '继续追问',
    running: '正在运行…',
    hint: '产出由该助手自己声明',
    newLabel: '新建任务',
    draftPrefix: '新建任务 → ',
    empty: '还没有任务记录。在下面写明目标并回车，就会为 %s 新建一次任务。',
    noRepo: '请先在左侧选择一个仓库，再描述任务目标。',
    unit: '条',
    planTitle: '执行计划',
    done: '任务完成',
    doneWith: '已完成',
  };

  var ICON = {
    /* 校样台：一张纸 + 批注竖线 */
    review:
      '<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
      '<path d="M3.4 2.8v14.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
      '<path d="M7.6 6.6h8.4M7.6 10h6M7.6 13.4h7.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
      '</svg>',
    /* 制图室：三层堆叠 */
    architect:
      '<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
      '<rect x="3" y="3" width="14" height="4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
      '<rect x="3" y="8" width="14" height="4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
      '<rect x="3" y="13" width="14" height="4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2.6 2"/>' +
      '</svg>',
    /* 未认识的助手：一个方框，不给它编一个假图标语义 */
    generic:
      '<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
      '<rect x="3.4" y="3.4" width="13.2" height="13.2" rx="2" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
      '<path d="M7.4 10h5.2M10 7.4v5.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>' +
      '</svg>',
  };

  var list = FALLBACK.slice();
  var currentId = FALLBACK[0].id;
  var listeners = [];
  var styleNode = null;

  function find(id) {
    var found = null;
    list.forEach(function (item) {
      if (item.id === id) {
        found = item;
      }
    });
    return found;
  }

  function dialect(id) {
    var agent = find(id || currentId);
    if (!agent) {
      return GENERIC_DIALECT;
    }
    return DIALECT[agent.outcome] || GENERIC_DIALECT;
  }

  function text(id, fallback) {
    return String(id ? id : fallback || '');
  }

  /* ---- 强调色：只注入一个色值，深浅两档由 CSS 派生 ---- */
  function injectAccents() {
    if (!styleNode) {
      styleNode = document.createElement('style');
      styleNode.id = 'agent-accents';
      document.head.appendChild(styleNode);
    }
    var css = list
      .map(function (agent) {
        var accent = text(agent.accent, '#0f6b5c');
        return (
          'body[data-agent="' + agent.id + '"]{' +
          '--agent:' + accent + ';' +
          '--agent-deep:color-mix(in oklab,' + accent + ' 78%,#000);' +
          '--agent-soft:color-mix(in oklab,' + accent + ' 12%,#fff);' +
          '--agent-line:color-mix(in oklab,' + accent + ' 30%,#fff);' +
          '}'
        );
      })
      .join('\n');
    styleNode.textContent = css;
  }

  /* ---- 轨道 ---- */
  function renderTray() {
    var tray = document.getElementById('agentTray');
    if (!tray) {
      return;
    }
    tray.textContent = '';

    var ul = document.createElement('ul');
    ul.className = 'tray__list';

    list.forEach(function (agent) {
      var li = document.createElement('li');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tray__item';
      btn.dataset.agentId = agent.id;
      btn.setAttribute('aria-pressed', String(agent.id === currentId));
      btn.setAttribute('aria-label', agent.name + ' · ' + (agent.room || ''));
      btn.title = agent.name + '：' + (agent.tagline || '');

      var icon = document.createElement('span');
      icon.className = 'tray__icon';
      icon.innerHTML = ICON[agent.id] || ICON[agent.outcome] || ICON.generic;
      btn.appendChild(icon);

      var label = document.createElement('span');
      label.className = 'tray__label';
      label.textContent = agent.name;
      btn.appendChild(label);

      btn.addEventListener('click', function () {
        select(agent.id);
      });

      li.appendChild(btn);
      ul.appendChild(li);
    });

    tray.appendChild(ul);
  }

  /* ---- 顶栏标记：当前助手一眼可见 ---- */
  function renderMarker(agent) {
    var node = document.getElementById('wsAgent');
    if (!node) {
      return;
    }
    if (!agent) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    node.textContent = agent.name;
    node.title = agent.tagline || agent.name;
  }

  /* ---- 输入坞文案 ---- */
  function applyDialect() {
    var agent = find(currentId);
    var words = dialect(currentId);

    var input = document.getElementById('prompt');
    if (input) {
      input.placeholder = words.placeholder;
    }

    var newBtn = document.getElementById('newReview');
    if (newBtn) {
      var label = newBtn.querySelector('span');
      if (label) {
        label.textContent = words.newLabel;
      }
    }

    // 只换后半句：前半句是 Enter / Shift+Enter 的操作提示，跟助手无关
    var hint = document.getElementById('composerHintText');
    if (hint) {
      hint.textContent = words.hint;
    }

    /* 发送按钮也要跟着换。漏了它，实时模式下切到架构助手后按钮仍写「开始审查」，
       而输入框里已是「开始设计…」—— 同一时刻自相矛盾（app.js 的 setBusy 只在
       运行开始/结束时刷新，切助手这条路不走它）。 */
    var sendLabel = document.querySelector('#sendBtn span');
    if (sendLabel && window.MARGIN_UI && window.MARGIN_UI.sentOnce) {
      sendLabel.textContent = window.MARGIN_UI.sentOnce() ? words.follow : words.send;
    }

    renderMarker(agent);
  }

  function emit() {
    var agent = find(currentId);
    listeners.forEach(function (fn) {
      fn(agent, dialect(currentId));
    });
  }

  function select(id, options) {
    var silent = options && options.silent;
    if (!find(id) || id === currentId) {
      return;
    }
    currentId = id;

    document.body.dataset.agent = id;
    renderTray();
    applyDialect();

    try {
      window.localStorage.setItem(STORE_KEY, id);
    } catch (error) {
      /* file:// 下可能不可用，忽略 */
    }

    /* URL 反映状态：刷新或把链接发给别人，落到的还是同一个助手 */
    try {
      var url = new URL(window.location.href);
      if (url.searchParams.get('agent') !== id) {
        url.searchParams.set('agent', id);
        window.history.replaceState(null, '', url.toString());
      }
    } catch (error) {
      /* 同上 */
    }

    if (!silent) {
      emit();
    }
  }

  function initialId(payload) {
    /* 优先级：URL > 上次选择 > 后端 default > 清单第一项 */
    var fromUrl = '';
    try {
      fromUrl = new URL(window.location.href).searchParams.get('agent') || '';
    } catch (error) {
      fromUrl = '';
    }
    if (fromUrl && find(fromUrl)) {
      return fromUrl;
    }

    var saved = '';
    try {
      saved = window.localStorage.getItem(STORE_KEY) || '';
    } catch (error) {
      saved = '';
    }
    if (saved && find(saved)) {
      return saved;
    }

    var fallbackDefault = payload && payload.default;
    if (fallbackDefault && find(fallbackDefault)) {
      return fallbackDefault;
    }
    return list[0].id;
  }

  /* ---- 启动 ---- */
  async function boot(options) {
    var opts = options || {};
    if (opts.onChange) {
      listeners.push(opts.onChange);
    }

    var payload = null;
    if (window.MARGIN_API && window.MARGIN_API.served && window.MARGIN_API.agents) {
      payload = await window.MARGIN_API.agents();
    }
    if (payload && Array.isArray(payload.agents) && payload.agents.length) {
      list = payload.agents.slice();
    }

    injectAccents();
    currentId = initialId(payload);
    document.body.dataset.agent = currentId;
    renderTray();
    applyDialect();
    return find(currentId);
  }

  window.MARGIN_AGENTS = {
    boot: boot,
    list: function () {
      return list.slice();
    },
    current: function () {
      return find(currentId);
    },
    currentId: function () {
      return currentId;
    },
    dialect: dialect,
    /* 按产出形态取方言。回放历史记录时用得上：一条架构记录就该说
       「设计计划」，哪怕现在选中的是审查助手。 */
    dialectFor: function (outcome) {
      return DIALECT[outcome] || GENERIC_DIALECT;
    },
    /* 产出形态决定用哪套渲染器。认识 blueprint 用蓝图，其余用通用渲染器 ——
       一个没见过的助手也必须能把自己的产出显示出来。 */
    outcome: function () {
      var agent = find(currentId);
      return agent ? agent.outcome : 'findings';
    },
    onChange: function (fn) {
      listeners.push(fn);
    },
    select: select,
    applyDialect: applyDialect,
  };
})();
