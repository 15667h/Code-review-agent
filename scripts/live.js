/* ==========================================================================
   Margin · 实时模式
   页面由 backend/harness/server.py 托管时接管工作台：
   空间/会话/审计事件流全部来自真实后端。

   只在 http(s) 且后端可达时启用；否则 tryBoot 返回 false，
   由 app.js 继续走演示数据 —— 双击 index.html 看原型的路径不受影响。

   SSE 事件到界面的映射：
     run / prefix  → 运行头部（仓库、模型、静态前缀指纹）
     step          → 计划面板里加一轮
     tool          → 工具卡片
     finding       → 发现卡片（边录边推，表头计数同步刷新）
     retry/compression → 运行提示
     done / error  → 收尾统计与报告链接
   ========================================================================== */

(function () {
  'use strict';

  var api = window.MARGIN_API;
  var ui = null;

  var source = null;          // 当前 EventSource
  var currentRunId = null;
  var view = null;            // 当前运行的视图句柄
  var modelOverride = null;   // 用户在模型菜单里选的模型

  /* ==========================================================================
     小工具
     ========================================================================== */

  function el(tag, className, text) {
    return ui.el(tag, className, text);
  }

  function pad(value) {
    return value < 10 ? '0' + value : String(value);
  }

  function timeLabel(seconds) {
    if (!seconds) {
      return '';
    }
    var date = new Date(seconds * 1000);
    var now = new Date();
    var sameDay =
      date.getFullYear() === now.getFullYear() &&
      date.getMonth() === now.getMonth() &&
      date.getDate() === now.getDate();
    if (sameDay) {
      return '今天 ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
    }
    return date.getMonth() + 1 + '月' + date.getDate() + '日';
  }

  function formatDuration(seconds) {
    if (seconds == null) {
      return '';
    }
    if (seconds < 60) {
      return seconds.toFixed(1) + 's';
    }
    return Math.floor(seconds / 60) + 'm' + pad(Math.round(seconds % 60)) + 's';
  }

  function plural(count, unit) {
    return count + ' ' + unit;
  }

  /* 结束原因是后端的枚举值，界面上一律显示中文 —— 不要把一个英文单词
     孤立地丢在一排中文里。 */
  var STOP_REASON_LABEL = {
    finished: '已完成',
    stopped: '已停止',
    max_iterations: '达轮次上限',
    final_answer: '已给出结论',
    model_error: '模型调用失败',
    protocol_error: '编排错误',
    error: '运行失败',
  };

  function stopReasonLabel(value) {
    if (!value) {
      return '—';
    }
    return STOP_REASON_LABEL[value] || value;
  }

  /* ==========================================================================
     启动
     ========================================================================== */

  async function tryBoot(uiRef) {
    ui = uiRef;

    // file:// 下不启用：跨域 + Chrome 私有网络访问（PNA）会拦截，徒增控制台噪音
    if (!api || !api.served) {
      return false;
    }

    var health = await api.health();
    if (!health || !health.ok) {
      return false;
    }

    setConnection('live', health.model || '已连接');
    ui.setPlaceholder('描述这次审查的重点，回车开始真实审计…');

    ui.onModelChange(function (id) {
      modelOverride = id;
    });
    ui.onSessionSelect(function (space, session) {
      openSession(session);
    });
    ui.setLiveHandlers({
      send: function (text) {
        return startAudit(text);
      },
      stop: function () {
        requestStop();
      },
    });

    await refresh();
    return true;
  }

  /* 顶栏连接指示：一眼看出当前数据是真实的还是演示的 */
  function setConnection(state, detail) {
    var node = document.getElementById('conn');
    var text = document.getElementById('connText');
    if (node) {
      node.dataset.state = state;
      node.hidden = false;
    }
    if (text) {
      var labels = {
        live: '后端已连接',
        demo: '演示数据',
        busy: '运行中',
        error: '连接异常',
      };
      text.textContent = labels[state] || state;
      node.title = detail ? labels[state] + ' · ' + detail : labels[state] || '';
    }
  }

  /* 拉取仓库、历史、模型与配置，重建侧栏与模型菜单 */
  async function refresh() {
    var results = await Promise.all([api.repos(), api.runs(), api.models(), api.config()]);
    var repos = (results[0] && results[0].repos) || [];
    var runs = (results[1] && results[1].runs) || [];
    var models = (results[2] && results[2].models) || [];
    var config = results[3] || {};

    var byRepo = {};
    runs.forEach(function (run) {
      (byRepo[run.repo] = byRepo[run.repo] || []).push(run);
    });

    var spaces = repos.map(function (repo, index) {
      var sessions = (byRepo[repo.path] || []).map(function (run) {
        return {
          id: run.run_id,
          runId: run.run_id,
          title: sessionTitle(run),
          time: timeLabel(run.created_at),
          status: run.status,
          goal: run.goal,
          current: false,
        };
      });
      return {
        id: repo.id,
        owner: '',
        name: repo.name,
        path: repo.path,
        files: repo.files,
        open: index === 0,
        sessions: sessions,
      };
    });

    ui.setSpaces(spaces);

    /* 模型菜单：真实模型列表 + 当前配置里的默认项 */
    var menu = models.map(function (model) {
      var bits = [];
      if (model.context_window) {
        bits.push(Math.round(model.context_window / 100000) / 10 + 'M 上下文');
      }
      if ((model.input_modalities || []).indexOf('image') !== -1) {
        bits.push('支持图像');
      }
      if (model.effort_default) {
        bits.push('推理强度 ' + model.effort_default);
      }
      return {
        id: model.id,
        name: model.name || model.id,
        desc: bits.join(' · ') || '—',
        current: model.id === (modelOverride || config.model),
      };
    });

    if (!menu.length) {
      menu = [
        {
          id: config.model || 'deepseek-flash',
          name: config.model || 'deepseek-flash',
          desc: config.ready ? '已配置密钥' : '未配置密钥，只能离线运行',
          current: true,
        },
      ];
    }
    ui.setModels(menu);
    ui.renderModels();
    ui.renderRail();

    /* 默认选中最近一次运行：顶栏上下文完全由数据驱动，
       不选的话顶栏只会是空的（HTML 里刻意没有静态默认值）。 */
    var first = spaces[0];
    if (first) {
      var target = first.sessions[0] || null;
      ui.applyContext(first, target);
      if (target) {
        openSession(target);
      }
    }
  }

  function sessionTitle(run) {
    var goal = (run.goal || '').trim();
    if (goal.length > 22) {
      goal = goal.slice(0, 22) + '…';
    }
    var mark = { running: '运行中', queued: '排队中', error: '失败', stopped: '已停止' }[run.status];
    return mark ? mark + ' · ' + (goal || '未命名') : goal || '未命名审查';
  }

  /* ==========================================================================
     会话打开：回放已归档的事件流
     ========================================================================== */

  var replaying = false;

  async function openSession(session, silent) {
    if (replaying || (ui.isBusy() && !silent)) {
      return;
    }
    if (!session || !session.runId || session.status === 'running') {
      return;
    }
    // 已经展示过这一次就不再重复追加，避免点两次出现两份相同内容
    if (session.runId === openRunId) {
      return;
    }

    var snapshot = await api.snapshot(session.runId, { events: true });
    if (!snapshot || snapshot.error || !snapshot.events) {
      return;
    }

    replaying = true;
    try {
      var handle = createView({
        repo_name: snapshot.repo_name,
        model: snapshot.model,
        goal: snapshot.goal,
        offline: snapshot.offline,
        replay: true,
      });
      handle.runId = session.runId;
      snapshot.events.forEach(function (event) {
        applyEvent(event.type, event.data, handle, true);
      });
      finishView(handle, snapshot.result || {}, true);
      openRunId = session.runId;
      currentRunId = session.runId;
      ui.setBusy(false);
      ui.setStatus('审查完成', plural(handle.steps, '轮'));
    } finally {
      replaying = false;
    }
  }

  /* 已经渲染在会话流里的运行 id（防止重复回放同一份） */
  var openRunId = null;

  /* ==========================================================================
     发起一次真实审计
     ========================================================================== */

  async function startAudit(text) {
    var active = ui.getActive();
    var space = active && active.space;

    ui.renderUserTurn(text);
    ui.markSent();

    if (!space || !space.path) {
      notice('请先在左侧选择一个仓库，再描述审查重点。');
      return;
    }

    var model = ui.currentModel();
    var payload = {
      repo: space.path,
      goal: text,
      model: model ? model.id : undefined,
      max_iterations: 40,
    };

    var handle = createView({
      repo_name: space.name,
      model: model ? model.id : '',
      goal: text,
      offline: false,
    });

    ui.setBusy(true);
    setConnection('busy', space.name);

    var response = await api.startAudit(payload);

    /* 没有密钥时不要只说「失败」——直接降级到离线模式并把这件事说清楚 */
    if (response && response.code === 'no_api_key') {
      handle.note('后端未配置 API Key，已自动改用离线规则模式；结论只覆盖规则能表达的模式。');
      response = await api.startAudit(Object.assign({}, payload, { offline: true }));
      if (response && response.run_id) {
        handle.offline = true;
      }
    }

    if (!response || !response.run_id) {
      var message = (response && response.error) || '启动审计失败';
      handle.note('启动失败：' + message);
      ui.setBusy(false);
      setConnection('error', message);
      return;
    }

    currentRunId = response.run_id;
    handle.runId = response.run_id;
    streamRun(response.run_id, handle);
  }

  function requestStop() {
    if (!currentRunId) {
      return;
    }
    api.stop(currentRunId);
    if (view) {
      view.note('已请求停止，会在本轮结束时停下。');
    }
    ui.setStatus('停止中…', '');
  }

  /* ==========================================================================
     SSE 消费
     ========================================================================== */

  function streamRun(runId, handle) {
    if (source) {
      source.close();
      source = null;
    }

    var closed = false;
    function close() {
      if (!closed) {
        closed = true;
        if (source) {
          source.close();
          source = null;
        }
      }
    }

    source = api.stream(runId, {
      run: function (data) {
        handle.meta = data;
        handle.setIntro(introFor(data));
      },
      prefix: function (data) {
        handle.prefix = data;
      },
      step: function (data) {
        handle.addStep(data);
      },
      tool: function (data) {
        handle.addTool(data);
      },
      finding: function (data) {
        handle.addFinding(data);
      },
      retry: function (data) {
        handle.note('模型重试：' + (data.error || ''));
      },
      compression: function (data) {
        handle.note('触发了上下文压缩。');
      },
      stopping: function () {
        handle.note('已请求停止，会在本轮结束时停下。');
      },
      done: function (data) {
        handle.setResult(data);
        finishView(handle, data, false);
        close();
        ui.setBusy(false);
        setConnection('live', handle.meta ? handle.meta.repo_name : '');
        refreshRailOnly();
      },
      error: function (data) {
        handle.note('运行失败：' + (data.message || '未知错误'));
        finishView(handle, { stop_reason: 'error' }, false);
        close();
        ui.setBusy(false);
        setConnection('error', data.message || '');
      },
      close: function () {
        close();
        if (ui.isBusy()) {
          ui.setBusy(false);
        }
      },
      connection_error: function (eventSource) {
        // EventSource 会自动重连；只有终态时才真正收工
        if (eventSource.readyState === 2) {
          close();
          if (ui.isBusy()) {
            ui.setBusy(false);
          }
        }
      },
    });
  }

  /* 运行结束后只刷新侧栏，不重放（否则会把刚跑完的流覆盖掉） */
  async function refreshRailOnly() {
    var results = await Promise.all([api.repos(), api.runs()]);
    var repos = (results[0] && results[0].repos) || [];
    var runs = (results[1] && results[1].runs) || [];
    var byRepo = {};
    runs.forEach(function (run) {
      (byRepo[run.repo] = byRepo[run.repo] || []).push(run);
    });
    var spaces = repos.map(function (repo, index) {
      return {
        id: repo.id,
        owner: '',
        name: repo.name,
        path: repo.path,
        files: repo.files,
        open: index === 0,
        sessions: (byRepo[repo.path] || []).map(function (run) {
          return {
            id: run.run_id,
            runId: run.run_id,
            title: sessionTitle(run),
            time: timeLabel(run.created_at),
            status: run.status,
            goal: run.goal,
            current: run.run_id === currentRunId,
          };
        }),
      };
    });
    ui.setSpaces(spaces);
    ui.renderRail();

    /* 重绘侧栏会把选中态清掉，所以按当前运行重新应用一次上下文 */
    if (currentRunId) {
      spaces.forEach(function (space) {
        space.sessions.forEach(function (session) {
          if (session.runId === currentRunId) {
            ui.applyContext(space, session);
          }
        });
      });
    }
  }

  /* ==========================================================================
     运行视图
     ========================================================================== */

  function introFor(meta) {
    var bits = [];
    if (meta.repo_name) {
      bits.push(meta.repo_name);
    }
    bits.push(meta.offline ? '离线规则模式' : meta.model || '模型未指定');
    return '已连接后端，开始审查 ' + bits.join(' · ') + '。';
  }

  function createView(meta) {
    var turn = el('article', 'turn turn--agent');
    var intro = el('p', 'run__intro is-animated', introFor(meta));
    turn.appendChild(intro);

    var plan = ui.buildPlan([]);
    plan.total.textContent = '0 轮';
    turn.appendChild(plan.root);

    var tools = el('div', 'tools');
    turn.appendChild(tools);

    var noticeBox = null;
    var findingsSection = null;
    var findingsList = null;
    var findings = [];
    var summaryNode = null;
    var statsNode = null;

    var handle = {
      meta: meta,
      prefix: null,
      steps: 0,
      begin: Date.now(),
      open: null,
      turn: turn,

      setIntro: function (text) {
        intro.textContent = text;
        ui.follow();
      },

      addStep: function (data) {
        // 上一轮收尾：用相邻两次 step 的墙钟差作为这一轮耗时
        if (handle.open) {
          var elapsed = (Date.now() - handle.open.at) / 1000;
          ui.setStepState(handle.open.item, 'done', formatDuration(elapsed));
        }
        var item = ui.makeStep({
          label: '第 ' + data.iteration + ' 轮 · ' + (data.label || '无工具调用'),
          meta: data.meta || '',
        });
        plan.list.appendChild(item.el);
        handle.steps += 1;
        plan.total.textContent = plural(handle.steps, '轮');
        handle.open = { item: item, at: Date.now() };
        ui.setStepState(item, 'active');
        // 进度只看本次运行的计划面板，不能把历史轮次也数进去
        ui.setStatus('执行计划…', data.iteration + ' / ' + plan.list.querySelectorAll('.step').length);
        ui.follow();
      },

      addTool: function (data) {
        var card = ui.buildTool({
          name: data.name,
          arg: data.arg || '',
          meta: data.meta || '',
          duration: data.duration != null ? formatDuration(data.duration) : '',
          output: data.output || '',
        });
        card.classList.add('is-animated');
        if (data.ok === false) {
          card.classList.add('is-failed');
        }
        tools.appendChild(card);
        ui.follow();
      },

      addFinding: function (finding) {
        if (!findingsSection) {
          var section = ui.buildFindings([]);
          findingsSection = section.root;
          findingsList = section.list;
          // 插在工具区之后、结语之前
          turn.appendChild(findingsSection);
        }
        findings.push(finding);
        var card = ui.buildFinding(finding);
        card.classList.add('is-animated');
        findingsList.appendChild(card);
        ui.refreshFindingsHead(findingsSection, findings);
        ui.follow();
      },

      note: function (text) {
        if (!noticeBox) {
          noticeBox = el('p', 'run__note');
          turn.appendChild(noticeBox);
        }
        // 同一条提示不重复追加。
        // 「停止」就是踩过的例子：前端点击时先提示一次给即时反馈，
        // 服务端的 stopping 事件到达后会再提示一次，不去重就会显示两遍。
        if (noticeBox.textContent.indexOf(text) !== -1) {
          return;
        }
        noticeBox.textContent = noticeBox.textContent
          ? noticeBox.textContent + ' ' + text
          : text;
        ui.follow();
      },

      setResult: function (payload) {
        handle.result = payload;
      },
    };

    handle.open = null;
    view = handle;
    ui.thread.appendChild(turn);
    ui.follow(true);
    return handle;
  }

  function finishView(handle, payload, replay) {
    // 收尾：把最后一轮标成完成
    if (handle.open) {
      var elapsed = ((replay ? handle.begin : Date.now()) - handle.open.at) / 1000;
      ui.setStepState(
        handle.open.item,
        payload.stop_reason === 'stopped' ? 'pending' : 'done',
        replay ? '' : formatDuration(elapsed)
      );
      handle.open = null;
    }

    var turn = handle.turn;
    if (!turn) {
      return;
    }

    if (payload.stop_reason === 'stopped') {
      var stopped = el('p', 'run__note', '已停止。上面是停止前已完成的部分。');
      turn.appendChild(stopped);
    }

    if (payload.summary) {
      turn.appendChild(el('p', 'run__summary is-animated', payload.summary));
    }

    if (payload.warnings && payload.warnings.length) {
      var warn = el('div', 'run__warn');
      var body = payload.warnings
        .slice(0, 3)
        .map(function (line) {
          return line.length > 160 ? line.slice(0, 160) + '…' : line;
        })
        .join('\n');
      warn.appendChild(el('p', 'run__warn-title', '运行提示（' + payload.warnings.length + ' 条）'));
      warn.appendChild(el('pre', 'run__warn-body', body));
      turn.appendChild(warn);
    }

    if (!replay) {
      turn.appendChild(buildStats(handle, payload));
      turn.appendChild(buildFollowUps(handle));
    } else if (handle.runId) {
      turn.appendChild(buildStats(handle, payload, handle.runId));
    }
  }

  function buildStats(handle, payload, replayRunId) {
    var usage = payload.usage || {};
    var stats = el('dl', 'run__stats');

    function fact(label, value) {
      var row = el('div', 'stat');
      row.appendChild(el('dt', null, label));
      row.appendChild(el('dd', 'num', value));
      stats.appendChild(row);
    }

    fact('轮数', String(payload.iterations != null ? payload.iterations : handle.steps));
    fact('耗时', formatDuration(payload.elapsed != null ? payload.elapsed : (Date.now() - handle.begin) / 1000));
    fact('结束原因', stopReasonLabel(payload.stop_reason));
    if (usage.prompt_tokens) {
      fact('tokens', usage.prompt_tokens + ' / ' + usage.completion_tokens);
    }
    if (payload.cache_hit_ratio) {
      fact('缓存命中', (payload.cache_hit_ratio * 100).toFixed(1) + '%');
    }
    if (handle.prefix && handle.prefix.fingerprint) {
      fact('前缀指纹', handle.prefix.fingerprint);
    }

    var runId = replayRunId || handle.runId;
    if (runId) {
      var link = el('a', 'run__report');
      link.href = api.reportUrl(runId);
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = '查看完整报告';
      stats.appendChild(link);
    }
    return stats;
  }

  function buildFollowUps(handle) {
    var wrap = el('div', 'followups is-animated');
    wrap.setAttribute('aria-label', '继续');

    var blockers = (handle.result && handle.result.counts && handle.result.counts.blocker) || 0;
    if (blockers) {
      var chip = el('button', 'followup', '只看阻断项（' + blockers + '）');
      chip.type = 'button';
      chip.addEventListener('click', function () {
        var section = handle.turn.querySelector('.findings');
        if (!section) {
          return;
        }
        section.querySelectorAll('.sev-chip').forEach(function (node) {
          if (node.dataset.sev === 'blocker') {
            node.click();
          }
        });
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      wrap.appendChild(chip);
    }

    var again = el('button', 'followup', '再跑一次');
    again.type = 'button';
    again.addEventListener('click', function () {
      if (ui.isBusy()) {
        return;
      }
      startAudit((handle.meta && handle.meta.goal) || '再审查一次');
    });
    wrap.appendChild(again);

    return wrap;
  }

  function notice(text) {
    var turn = el('article', 'turn turn--agent');
    turn.appendChild(el('p', 'run__note', text));
    ui.thread.appendChild(turn);
    ui.follow(true);
  }

  /* ==========================================================================
     回放用的事件分发（与实时路径共用同一个 handle）
     ========================================================================== */

  function applyEvent(type, data, handle, replay) {
    switch (type) {
      case 'run':
        handle.meta = data;
        handle.setIntro(introFor(data));
        break;
      case 'prefix':
        handle.prefix = data;
        break;
      case 'step':
        handle.addStep(data);
        break;
      case 'tool':
        handle.addTool(data);
        break;
      case 'finding':
        handle.addFinding(data);
        break;
      case 'done':
        handle.setResult(data);
        break;
      default:
        break;
    }
  }

  window.MARGIN_LIVE = {
    tryBoot: tryBoot,
    setConnection: setConnection,
  };
})();
