/* ==========================================================================
   Margin 原型 · 交互
   纯原生，无构建。这里演示三件事：
   ① 切换助手 → 强调色、上下文栏内容、产出面板、输入坞文案一起变
   ② 架构图的图-清单双向绑定（点节点 → 高亮连线 + 右侧换成它的详情）
   ③ 助手浮层（可插拔的可见入口）
   ========================================================================== */

(function () {
  'use strict';

  /* ---------------------------------------------------------------- 助手配置
     这就是「可插拔」的契约雏形：一个助手 = 一组声明。
     真实实现里这些应该来自后端，前端只认这份结构。 */
  var AGENTS = {
    review: {
      room: '校样台',
      title: 'ledger-core / #1291',
      meta: '<span><b>2</b> 个文件</span><span><b>4</b> 条发现</span><span><b>2</b> 条阻断</span>',
      newItem: '新建审查',
      foot: '3 个空间，共 5 次审查',
      placeholder: '描述审查重点，或直接回车开始全量审查…',
      send: '开始审查',
      suggest: ['重点看并发与资源泄漏', '只看这次改动的文件', '解释这条阻断的复现路径'],
    },
    architect: {
      room: '制图室',
      title: '订单服务拆分 / v3',
      meta: '<span><b>3</b> 层</span><span><b>8</b> 组件</span><span><b>2</b> 项待确认</span>',
      newItem: '新建设计',
      foot: '2 个设计任务，共 3 个方案版本',
      placeholder: '描述系统目标与约束…',
      send: '开始设计',
      suggest: ['先给我三个可选方案再推荐', '只考虑 3 人团队能维护的方案', '这笔预算下最该先放弃什么'],
    },
  };

  /* ------------------------------------------------------------- 图纸数据 */

  var NODES = {
    gateway: {
      name: 'API Gateway',
      role: '协议转换与准入',
      duty: '只做路由、鉴权前置、限流。不做任何业务判断 —— 业务判断下沉能避免网关变成第二个单体。',
      iface: ['POST /orders', 'GET /orders/{id}', 'POST /payments/*'],
      up: [],
      down: ['order', 'payment', 'inventory'],
      fail: '网关不可用时全部业务不可达。因为它无状态，按进程数横向加即可，不需要额外方案。',
      why: '把限流放在最外层，避免每个服务各写一套计数逻辑（现在单体里就有三份）。',
    },
    auth: {
      name: 'Auth',
      role: '身份与会话',
      duty: '签发与校验令牌，向后传递调用者身份。不持有业务权限 —— 权限由各服务按自己的模型判定。',
      iface: ['POST /auth/token', 'GET /auth/verify'],
      up: ['gateway'],
      down: [],
      fail: '校验失败要快速失败（不重试），否则会把故障放大到所有服务。',
      why: '独立出来是因为它被所有服务依赖，且与业务发布节奏无关。',
    },
    order: {
      name: 'Order',
      role: '订单聚合根与状态机',
      duty: '持有订单状态与合法迁移集合。库存预占、支付结果都由它发起并收敛，但不由它直接改别人的数据。',
      iface: ['POST /orders', 'POST /orders/{id}/cancel', '消费 PaymentSucceeded'],
      up: ['gateway'],
      down: ['mysql', 'outbox', 'inventory'],
      fail: '写库失败即整体回滚；跨服务的失败由 Outbox 重试 + 幂等键兜住。',
      why: '按业务能力切，而不是按「controller / service / dao」切 —— 后者拆完还是要跨服务改一处逻辑。',
    },
    payment: {
      name: 'Payment',
      role: '支付与对账',
      duty: '对接渠道、记录流水、产出对账结果。对账口径不可变，是这个拆分里唯一不能妥协的约束。',
      iface: ['POST /payments/charge', 'POST /payments/refund', 'GET /reconcile/{day}'],
      up: ['gateway'],
      down: ['mysql', 'outbox'],
      fail: '渠道超时不能直接判失败，必须落到「处理中」并靠对账收敛。',
      why: '与订单的生命周期不同（支付流水是账务事实，订单是业务状态），混在一个模型里会导致对账口径被业务状态污染。',
    },
    inventory: {
      name: 'Inventory',
      role: '库存预占与释放',
      duty: '维护可售量，处理预占、确认、超时释放。释放由内部延迟队列负责。',
      iface: ['POST /inventory/reserve', 'POST /inventory/commit', 'POST /inventory/release'],
      up: ['order'],
      down: ['mysql', 'redis'],
      fail: '预占成功但确认失败时，靠超时释放兜底；这要求释放必须幂等。',
      why: '库存是并发争用最集中的地方，独立出来后可以单独压测与限流。',
    },
    mysql: {
      name: 'MySQL',
      role: '持久化，单实例',
      duty: '订单、支付、库存各占一组逻辑表。「共库不同表」，不跨服务直接 JOIN。',
      iface: ['read/write', 'binlog（供后续 CDC）'],
      up: ['order', 'payment', 'inventory'],
      down: [],
      fail: '单实例是当前最大的单点。先靠主从 + 定期演练恢复，不在这半年里上分库分表。',
      why: '保留现有库是硬约束（财务口径），所以把「拆」的口子留在表边界与访问路径上，而不是物理库。',
    },
    redis: {
      name: 'Redis',
      role: '缓存与限流计数',
      duty: '缓存热点订单与商品，承载全局限流计数。所有写入都可丢 —— 丢了只影响性能，不影响正确性。',
      iface: ['GET/SET', 'INCR（限流窗口）'],
      up: ['inventory', 'gateway'],
      down: [],
      fail: '不可用时降级为直连数据库，同时关闭限流而非放行无限流量。',
      why: '把「可丢的数据」集中在一处，能在故障时一刀切降级；混进业务库就做不到了。',
    },
    outbox: {
      name: 'Outbox',
      role: '跨服务一致性',
      duty: '每个服务把要发的事件写进自己的 outbox 表，与业务写在同一事务里；由轮询器投递并保证至少一次。',
      iface: ['poll → deliver', '幂等键 (event_id)'],
      up: ['order', 'payment'],
      down: ['mysql'],
      fail: '投递失败只重试，不阻塞业务；消费方必须按 event_id 去重。',
      why: '不引入 Kafka 是为了让 3 人团队少运维一个组件 —— 这是这次取舍里唯一「选简单」的地方。',
    },
  };

  var EDGES = [
    { from: 'gateway', to: 'order' },
    { from: 'gateway', to: 'payment' },
    { from: 'gateway', to: 'auth' },
    // 跳层依赖（接入层直连数据层）走左侧走廊绕行，
    // 否则连线会横穿业务层整行，把节点压住。
    { from: 'gateway', to: 'redis', skip: true },
    { from: 'order', to: 'inventory' },
    { from: 'order', to: 'mysql' },
    { from: 'order', to: 'outbox' },
    { from: 'payment', to: 'mysql' },
    { from: 'payment', to: 'outbox' },
    { from: 'inventory', to: 'mysql' },
    { from: 'inventory', to: 'redis' },
    { from: 'outbox', to: 'mysql' },
  ];

  /* ------------------------------------------------------------------ 工具 */

  var $ = function (sel, root) {
    return (root || document).querySelector(sel);
  };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------ 切换助手 */

  var current = 'architect';

  function switchAgent(id, options) {
    if (!AGENTS[id]) return;
    current = id;
    document.body.dataset.agent = id;

    // URL 反映状态：助手是可深链的，刷新/分享都能回到同一个视图
    if (!options || options.syncUrl !== false) {
      try {
        var url = new URL(window.location.href);
        url.searchParams.set('agent', id);
        window.history.replaceState(null, '', url);
      } catch (error) {
        /* file:// 下 history 可能受限，忽略 */
      }
    }

    var config = AGENTS[id];
    $('#roomName').textContent = config.room;
    $('#topTitle').textContent = config.title;
    $('#topMeta').innerHTML = config.meta;
    $('#newItemLabel').textContent = config.newItem;
    $('#contextFoot').textContent = config.foot;
    $('#prompt').placeholder = config.placeholder;
    $('#sendLabel').textContent = config.send;

    $$('.agent[data-agent-id]').forEach(function (btn) {
      if (btn.dataset.agentId === id) {
        btn.setAttribute('aria-current', 'true');
      } else {
        btn.removeAttribute('aria-current');
      }
    });

    $$('[data-list]').forEach(function (node) {
      node.hidden = node.dataset.list !== id;
    });
    $$('[data-panel]').forEach(function (node) {
      node.hidden = node.dataset.panel !== id;
    });

    renderSuggest(config.suggest);
    if (id === 'architect') {
      requestAnimationFrame(drawEdges);
    }
    document.title = 'Margin · ' + (id === 'review' ? '代码审查' : '架构设计') + '（原型）';
  }

  function renderSuggest(list) {
    var row = $('#suggestRow');
    row.textContent = '';
    row.appendChild(el('span', 'suggest__label', '可以这样问'));
    list.forEach(function (text) {
      var chip = el('button', 'suggest__chip', text);
      chip.type = 'button';
      chip.addEventListener('click', function () {
        var input = $('#prompt');
        input.value = text;
        input.focus();
      });
      row.appendChild(chip);
    });
  }

  /* --------------------------------------------------------- 架构图连线 */

  var svg = $('#bpEdges');
  var canvas = $('#bpCanvas');

  function anchor(nodeEl, which) {
    var box = nodeEl.getBoundingClientRect();
    var base = canvas.getBoundingClientRect();
    var left = box.left - base.left;
    var top = box.top - base.top;
    if (which === 'bottom') return { x: left + box.width / 2, y: top + box.height };
    if (which === 'top') return { x: left + box.width / 2, y: top };
    if (which === 'left') return { x: left, y: top + box.height / 2 };
    return { x: left + box.width, y: top + box.height / 2 };
  }

  function drawEdges() {
    if (!svg || !canvas) return;
    var active = $('.bp-node[aria-pressed="true"]');
    var activeId = active ? active.dataset.node : '';

    svg.textContent = '';
    svg.setAttribute('viewBox', '0 0 ' + canvas.clientWidth + ' ' + canvas.clientHeight);
    svg.setAttribute('width', canvas.clientWidth);
    svg.setAttribute('height', canvas.clientHeight);

    EDGES.forEach(function (edge) {
      var fromEl = $('.bp-node[data-node="' + edge.from + '"]');
      var toEl = $('.bp-node[data-node="' + edge.to + '"]');
      if (!fromEl || !toEl) return;

      var fromTop = fromEl.getBoundingClientRect().top;
      var toTop = toEl.getBoundingClientRect().top;
      var d;

      if (edge.skip) {
        // 跳层依赖：下 → 左进走廊 → 竖直下 → 右回目标。
        // 用直角走线（工程制图的惯例），一眼能和「跨层斜线」区分开。
        var s = anchor(fromEl, 'bottom');
        var t = anchor(toEl, 'top');
        var corridor = 18;
        d = 'M ' + s.x + ' ' + s.y +
            ' L ' + s.x + ' ' + (s.y + 14) +
            ' L ' + corridor + ' ' + (s.y + 14) +
            ' L ' + corridor + ' ' + (t.y - 14) +
            ' L ' + t.x + ' ' + (t.y - 14) +
            ' L ' + t.x + ' ' + t.y;
      } else if (Math.abs(fromTop - toTop) < 12) {
        // 同层依赖。先判断两者之间是否夹着别的节点：
        // 夹着就必须从节点行下方绕行，否则线会从中间那个节点身上穿过去
        // （探针实测 order → inventory 会压住 payment）。
        var lb = fromEl.getBoundingClientRect();
        var rb = toEl.getBoundingClientRect();
        var leftBox = lb.left < rb.left ? lb : rb;
        var rightBox = leftBox === lb ? rb : lb;
        var blocked = Object.keys(NODES).some(function (id) {
          var el = $('.bp-node[data-node="' + id + '"]');
          if (!el || el === fromEl || el === toEl) return false;
          var b = el.getBoundingClientRect();
          // 完全落在两者之间的横向区间内，且同一行
          return b.left > leftBox.right - 2 && b.right < rightBox.left + 2 &&
                 Math.abs(b.top - lb.top) < 12;
        });

        var la = anchor(leftBox === lb ? fromEl : toEl, 'bottom');
        var ra = anchor(rightBox === lb ? fromEl : toEl, 'bottom');
        if (blocked) {
          // 直角下绕：与跳层依赖同一套走线语言
          var drop = 18;
          d = 'M ' + la.x + ' ' + la.y +
              ' L ' + la.x + ' ' + (la.y + drop) +
              ' L ' + ra.x + ' ' + (ra.y + drop) +
              ' L ' + ra.x + ' ' + ra.y;
        } else {
          // 相邻：贴着一小段弧连过去，不占下方走廊
          var sa = anchor(fromEl, 'right');
          var sb = anchor(toEl, 'left');
          var midx = (sa.x + sb.x) / 2;
          d = 'M ' + sa.x + ' ' + sa.y +
              ' Q ' + midx + ' ' + (sa.y + 18) + ', ' + sb.x + ' ' + sb.y;
        }
      } else {
        // 跨层：竖直走一条贝塞尔，中点在两层的空隙里
        var top = anchor(fromEl, 'bottom');
        var bottom = anchor(toEl, 'top');
        var midY = (top.y + bottom.y) / 2;
        d = 'M ' + top.x + ' ' + top.y +
            ' C ' + top.x + ' ' + midY + ', ' + bottom.x + ' ' + midY +
            ', ' + bottom.x + ' ' + bottom.y;
      }

      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      path.setAttribute('class', edge.skip ? 'bp-edge is-skip' : 'bp-edge');
      if (activeId && (edge.from === activeId || edge.to === activeId)) {
        path.classList.add('is-active');
      }
      svg.appendChild(path);
    });
  }

  /* ------------------------------------------------------- 组件详情渲染 */

  var FIELDS = [
    ['职责', 'duty'],
    ['对外接口', 'iface'],
    ['依赖（下游）', 'down'],
    ['被谁依赖（上游）', 'up'],
    ['失败模式', 'fail'],
    ['为什么这样切', 'why'],
  ];

  function renderDetail(id) {
    var node = NODES[id];
    if (!node) return;
    $('#detailName').textContent = node.name;
    $('#detailRole').textContent = node.role;

    var body = $('#detailBody');
    body.textContent = '';

    FIELDS.forEach(function (pair) {
      var label = pair[0];
      var value = node[pair[1]];
      if (!value || (Array.isArray(value) && !value.length)) return;

      var wrap = el('div', 'field');
      wrap.appendChild(el('p', 'field__label', label));

      if (Array.isArray(value)) {
        var list = el('div', 'dep-list');
        value.forEach(function (item) {
          var isUp = pair[1] === 'up';
          var chip = el('span', 'dep' + (isUp ? ' dep--up' : ''), item);
          chip.setAttribute('translate', 'no');
          list.appendChild(chip);
        });
        wrap.appendChild(list);
      } else {
        wrap.appendChild(el('p', 'field__value', value));
      }
      body.appendChild(wrap);
    });
  }

  /* ------------------------------------------------------------- 事件绑定 */

  $$('.agent[data-agent-id]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      closeDrawer();
      switchAgent(btn.dataset.agentId);
    });
  });

  $$('.bp-node').forEach(function (node) {
    node.addEventListener('click', function () {
      $$('.bp-node').forEach(function (other) {
        other.setAttribute('aria-pressed', String(other === node));
      });
      renderDetail(node.dataset.node);
      drawEdges();
    });
  });

  $$('.filters .chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      $$('.filters .chip').forEach(function (other) {
        other.setAttribute('aria-pressed', String(other === chip));
      });
      var want = chip.dataset.sev;
      $$('#findingList .finding').forEach(function (row) {
        row.hidden = want !== 'all' && row.dataset.sev !== want;
      });
    });
  });

  /* 助手浮层 */
  var sheet = $('#sheet');
  var scrim = $('#scrim');

  function openSheet() {
    closeDrawer();
    sheet.hidden = false;
    scrim.hidden = false;
    $('#sheetClose').focus();
  }

  function closeSheet() {
    sheet.hidden = true;
    scrim.hidden = true;
    $('#addAgent').focus();
  }

  $('#addAgent').addEventListener('click', openSheet);
  $('#sheetClose').addEventListener('click', closeSheet);

  /* 窄屏抽屉：≤900px 时上下文栏是 fixed 抽屉，需要一个开关。
     遮罩与浮层共用，点遮罩时两边一起收掉。 */
  var context = $('.context');
  var drawerBtn = $('#drawerBtn');

  function drawerOpen() {
    return !!context && context.dataset.open === 'true';
  }

  function openDrawer() {
    // 只在开关真的可见时生效（宽屏下 context 是常驻栏，没有抽屉概念）。
    // 否则程序化触发会平白弹出一层遮罩盖住整个界面。
    if (!drawerBtn.offsetParent) return;
    context.dataset.open = 'true';
    drawerBtn.setAttribute('aria-expanded', 'true');
    scrim.hidden = false;
  }

  function closeDrawer() {
    if (!context || !drawerOpen()) return;
    context.dataset.open = 'false';
    drawerBtn.setAttribute('aria-expanded', 'false');
    if (sheet.hidden) scrim.hidden = true;
  }

  drawerBtn.addEventListener('click', function () {
    if (drawerOpen()) closeDrawer();
    else openDrawer();
  });

  // 点任务之后抽屉该收起来，否则挡住了刚打开的内容
  $$('.context .item').forEach(function (row) {
    row.addEventListener('click', closeDrawer);
  });

  scrim.addEventListener('click', function () {
    closeDrawer();
    if (!sheet.hidden) closeSheet();
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!sheet.hidden) closeSheet();
    else closeDrawer();
  });

  $$('[data-pick]').forEach(function (card) {
    card.addEventListener('click', function () {
      switchAgent(card.dataset.pick);
      closeSheet();
    });
  });

  /* 重绘连线：窗口尺寸变化、字体加载完成都会改变节点位置 */
  var redrawTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(redrawTimer);
    redrawTimer = setTimeout(drawEdges, 120);
  });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      drawEdges();
    });
  }

  /* -------------------------------------------------------------- 初始化 */

  var initial = 'architect';
  try {
    var wanted = new URL(window.location.href).searchParams.get('agent');
    if (wanted && AGENTS[wanted]) initial = wanted;
  } catch (error) {
    /* 忽略 */
  }

  switchAgent(initial);
  renderDetail('order');
  requestAnimationFrame(drawEdges);
  window.addEventListener('load', function () {
    drawEdges();
  });
})();
