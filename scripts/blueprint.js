/* ==========================================================================
   Margin · 架构产出渲染器（outcome = blueprint）

   审查的产出是「发现列表」，架构的产出是「分层图 + 组件 + 决策 + 开放问题」。
   两者没有公共形状，所以这里是**第二套渲染器**，不是发现卡片换层皮：

     ┌ 层名（左侧栏位） │ 组件节点 ────────────┐
     │                 │  [A] → [B] → [C]     │   ← 连线在 SVG 覆盖层里
     └─────────────────┴──────────────────────┘
     决策记录（选了什么 / 放弃了什么 / 代价）
     开放问题（agent 主动暴露的不确定）

   三条刻意的约束：

   1. **数据是逐条推来的**（模型边想边记），所以每个 add* 之后都整体重排一次。
      组件数量级是十几个，重排代价可以忽略；换来的是不必维护两套布局代码。
   2. **组件按名字匹配依赖**。模型的 depends_on 写的是组件名，不是 id；
      匹配不上就**不画线** —— 画一条猜出来的依赖，比不画更糟（错的图比没有图更糟）。
   3. **跨层依赖用虚线**，同层用实线。线型本身携带信息，图下方有图例说明。
   ========================================================================== */

(function () {
  'use strict';

  var seq = 0;

  /* 没有登记层的组件放在最后这一组 ——
     这是「模型漏记了层」这一真实状态的如实呈现，不是一个编造的层名。 */
  var UNLAYERED = '未分层';

  function norm(name) {
    return String(name || '').trim().toLowerCase();
  }

  /* 活跃视图注册表。Observer 强引用 canvas，视图被丢弃（切会话/切助手/清正文）时
     必须 disconnect，否则整棵蓝图树都无法回收 —— 实时模式每回放一条架构会话
     就泄漏一份。集中在这里，消费方（app.js 的 replaceThread）只需调 destroyAll。 */
  var views = new Set();

  function create(tools) {
    var el = tools.el;          // ui.el
    var esc = tools.esc;        // ui.esc
    var uid = 'bp' + (seq += 1);

    var layers = [];
    var components = [];
    var decisions = [];
    var questions = [];
    var nameMap = {};
    var seen = {};
    var selectedId = null;

    var root = el('section', 'bp is-animated');
    root.setAttribute('aria-label', '架构方案');

    /* ---- 表头：计数跟着产出涨，与发现列表一个规矩 ---- */
    var head = el('header', 'bp__head');
    root.appendChild(head);

    /* ---- 图 + 详情 ---- */
    var body = el('div', 'bp__body');
    var canvasWrap = el('div', 'bp__canvas-wrap');
    var canvas = el('div', 'bp__canvas');
    var wires = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    wires.setAttribute('class', 'bp__wires');
    wires.setAttribute('aria-hidden', 'true');
    wires.setAttribute('preserveAspectRatio', 'none');
    canvas.appendChild(wires);
    canvasWrap.appendChild(canvas);

    var legend = el('p', 'bp__legend');
    var solid = el('span', 'bp__legend-item');
    solid.appendChild(el('i', 'bp__legend-line'));
    solid.appendChild(el('span', null, '同层依赖'));
    var dashed = el('span', 'bp__legend-item');
    dashed.appendChild(el('i', 'bp__legend-line bp__legend-line--jump'));
    dashed.appendChild(el('span', null, '跨层依赖'));
    legend.appendChild(solid);
    legend.appendChild(dashed);
    canvasWrap.appendChild(legend);

    var detail = el('aside', 'bp__detail');
    detail.setAttribute('aria-label', '组件详情');
    body.appendChild(canvasWrap);
    body.appendChild(detail);
    root.appendChild(body);

    /* ---- 决策与开放问题 ---- */
    var decisionBox = el('section', 'bp__section bp__decisions');
    decisionBox.hidden = true;
    root.appendChild(decisionBox);
    var questionBox = el('section', 'bp__section bp__questions');
    questionBox.hidden = true;
    root.appendChild(questionBox);

    /* ======================================================================
       表头
       ====================================================================== */

    function renderHead() {
      head.textContent = '';
      head.appendChild(el('h3', 'bp__title', '架构方案'));

      var bits = [];
      if (layers.length) {
        bits.push(layers.length + ' 层');
      }
      if (components.length) {
        bits.push(components.length + ' 个组件');
      }
      if (decisions.length) {
        bits.push(decisions.length + ' 条决策');
      }
      var blocking = questions.filter(function (item) {
        return item.blocking;
      }).length;
      if (questions.length) {
        bits.push(questions.length + ' 个开放问题' + (blocking ? '（' + blocking + ' 阻塞）' : ''));
      }
      if (bits.length) {
        head.appendChild(el('p', 'bp__count num', bits.join(' · ')));
      }
    }

    /* ======================================================================
       分层图
       ====================================================================== */

    function sortedLayers() {
      return layers.slice().sort(function (a, b) {
        return (a.order || 0) - (b.order || 0);
      });
    }

    function layerNames() {
      var names = sortedLayers().map(function (layer) {
        return layer.name;
      });
      // 有组件落在没登记过的层里时，把那些层也补进来（保序：按首个组件出现的位置）
      components.forEach(function (comp) {
        var name = comp.layer || UNLAYERED;
        if (names.indexOf(name) === -1) {
          names.push(name);
        }
      });
      return names;
    }

    function layerInfo(name) {
      var found = null;
      layers.forEach(function (layer) {
        if (layer.name === name) {
          found = layer;
        }
      });
      return found;
    }

    function renderCanvas() {
      canvas.querySelectorAll('.bp-layer').forEach(function (node) {
        node.remove();
      });

      var names = layerNames();

      if (!components.length && !names.length) {
        var empty = el('p', 'bp__empty', '架构助手还没有登记分层与组件。');
        canvas.appendChild(empty);
        return;
      }

      names.forEach(function (name, index) {
        var info = layerInfo(name);
        var group = components.filter(function (comp) {
          return (comp.layer || UNLAYERED) === name;
        });
        if (info && !group.length) {
          // 层登记了但还没放组件：照样画出来，它标的是边界，不是内容
        }

        var row = el('div', 'bp-layer');
        row.dataset.layer = name;
        row.dataset.index = String(index);

        var side = el('div', 'bp-layer__side');
        var label = el('span', 'bp-layer__name', name);
        label.setAttribute('translate', 'no');
        side.appendChild(label);
        if (info && info.purpose) {
          side.appendChild(el('span', 'bp-layer__purpose', info.purpose));
        }
        if (info && info.hint) {
          side.appendChild(el('span', 'bp-layer__hint', info.hint));
        }
        row.appendChild(side);

        var nodes = el('div', 'bp-layer__nodes');
        if (!group.length) {
          nodes.appendChild(el('span', 'bp-layer__void', '本层暂无组件'));
        }
        group.forEach(function (comp) {
          var node = el('button', 'bp-node');
          node.type = 'button';
          node.dataset.id = comp.id;
          node.setAttribute('aria-pressed', String(comp.id === selectedId));
          if (comp.id === selectedId) {
            node.classList.add('is-selected');
          }
          var nodeName = el('span', 'bp-node__name', comp.name);
          nodeName.setAttribute('translate', 'no');
          node.appendChild(nodeName);
          if (comp.role) {
            node.appendChild(el('span', 'bp-node__role', comp.role));
          }
          node.addEventListener('click', function () {
            select(comp.id);
          });
          nodes.appendChild(node);
        });
        row.appendChild(nodes);
        canvas.appendChild(row);
      });
    }

    /* ======================================================================
       连线
       ====================================================================== */

    function box(node) {
      var cRect = canvas.getBoundingClientRect();
      var nRect = node.getBoundingClientRect();
      return {
        left: nRect.left - cRect.left,
        right: nRect.right - cRect.left,
        top: nRect.top - cRect.top,
        bottom: nRect.bottom - cRect.top,
        cx: nRect.left - cRect.left + nRect.width / 2,
        cy: nRect.top - cRect.top + nRect.height / 2,
      };
    }

    function nodeEl(id) {
      return canvas.querySelector('.bp-node[data-id="' + id + '"]');
    }

    function path(d, className, from, to) {
      var line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      line.setAttribute('d', d);
      line.setAttribute('class', className);
      line.setAttribute('marker-end', 'url(#' + uid + '-arrow)');
      line.dataset.from = from;
      line.dataset.to = to;
      if (selectedId && (from === selectedId || to === selectedId)) {
        line.classList.add('is-active');
      } else if (selectedId) {
        line.classList.add('is-dim');
      }
      wires.appendChild(line);
    }

    /* ======================================================================
       连线几何（踩过坑，改法都有实测依据）

       旧画法三个问题，用户截图里都出现了：
       ① 走廊写死 min(90, width*0.12)，画布一窄就小于层名栏位宽（84px），
          竖线直接压进层名文字；横段也跟着伸进栏位。
       ② 同层浅弧用固定下沉量，会从换行后第二行的节点身上穿过去。
       ③ 跨层的定长 14px 竖段会伸出层外，箭头看着指向别处。

       新画法的原则：**每一段都走在「本来就空着的地方」** ——
       横段走节点行之间的 gap 中线（flex gap 8px，取 +4 上下各有 4px 净空），
       竖段走层名栏位与节点区之间的缝（宽度从 DOM 实测，不写死）。
       ====================================================================== */

    /* 该节点所在层的层名栏位宽度：走廊 = 栏位宽 + 缝。
       写死 90 或按画布宽度比例算，窄画布下都会小于栏位宽。 */
    function corridorX() {
      var side = canvas.querySelector('.bp-layer__side');
      var w = side ? side.getBoundingClientRect().width : 84;
      return w + 6;
    }

    function drawWires() {
      wires.textContent = '';

      var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      var marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
      marker.setAttribute('id', uid + '-arrow');
      marker.setAttribute('viewBox', '0 0 8 8');
      marker.setAttribute('refX', 6.5);
      marker.setAttribute('refY', 4);
      marker.setAttribute('markerWidth', 7);
      marker.setAttribute('markerHeight', 7);
      marker.setAttribute('orient', 'auto-start-reverse');
      var tip = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      tip.setAttribute('d', 'M1 1.4 L6.6 4 L1 6.6');
      tip.setAttribute('class', 'bp__tip');
      marker.appendChild(tip);
      defs.appendChild(marker);
      wires.appendChild(defs);

      if (!components.length) {
        return;
      }

      var width = canvas.clientWidth;
      var height = canvas.clientHeight;
      wires.setAttribute('viewBox', '0 0 ' + Math.max(width, 1) + ' ' + Math.max(height, 1));
      wires.setAttribute('width', String(Math.max(width, 1)));
      wires.setAttribute('height', String(Math.max(height, 1)));

      var corridor = corridorX();

      components.forEach(function (comp) {
        (comp.depends_on || []).forEach(function (depName) {
          var from = nameMap[norm(depName)];
          if (!from || from.id === comp.id) {
            return; // 匹配不上就不画：猜出来的依赖比没有依赖更有害
          }
          var a = nodeEl(from.id);
          var b = nodeEl(comp.id);
          if (!a || !b) {
            return;
          }
          var ra = box(a);
          var rb = box(b);
          var sameLayer = (from.layer || UNLAYERED) === (comp.layer || UNLAYERED);
          /* 同一行：纵向差 < 2px。同层不同行（换行）不能走短 U，
             竖段会从第二行的节点身上穿过去。 */
          var sameRow = Math.abs(ra.top - rb.top) < 2;
          /* 同一行里两点之间还夹着别的节点时也不能走短 U：横段会穿它。 */
          var blocked = false;
          if (sameLayer && sameRow) {
            var parent = a.parentNode;
            if (parent) {
              var lo = Math.min(ra.left, rb.left);
              var hi = Math.max(ra.right, rb.right);
              Array.prototype.forEach.call(parent.querySelectorAll('.bp-node'), function (n) {
                if (n === a || n === b) {
                  return;
                }
                var r = box(n);
                if (r.left > lo && r.right < hi) {
                  blocked = true;
                }
              });
            }
          }
          var jump = !(sameLayer && sameRow) || blocked;
          var downward = rb.top > ra.top;

          if (!jump) {
            /* 同行同层：从两点下方 gap 的中线绕一行，短 U 形。
               gap 是 flex 的 8px，取 +4 保证上下各 4px 净空。 */
            var y = Math.max(ra.bottom, rb.bottom) + 4;
            path(
              'M' + ra.cx + ' ' + ra.bottom +
                ' V' + y +
                ' H' + rb.cx +
                ' V' + rb.bottom,
              'bp-wire bp-wire--same',
              from.id,
              comp.id
            );
            return;
          }

          /* 跨层（或同层不同行）：横段贴在本节点的上/下空隙里，
             竖段走走廊。永远不出层、不压层名、不穿节点。 */
          var y1 = downward ? ra.bottom + 4 : ra.top - 4;
          var y2 = downward ? rb.top - 4 : rb.bottom + 4;
          var startY = downward ? ra.bottom : ra.top;
          var endY = downward ? rb.top : rb.bottom;
          path(
            'M' + ra.cx + ' ' + startY +
              ' V' + y1 +
              ' H' + corridor +
              ' V' + y2 +
              ' H' + rb.cx +
              ' V' + endY,
            'bp-wire ' + (sameLayer ? 'bp-wire--same' : 'bp-wire--jump'),
            from.id,
            comp.id
          );
        });
      });
    }

    /* ======================================================================
       组件详情
       ====================================================================== */

    function fact(label, node) {
      var row = el('div', 'bp-fact');
      row.appendChild(el('dt', null, label));
      var dd = el('dd');
      dd.appendChild(node);
      row.appendChild(dd);
      return row;
    }

    function listNode(items, className) {
      var ul = el('ul', className || 'bp-list');
      items.forEach(function (text) {
        ul.appendChild(el('li', null, text));
      });
      return ul;
    }

    function renderDetail() {
      detail.textContent = '';

      if (!components.length) {
        detail.appendChild(el('p', 'bp__detail-empty', '组件登记后会显示在这里。'));
        return;
      }

      var comp = null;
      components.forEach(function (item) {
        if (item.id === selectedId) {
          comp = item;
        }
      });
      if (!comp) {
        comp = components[0];
      }

      var headBox = el('div', 'bp__detail-head');
      var layerTag = el('span', 'bp__detail-layer', comp.layer || UNLAYERED);
      headBox.appendChild(layerTag);
      var name = el('h4', 'bp__detail-name', comp.name);
      name.setAttribute('translate', 'no');
      headBox.appendChild(name);
      if (comp.role) {
        headBox.appendChild(el('p', 'bp__detail-role', comp.role));
      }
      detail.appendChild(headBox);

      var dl = el('dl', 'bp__facts');

      if (comp.responsibility) {
        dl.appendChild(fact('职责', el('p', null, comp.responsibility)));
      }
      if ((comp.interfaces || []).length) {
        dl.appendChild(fact('对外接口', listNode(comp.interfaces)));
      }
      if ((comp.depends_on || []).length) {
        var deps = el('div', 'bp-chips');
        comp.depends_on.forEach(function (depName) {
          var target = nameMap[norm(depName)];
          var chip = el('button', 'bp-chip');
          chip.type = 'button';
          chip.appendChild(el('span', null, depName));
          if (target) {
            chip.addEventListener('click', function () {
              select(target.id);
            });
          } else {
            // 依赖写了一个不存在的组件名：如实标出来，不要假装它存在
            chip.classList.add('is-unknown');
            chip.title = '方案里没有名为「' + depName + '」的组件';
          }
          deps.appendChild(chip);
        });
        dl.appendChild(fact('依赖', deps));
      }
      if ((comp.failure_modes || []).length) {
        dl.appendChild(fact('会怎么失败', listNode(comp.failure_modes, 'bp-list bp-list--warn')));
      }
      if (comp.rationale) {
        dl.appendChild(fact('为什么这样切', el('p', null, comp.rationale)));
      }

      if (!dl.childElementCount) {
        dl.appendChild(el('p', 'bp__detail-empty', '这个组件只登记了名字。'));
      }
      detail.appendChild(dl);
    }

    /* ======================================================================
       决策记录 / 开放问题
       ====================================================================== */

    function renderDecisions() {
      decisionBox.hidden = !decisions.length;
      decisionBox.textContent = '';
      if (!decisions.length) {
        return;
      }
      decisionBox.appendChild(el('h3', 'bp__section-title', '关键决策'));
      var list = el('div', 'bp__cards');
      decisions.forEach(function (item, index) {
        var card = el('article', 'bp-card is-animated');
        var headRow = el('header', 'bp-card__head');
        headRow.appendChild(el('span', 'bp-card__no num', String(index + 1)));
        headRow.appendChild(el('h4', 'bp-card__title', item.title));
        card.appendChild(headRow);

        if (item.decision) {
          card.appendChild(el('p', 'bp-card__line', item.decision));
        }
        if ((item.alternatives || []).length) {
          var box = el('div', 'bp-card__alt');
          box.appendChild(el('span', 'bp-card__label', '放弃的方案'));
          box.appendChild(listNode(item.alternatives, 'bp-list bp-list--muted'));
          card.appendChild(box);
        }
        if (item.rationale) {
          card.appendChild(el('p', 'bp-card__line bp-card__line--why', item.rationale));
        }
        if (item.consequences) {
          var cost = el('p', 'bp-card__cost');
          cost.appendChild(el('span', 'bp-card__label', '代价'));
          cost.appendChild(el('span', null, item.consequences));
          card.appendChild(cost);
        }
        list.appendChild(card);
      });
      decisionBox.appendChild(list);
    }

    function renderQuestions() {
      questionBox.hidden = !questions.length;
      questionBox.textContent = '';
      if (!questions.length) {
        return;
      }
      var blocking = questions.filter(function (item) {
        return item.blocking;
      }).length;
      var titleRow = el('div', 'bp__section-head');
      titleRow.appendChild(el('h3', 'bp__section-title', '开放问题'));
      if (blocking) {
        var mark = el('span', 'bp__blocking num', blocking + ' 项阻塞');
        mark.setAttribute('role', 'status');
        titleRow.appendChild(mark);
      }
      questionBox.appendChild(titleRow);

      var list = el('ul', 'bp__qlist');
      questions.forEach(function (item) {
        var li = el('li', 'bp-q');
        if (item.blocking) {
          li.classList.add('is-blocking');
        }
        var mark = el('span', 'bp-q__mark', item.blocking ? '阻塞' : '待定');
        li.appendChild(mark);
        var text = el('div', 'bp-q__text');
        text.appendChild(el('p', 'bp-q__q', item.question));
        if (item.why_it_matters) {
          text.appendChild(el('p', 'bp-q__why', item.why_it_matters));
        }
        li.appendChild(text);
        list.appendChild(li);
      });
      questionBox.appendChild(list);
    }

    /* ======================================================================
       重排 / 选中
       ====================================================================== */

    var queued = false;
    function relayout() {
      if (queued) {
        return;
      }
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        renderHead();
        renderCanvas();
        renderDetail();
        renderDecisions();
        renderQuestions();
        drawWires();
        applySelection();
      });
    }

    function applySelection() {
      canvas.querySelectorAll('.bp-node').forEach(function (node) {
        var on = node.dataset.id === selectedId;
        node.classList.toggle('is-selected', on);
        node.setAttribute('aria-pressed', String(on));
      });
    }

    function select(id) {
      selectedId = id;
      relayout();
    }

    /* 画布尺寸变了连线就全错，所以必须跟着重画 */
    var ro = null;
    if (typeof window.ResizeObserver === 'function') {
      ro = new window.ResizeObserver(function () {
        drawWires();
      });
      ro.observe(canvas);
    } else {
      window.addEventListener('resize', drawWires);
    }

    var handle = {
      root: root,
      outcome: 'blueprint',

      /* 视图被丢弃时调用（切会话、切助手、replaceThread）。
         不 disconnect 的话，Observer 会一直强引用已脱离文档的 canvas ——
         连同整棵蓝图树一起无法回收；实时模式每回放一条架构会话就泄漏一份。 */
      destroy: function () {
        if (ro) {
          ro.disconnect();
          ro = null;
        } else {
          window.removeEventListener('resize', drawWires);
        }
        views.delete(handle);
      },

      /* 四种产出各有自己的入口：kind 决定去哪个桶。
         按 id 去重 —— 终态补齐（fill）与边跑边推的事件可能给同一条产出两次。 */
      add: function (kind, data) {
        var key = kind + ':' + (data.id || data.fingerprint || JSON.stringify(data));
        if (seen[key]) {
          return false;
        }
        seen[key] = true;
        if (kind === 'layer') {
          layers.push(data);
        } else if (kind === 'component') {
          components.push(data);
          nameMap[norm(data.name)] = data;
          if (!selectedId) {
            selectedId = data.id;
          }
        } else if (kind === 'decision') {
          decisions.push(data);
        } else if (kind === 'open_question') {
          questions.push(data);
        } else {
          return false;
        }
        relayout();
        return true;
      },

      counts: function () {
        return {
          layer: layers.length,
          component: components.length,
          decision: decisions.length,
          open_question: questions.length,
        };
      },

      /* 回放结束用：终态里若带了完整产出，直接补齐（事件流可能缺 tail） */
      fill: function (artifacts) {
        if (!artifacts) {
          return;
        }
        Object.keys(artifacts).forEach(function (kind) {
          (artifacts[kind] || []).forEach(function (data) {
            handle.add(kind, data);
          });
        });
      },

      redraw: function () {
        drawWires();
      },
    };

    views.add(handle);
    return handle;
  }

  /* ==========================================================================
     兜底渲染器：给前端「不认识」的 outcome 用

     可插拔的诚实程度就体现在这里 —— 后端注册一个新 agent，它的产出类型前端
     没写过专用渲染器时，必须还能把内容显示出来（按字段原样列），
     而不是白屏或者假装它是发现列表。
     ========================================================================== */

  var SKIP_FIELDS = { id: 1, fingerprint: 1 };

  function createGeneric(tools) {
    var el = tools.el;

    var root = el('section', 'bp bp--generic is-animated');
    var head = el('header', 'bp__head');
    root.appendChild(head);
    var groups = {};
    var order = [];
    var seen = {};

    function renderHead(total) {
      head.textContent = '';
      head.appendChild(el('h3', 'bp__title', '产出'));
      head.appendChild(el('p', 'bp__count num', total + ' 条'));
    }

    var handle = {
      root: root,
      outcome: 'generic',
      add: function (kind, data) {
        // 与蓝图同一套去重：终态补齐会和逐条推来的事件给同一条产出两次
        var key = kind + ':' + (data.id || data.fingerprint || JSON.stringify(data));
        if (seen[key]) {
          return false;
        }
        seen[key] = true;
        if (!groups[kind]) {
          groups[kind] = [];
          order.push(kind);
        }
        groups[kind].push(data);

        var box = root.querySelector('[data-kind="' + kind + '"]');
        if (!box) {
          box = el('section', 'bp__section bp__generic-group');
          box.dataset.kind = kind;
          var title = el('h3', 'bp__section-title', kind);
          title.setAttribute('translate', 'no');
          box.appendChild(title);
          root.appendChild(box);
        }

        var card = el('article', 'bp-card is-animated');
        var dl = el('dl', 'bp__facts');
        var filled = false;
        Object.keys(data).forEach(function (key) {
          if (SKIP_FIELDS[key] || data[key] == null || data[key] === '') {
            return;
          }
          var value = data[key];
          if (Array.isArray(value)) {
            if (!value.length) {
              return;
            }
            var ul = el('ul', 'bp-list');
            value.forEach(function (item) {
              ul.appendChild(el('li', null, String(item)));
            });
            value = ul;
          } else if (typeof value === 'object') {
            value = el('code', null, JSON.stringify(value));
          } else if (typeof value === 'boolean') {
            value = el('span', null, value ? '是' : '否');
          } else {
            value = el('span', null, String(value));
          }
          var row = el('div', 'bp-fact');
          row.appendChild(el('dt', null, key));
          var dd = el('dd');
          dd.appendChild(value);
          row.appendChild(dd);
          dl.appendChild(row);
          filled = true;
        });
        if (!filled) {
          dl.appendChild(el('p', 'bp__detail-empty', '（这条产出没有可显示的字段）'));
        }
        card.appendChild(dl);
        box.appendChild(card);

        var total = 0;
        order.forEach(function (name) {
          total += groups[name].length;
        });
        renderHead(total);
        return true;
      },
      counts: function () {
        var out = {};
        order.forEach(function (name) {
          out[name] = groups[name].length;
        });
        return out;
      },
      fill: function (artifacts) {
        var self = this;
        Object.keys(artifacts || {}).forEach(function (kind) {
          (artifacts[kind] || []).forEach(function (data) {
            self.add(kind, data);
          });
        });
      },
      redraw: function () {},
    };

    views.add(handle);
    return handle;
  }

  window.MARGIN_BLUEPRINT = {
    create: create,
    createGeneric: createGeneric,
    /* 丢弃全部活跃视图（Observer / resize 监听一并释放）。 */
    destroyAll: function () {
      Array.from(views).forEach(function (view) {
        if (view && typeof view.destroy === 'function') {
          view.destroy();
        }
      });
    },
  };
})();
