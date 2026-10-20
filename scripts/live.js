/* ==========================================================================
   Margin · 实时模式
   页面由 backend/harness/server.py 托管时接管工作台：
   空间/会话/审计事件流全部来自真实后端。

   只在 http(s) 且后端可达时启用；否则 tryBoot 返回 false，
   由 app.js 渲染「未连接」面板。界面不降级到假数据：一份看起来像真结果的
   审查，比一句「没连上」更容易误导人。

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
  var live = false;           // tryBoot 是否成功接管（app.js 据此决定走哪条分支）

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
    ui.setPlaceholder((ui.dialect ? ui.dialect().placeholder : '') || '描述这次任务，回车开始…');

    ui.onModelChange(function (id) {
      modelOverride = id;
    });
    ui.onSessionSelect(function (space, session) {
      openSession(session);
    });

    /* 点击空间行但该空间下还没有记录：清掉正文，只把顶栏切过去。
       否则用户点了别的仓库，看到的还是上一个仓库的旧内容。 */
    ui.onSpaceSelect(function (space) {
      showEmptyWorkspace(space);
    });

    /* 在某个仓库下开新审查：清正文 + 清空范围 chip，等用户输入 */
    ui.onDraft(function (space) {
      showEmptyWorkspace(space);
      ui.clearScope();
      ui.setStatus('待开始', space.name);
    });

    /* 回形针里加入/移出仓库后，侧栏要立刻反映出来 */
    /* 工作空间按助手隔离：候选、加入、移除、浏览都带上当前助手 id。
       服务端据此分发 —— 审查看靶场、架构看案例，两边的空间列表互不干扰。 */
    ui.setWorkspaceProvider({
      list: function () {
        var agent = agentScope();
        return Promise.all([api.candidates(agent), api.repos(agent)]).then(function (results) {
          return {
            candidates: (results[0] && results[0].candidates) || [],
            repos: (results[1] && results[1].repos) || [],
          };
        });
      },
      add: function (path) {
        return api.addRepo(path, agentScope());
      },
      browse: function (path) {
        return api.fsList(path, agentScope());
      },
      remove: function (path) {
        return api.removeRepo(path, agentScope());
      },
      files: function (repoPath) {
        return api.repoFiles(repoPath);
      },
    });

    ui.onWorkspaceChange(function (addedPath) {
      // 传了路径说明是「刚加入这个仓库」——refresh 完要选中它
      selectAfterRefresh = addedPath || '';
      refresh();
    });

    ui.setLiveHandlers({
      send: function (text) {
        return startAudit(text);
      },
      stop: function () {
        requestStop();
      },
    });

    live = true;
    await refresh();
    return true;
  }

  /* 切换助手：另一位助手有另一套历史，正文与侧栏都得重来。
     少了这一步就会出现「助手已经切成架构了，正文里还挂着审查的发现」。 */
  async function refocus() {
    if (!live) {
      return;
    }
    /* 正在来回不了头的流要断开，否则它会继续往新助手的正文里写内容 */
    if (source) {
      source.close();
      source = null;
    }
    if (ui.isBusy()) {
      ui.setBusy(false);
      setConnection('live', '切换助手');
    }
    openRunId = null;
    currentRunId = null;
    view = null;
    await refresh();
  }

  function isLive() {
    return live;
  }

  /* 切换到一个还没有运行记录的仓库时的空态 */
  function showEmptyWorkspace(space) {
    var template =
      (ui.dialect && ui.dialect().empty) ||
      '还没有记录。在下面写明目标并回车，就会为 %s 新建一次任务。';
    var wrap = el('div', 'turn turn--agent');
    wrap.appendChild(el('p', 'run__intro', template.replace('%s', space.name)));

    var note = el('p', 'run__note');
    note.textContent =
      '只跑某几个文件时，用输入框左侧的筛选按钮勾选范围；' +
      '要把别的仓库加进工作区，用回形针按钮。';
    wrap.appendChild(note);

    ui.replaceThread(wrap);
  }

  /* 顶栏连接指示：一眼看出当前数据是真实的还是没连上 */
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
        demo: '未连接',
        busy: '运行中',
        error: '连接异常',
      };
      text.textContent = labels[state] || state;
      node.title = detail ? labels[state] + ' · ' + detail : labels[state] || '';
    }
  }

  /* 拉取仓库、历史、模型与配置，重建侧栏与模型菜单 */
  /* 名单属于哪个助手。它既是请求参数，也是「为什么这一栏只有一半历史」的答案，
     所以只在这里算一次，refresh 与 refreshRailOnly 共用同一份。 */
  function agentScope() {
    return currentAgentId();
  }

  /* 列表取不到时必须让用户看见。吞掉它就变成「这个助手没有历史」——
     和「真的没跑过」长得一模一样，用户无从分辨。 */
  function guardListing() {
    for (var i = 0; i < arguments.length; i += 1) {
      var payload = arguments[i];
      if (payload && payload.error) {
        notice('读取列表失败：' + payload.error);
        if (window.console && console.warn) {
          console.warn('[margin] 列表接口报错：', payload);
        }
        return;
      }
    }
  }

  async function refresh() {
    var results = await Promise.all([
      api.repos(agentScope()),
      api.runs(null, agentScope()),
      api.models(),
      api.config(),
    ]);
    guardListing(results[0], results[1]);
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

    /* 选中项的处理顺序（这决定了「切换仓库后顶栏有没有变」）：
       1. 刚加入工作区的那个仓库 → 直接选中它，让用户看到加入的结果；
       2. 当前的选中项还在 → 保留，不要因为一次 refresh 就把用户甩回第一个；
       3. 否则退回第一个仓库（即使它没有历史记录，也要把顶栏填上）。 */
    var active = ui.getActive();
    var wanted =
      pickScope(spaces, selectAfterRefresh) ||
      pickScope(spaces, active.space && (active.space.path || active.space.name)) ||
      spaces[0] ||
      null;

    selectAfterRefresh = '';

    if (!wanted) {
      return;
    }
    // 选中项被移出工作区之后，原来是第几条就已经不重要了
    var target = wanted.sessions[0] || null;
    ui.applyContext(wanted, target);
    if (target) {
      openSession(target);
    } else {
      showEmptyWorkspace(wanted);
    }
  }

  function pickScope(spaces, key) {
    if (!key) {
      return null;
    }
    var found = null;
    spaces.forEach(function (space) {
      if (space.path === key || space.id === key || space.name === key) {
        found = space;
      }
    });
    return found;
  }

  function sessionTitle(run) {
    var goal = (run.goal || '').trim();
    if (goal.length > 22) {
      goal = goal.slice(0, 22) + '…';
    }
    var mark = { running: '运行中', queued: '排队中', error: '失败', stopped: '已停止' }[run.status];
    /* 不再给标题加「设计 · 」前缀：会话列表已经按助手隔离了，
       同一个助手名下不可能混进另一种产出，前缀只是噪音。 */
    return mark ? mark + ' · ' + (goal || '未命名') : goal || '未命名任务';
  }

  /* ==========================================================================
     会话打开：回放已归档的事件流
     ========================================================================== */

  var replaying = false;
  /* 加入工作区之后要在 refresh 完成时自动选中的仓库路径 */
  var selectAfterRefresh = '';

  async function openSession(session, silent) {
    if (replaying || (ui.isBusy() && !silent)) {
      return;
    }
    if (!session || !session.runId) {
      return;
    }
    /* 幂等：同一条会话已经在正文里了就不再重来。这只防重复，
       不代表「追加」—— 正文永远只属于当前选中的那一条会话。 */
    if (session.runId === openRunId) {
      return;
    }

    var snapshot = await api.snapshot(session.runId, { events: true });
    if (!snapshot || snapshot.error || !snapshot.events) {
      return;
    }

    replaying = true;
    try {
      /* 正文先清空，再渲染这一条会话。少了这一步，
         点第二个任务就会把它的输出接在第一个任务下面 —— 看起来像
         「所有任务共用一个对话框」，其实是会话之间压根没有边界。 */
      ui.replaceThread(null);
      openRunId = session.runId;
      currentRunId = session.runId;

      var meta = {
        repo_name: snapshot.repo_name,
        model: snapshot.model,
        goal: snapshot.goal,
        offline: snapshot.offline,
        agent: snapshot.agent,
        outcome: outcomeFor(snapshot),
      };

      /* 还在跑：SSE 会先补齐历史事件再实时推，所以「接着看」和
         从头回放走的是同一条路径，不需要在这里再渲染一遍快照里的事件。 */
      if (snapshot.status === 'running') {
        var liveHandle = createView(meta);
        liveHandle.runId = session.runId;
        streamRun(session.runId, liveHandle);
        setConnection('busy', snapshot.repo_name);
        return;
      }

      var handle = createView(Object.assign({ replay: true }, meta));
      handle.runId = session.runId;
      snapshot.events.forEach(function (event) {
        applyEvent(event.type, event.data, handle, true);
      });
      finishView(handle, snapshot.result || {}, true);
      ui.setBusy(false);
      ui.setStatus('运行完成', plural(handle.steps, '轮'));
    } finally {
      replaying = false;
    }
  }

  /* 已经渲染在会话流里的运行 id（防止重复回放同一份） */
  var openRunId = null;

  /* ==========================================================================
     发起一次真实审计
     ========================================================================== */

  function currentAgentId() {
    return ui.agentId ? ui.agentId() : 'review';
  }

  function currentOutcome() {
    return window.MARGIN_AGENTS ? window.MARGIN_AGENTS.outcome() : 'findings';
  }

  async function startAudit(text) {
    var active = ui.getActive();
    var space = active && active.space;
    var dialect = ui.dialect ? ui.dialect() : {};

    /* 新建一次任务 = 进入一条新会话：正文必须清空。
       否则这次运行的输出会接在上一条会话的输出后面，用户会以为
       「问了新的一句话，AI 却还在回答上一个任务」。 */
    ui.replaceThread(null);
    openRunId = null;
    ui.renderUserTurn(text);
    ui.markSent();

    if (!space || !space.path) {
      notice(dialect.noRepo || '请先在左侧选择一个仓库，再描述任务目标。');
      return;
    }

    var model = ui.currentModel();
    var scope = (ui.getScope && ui.getScope()) || [];
    var payload = {
      repo: space.path,
      goal: text,
      model: model ? model.id : undefined,
      max_iterations: 40,
      /* 用哪个助手跑由后端校验：未知 id 会直接 400，绝不静默回退成审查 */
      agent: currentAgentId(),
    };
    if (scope.length) {
      payload.scope = scope;
    }

    /* 增量审查：只审 BASE..工作树。后端拿不到 diff 会直接 400（不静默退化成
       全仓审查），所以这里要把它当成用户可见的错误处理，而不是忽略。 */
    if (ui.isDiff && ui.isDiff()) {
      payload.diff = 'HEAD';
    }

    var handle = createView({
      repo_name: space.name,
      model: model ? model.id : '',
      goal: text,
      offline: false,
      agent: currentAgentId(),
      outcome: currentOutcome(),
    });

    if (scope.length) {
      handle.note('本次任务限定 ' + scope.length + ' 个路径：' + scope.join('、'));
    }

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
      /* diff 相关的失败要单独说清：这是用户主动打开的模式，
         不是后端随便报了个错。 */
      if (response && (response.code === 'diff_unavailable' || response.code === 'diff_empty')) {
        message = message + '（增量模式要求仓库是 git 仓库，且 BASE 到工作树之间确实有改动）';
      }
      handle.note('启动失败：' + message);
      ui.setBusy(false);
      setConnection('error', message);
      return;
    }

    currentRunId = response.run_id;
    handle.runId = response.run_id;
    // 这次运行已经占了正文：再点侧栏里它自己时应当是 no-op，而不是重新回放
    openRunId = response.run_id;
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
      artifact: function (data) {
        handle.addArtifact(data);
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
    var results = await Promise.all([api.repos(agentScope()), api.runs(null, agentScope())]);
    guardListing(results[0], results[1]);
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
    /* 同一个输入坞，不同助手说不同的话：审查 / 设计。
       走 outcomeFor 而不是只看 meta.outcome —— run 事件里的元数据未必
       带 outcome（老归档只有 agent），少了这一步回放旧记录会显示「开始审查」。 */
    var verb = outcomeFor(meta) === 'blueprint' ? '设计' : '审查';
    return '已连接后端，开始' + verb + ' ' + bits.join(' · ') + '。';
  }

  /* 这次运行用哪套产出渲染器。终态里带了 outcome 就以它为准，
     否则按 agent id 查清单 —— 历史记录是旧版本跑的，没有 outcome 字段。 */
  function outcomeFor(snapshot) {
    if (snapshot && snapshot.result && snapshot.result.outcome) {
      return snapshot.result.outcome;
    }
    var id = snapshot && snapshot.agent;
    if (id && window.MARGIN_AGENTS) {
      var found = null;
      window.MARGIN_AGENTS.list().forEach(function (item) {
        if (item.id === id) {
          found = item;
        }
      });
      if (found) {
        return found.outcome;
      }
    }
    return 'findings';
  }

  function createView(meta) {
    var turn = el('article', 'turn turn--agent');
    var intro = el('p', 'run__intro is-animated', introFor(meta));
    turn.appendChild(intro);

    // 计划面板的标题跟着**这一次运行**的产出形态走，不是当前选中的助手 ——
    // 否则切到审查去看一条旧的架构记录，面板会写「审查计划」。
    var plan = ui.buildPlan([], meta && meta.outcome);
    plan.total.textContent = '0 轮';
    turn.appendChild(plan.root);

    var tools = el('div', 'tools');
    turn.appendChild(tools);

    /* 产出插槽：固定插在工具区之后。产出是逐条推来的，提示也可能插进来，
       有了这个槽，产出区的位置就不会因为「谁先到」而变。 */
    var slot = el('div', 'run__outcome');
    turn.appendChild(slot);

    var noticeBox = null;
    var findingsSection = null;
    var findingsList = null;
    var findings = [];
    /* 已渲染过的发现 id。后端对同一条发现会发两种事件 —— `finding` 与
       `artifact{kind:'finding'}`（前者给审查视图，后者给通用产出通道），
       两条都进 addFinding 就会渲染成两张卡、表头计数也翻倍。
       实测：5 条发现显示成 10 张卡、表头写「全部 10」。 */
    var seenFindings = {};
    var summaryNode = null;
    var statsNode = null;
    /* 非 findings 形态的产出（蓝图 / 通用）由这一层渲染 */
    var artifactView = null;

    var handle = {
      meta: meta,
      prefix: null,
      steps: 0,
      begin: Date.now(),
      open: null,
      turn: turn,
      outcome: (meta && meta.outcome) || 'findings',
      artifactView: null,
      slot: slot,

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
        if (!finding) {
          return;
        }
        // 按 id 去重：同一发现可能经 finding 与 artifact 两条事件到达
        var key = finding.id || finding.fingerprint;
        if (key) {
          if (seenFindings[key]) {
            return;
          }
          seenFindings[key] = true;
        }
        if (!findingsSection) {
          var section = ui.buildFindings([]);
          findingsSection = section.root;
          findingsList = section.list;
          slot.appendChild(findingsSection);
        }
        findings.push(finding);
        var card = ui.buildFinding(finding);
        card.classList.add('is-animated');
        findingsList.appendChild(card);
        ui.refreshFindingsHead(findingsSection, findings);
        ui.follow();
      },

      /* 产出的统一入口：kind 决定去哪。
         finding 走发现卡片（带严重级别筛选），其余交给该助手的渲染器。 */
      addArtifact: function (data) {
        if (!data || !data.kind) {
          return;
        }
        // 事件体是 { kind, artifact, iteration }；产出本体在 artifact 里。
        // 直接把整个事件丢给渲染器，画出来的会是「kind/iteration」两个字段。
        var item = data.artifact || data;
        if (data.kind === 'finding') {
          handle.addFinding(item);
          return;
        }
        if (!artifactView) {
          artifactView = ui.createOutcome(handle.outcome);
          if (!artifactView) {
            return;
          }
          handle.artifactView = artifactView;
          slot.appendChild(artifactView.root);
        }
        artifactView.add(data.kind, item);
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

    /* 终态里带着完整产出。两件事靠它兜底：
       ① 事件流缺尾（服务中途重启）→ 补齐；
       ② 老归档只有 finding 事件、但这次是蓝图 → 至少把产出显示出来。 */
    if (payload.artifacts && handle.outcome !== 'findings' && handle.slot) {
      var kinds = Object.keys(payload.artifacts || {});
      var hasNonFinding = kinds.some(function (kind) {
        return kind !== 'finding';
      });
      if (hasNonFinding && !handle.artifactView) {
        var created = ui.createOutcome(handle.outcome);
        if (created) {
          handle.artifactView = created;
          handle.slot.appendChild(created.root);
        }
      }
    }
    if (handle.artifactView && payload.artifacts) {
      handle.artifactView.fill(payload.artifacts);
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

    /* 证据核验面板：只有架构助手会产生这个报告。
       放在摘要之后、统计之前 —— 它回答的是「这份方案有多少说法站得住」，
       比 token 与耗时更该被先看到。 */
    var evidence = buildEvidence(payload.evidence_report);
    if (evidence) {
      turn.appendChild(evidence);
    }

    if (!replay) {
      turn.appendChild(buildStats(handle, payload));
      turn.appendChild(buildFollowUps(handle));
    } else if (handle.runId) {
      turn.appendChild(buildStats(handle, payload, handle.runId));
    }
  }

  /* ==========================================================================
     证据核验（后端 evidence_report）
     --------------------------------------------------------------------------
     后端逐条核验「这条产出凭什么说自己存在」，输出 ok / weak / bad 三档计数。
     这里把它画成一条可读的进度条 + 分类型明细 + 问题清单。
     没有报告（审查助手、或旧归档）就返回 null，不占位置。
     ========================================================================== */

  var KIND_LABEL = {
    layer: '分层',
    component: '组件',
    decision: '决策',
    open_question: '开放问题',
  };

  function buildEvidence(report) {
    if (!report || !report.total_refs) {
      return null;
    }

    var box = el('section', 'evidence is-animated');
    box.setAttribute('aria-label', '证据核验');

    var head = el('header', 'evidence__head');
    head.appendChild(el('h3', 'evidence__title', '证据核验'));

    var rate = el('p', 'evidence__rate');
    rate.appendChild(document.createTextNode(String(report.grounded_rate)));
    var pct = el('small', null, '%');
    rate.appendChild(pct);
    head.appendChild(rate);
    box.appendChild(head);

    /* 进度条：ok 用强调色，weak 用同色淡化，bad 用阻断色。
       三者加起来正好是 100%，所以宽度直接按比例给。 */
    var total = report.total_refs || 1;
    var bar = el('div', 'evidence__bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute(
      'aria-label',
      '可精确核实 ' + report.ok + ' 处，不够精确 ' + report.weak + ' 处，无法核实 ' + report.bad + ' 处'
    );
    [
      [report.ok, ''],
      [report.weak, 'is-weak'],
      [report.bad, 'is-bad'],
    ].forEach(function (pair) {
      if (!pair[0]) {
        return;
      }
      var seg = el('i', pair[1]);
      seg.style.width = (pair[0] / total) * 100 + '%';
      bar.appendChild(seg);
    });
    box.appendChild(bar);

    var legend = el('div', 'evidence__legend');
    [
      ['可精确核实', report.ok],
      ['不够精确', report.weak],
      ['无法核实', report.bad],
      ['缺证据条目', report.items_without_evidence || 0],
    ].forEach(function (pair) {
      var item = el('span');
      item.appendChild(document.createTextNode(pair[0]));
      item.appendChild(el('b', null, String(pair[1])));
      legend.appendChild(item);
    });
    box.appendChild(legend);

    /* 分类型明细 */
    var kinds = report.per_kind || {};
    var names = Object.keys(kinds);
    if (names.length) {
      var grid = el('dl', 'evidence__kinds');
      names.forEach(function (name) {
        var stat = kinds[name];
        var cell = el('div', 'evidence__kind');
        cell.appendChild(el('dt', null, KIND_LABEL[name] || name));
        var value = stat.total
          ? stat.ok + ' / ' + stat.total
          : '缺 ' + stat.no_evidence + ' 条';
        cell.appendChild(el('dd', null, value));
        grid.appendChild(cell);
      });
      box.appendChild(grid);
    }

    /* 问题清单：只列 bad 与缺证据，weak 不逐条列（它只是「不够精确」） */
    var problems = (report.problems || []).filter(function (item) {
      return item.status === 'bad';
    });
    if (problems.length) {
      var list = el('div', 'evidence__problems');
      problems.slice(0, 6).forEach(function (item) {
        var row = el('div', 'evidence__problem');
        row.appendChild(el('b', null, (KIND_LABEL[item.kind] || item.kind) + '：' + (item.name || '未命名')));
        var ref = el('code', null, item.evidence);
        ref.setAttribute('translate', 'no');
        row.appendChild(ref);
        list.appendChild(row);
      });
      box.appendChild(list);
    } else if (report.items_without_evidence) {
      box.appendChild(
        el(
          'p',
          'evidence__none',
          '有 ' + report.items_without_evidence + ' 条产出没有登记证据 —— 它们可能成立，但评审无法直接核对。'
        )
      );
    }

    return box;
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
      case 'artifact':
        handle.addArtifact(data);
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
    refocus: refocus,
    isLive: isLive,
  };
})();
