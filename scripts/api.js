/* ==========================================================================
   Margin · 后端接口客户端
   只负责与 harness HTTP 服务通信，不碰 DOM。
   接口契约与 backend/harness/server.py 一一对应。

   一个重要约束：页面用 file:// 直接打开时不启用实时模式。
   原因有两个，都不是洁癖：
   1. file:// 的来源是 null，请求 localhost 属于跨域；
   2. Chrome 的私有网络访问（PNA）策略会拦截「公网页面 → 本机服务」，
      而 file:// 也在拦截范围内。
   与其让用户看到一堆控制台报错，不如直接走演示数据，并在界面上说清楚。
   ========================================================================== */

(function () {
  'use strict';

  var origin = window.location.origin;
  var served = window.location.protocol === 'http:' || window.location.protocol === 'https:';

  function url(path) {
    return served ? origin + path : path;
  }

  /* 只拼有值的项 —— 值为空的键不能出现在请求里，否则服务端会以为
     调用方真的在要「某个空 id」的记录。 */
  function qs(params) {
    var bits = [];
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') {
        return;
      }
      bits.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    });
    return bits.length ? '?' + bits.join('&') : '';
  }

  function jsonFetch(path, options) {
    var opts = options || {};
    return fetch(url(path), {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store',
    }).then(function (response) {
      return response.text().then(function (text) {
        var payload = null;
        try {
          payload = text ? JSON.parse(text) : null;
        } catch (error) {
          payload = { error: '响应不是合法 JSON：' + text.slice(0, 200) };
        }
        if (!response.ok) {
          payload = payload || {};
          if (!payload.error) {
            payload.error = 'HTTP ' + response.status;
          }
          payload.status = response.status;

          /* 服务端开启了访问控制（MARGIN_REQUIRE_AUTH=1）而我们没登录：
             直接送去登录页，别让界面拿一堆 401 空数据渲染出一个「什么都为空」的
             假象 —— 那种空看起来像「没有记录」，其实是没有权限。 */
          if (response.status === 401 && payload.code === 'unauthenticated') {
            var here = window.location.pathname.split('/').pop() || 'index.html';
            if (here !== 'login.html' && here !== 'register.html') {
              window.location.href = 'login.html';
            }
          }
        }
        return payload;
      });
    });
  }

  var MARGIN_API = {
    served: served,
    base: served ? origin : '',

    /* ---- 账号 ---- */
    /* 会话靠 HttpOnly Cookie 维持，脚本读不到也不需要读；
       fetch 默认 credentials: 'same-origin'，同源请求会自动带上。 */

    me: function () {
      return jsonFetch('/api/auth/me');
    },

    login: function (email, password) {
      return jsonFetch('/api/auth/login', {
        method: 'POST',
        body: { email: email, password: password },
      });
    },

    register: function (email, name, password) {
      return jsonFetch('/api/auth/register', {
        method: 'POST',
        body: { email: email, name: name, password: password },
      });
    },

    logout: function () {
      return jsonFetch('/api/auth/logout', { method: 'POST', body: {} });
    },

    /* ---- 服务与配置 ---- */

    /* Agent 清单。取不到返回 null（而不是空数组）：调用方要区分
       「后端没有这个接口」和「后端确实一个 agent 都没有」——
       前者要退回内置清单，后者是后端坏了，该报错。 */
    agents: function () {
      return jsonFetch('/api/agents').catch(function () {
        return null;
      });
    },

    health: function () {
      if (!served) {
        return Promise.resolve(null);
      }
      return jsonFetch('/api/health').catch(function () {
        return null;
      });
    },

    config: function () {
      return jsonFetch('/api/config');
    },

    saveConfig: function (patch) {
      return jsonFetch('/api/config', { method: 'POST', body: patch });
    },

    testConfig: function (patch) {
      return jsonFetch('/api/config/test', { method: 'POST', body: patch || {} });
    },

    models: function (force) {
      return jsonFetch('/api/models' + (force ? '?force=1' : '')).catch(function (error) {
        return { ok: false, models: [], error: String(error) };
      });
    },

    /* ---- 仓库与历史 ---- */

    /* agent：把名单限定到某一个助手。不传 = 全部。

       注意这里**不把服务端错误吞成空列表**：未知 agent 会被服务端判 400
       （unknown_agent），吞掉它就变成「这个助手没有历史」—— 用户没法分辨
       「真的没跑过」和「点了一个不存在的助手」。错误原样往上抛。 */
    repos: function (agent) {
      return jsonFetch('/api/repos' + qs({ agent: agent })).catch(function () {
        return { repos: [] };
      });
    },

    runs: function (repo, agent) {
      return jsonFetch('/api/runs' + qs({ repo: repo, agent: agent })).catch(function () {
        return { runs: [] };
      });
    },

    /* ---- 工作区：加入 / 移出 / 文件清单 ---- */

    candidates: function () {
      return jsonFetch('/api/workspace/candidates').catch(function () {
        return { candidates: [] };
      });
    },

    addRepo: function (path) {
      return jsonFetch('/api/repos', { method: 'POST', body: { path: path } });
    },

    removeRepo: function (path) {
      return jsonFetch('/api/repos/remove', { method: 'POST', body: { path: path } });
    },

    repoFiles: function (repo) {
      return jsonFetch('/api/repo-files?repo=' + encodeURIComponent(repo)).catch(function () {
        return { files: [] };
      });
    },

    /* ---- 审计运行 ---- */

    startAudit: function (options) {
      return jsonFetch('/api/audit', { method: 'POST', body: options });
    },

    snapshot: function (runId, opts) {
      var query = [];
      if (opts && opts.events) {
        query.push('events=1');
      }
      if (opts && opts.since) {
        query.push('since=' + opts.since);
      }
      return jsonFetch('/api/audit/' + runId + (query.length ? '?' + query.join('&') : ''));
    },

    stop: function (runId) {
      return jsonFetch('/api/audit/' + runId + '/stop', { method: 'POST', body: {} });
    },

    reportUrl: function (runId, format) {
      return url('/api/audit/' + runId + '/report' + (format ? '?format=' + format : ''));
    },

    /* ---- 事件流 ----
       返回 EventSource；调用方负责在 close/done/error 时 close()。
       handlers 以事件名为键，接收已解析的 data 对象。 */
    stream: function (runId, handlers, onOpen) {
      var source = new EventSource(url('/api/audit/' + runId + '/stream'));

      /* artifact 是产出的统一入口：kind=finding 走发现卡片，
         其余（layer / component / decision / open_question）走蓝图面板。
         旧的 finding 事件保留，老归档里只有它。 */
      var TYPES = [
        'run', 'prefix', 'step', 'tool', 'finding', 'artifact',
        'retry', 'compression', 'stopping', 'done', 'error', 'close',
      ];

      TYPES.forEach(function (type) {
        source.addEventListener(type, function (event) {
          var data = {};
          try {
            data = event.data ? JSON.parse(event.data) : {};
          } catch (error) {
            data = { raw: event.data };
          }
          if (handlers && handlers[type]) {
            handlers[type](data);
          }
        });
      });

      source.onopen = function () {
        if (onOpen) {
          onOpen(source);
        }
      };

      source.onerror = function () {
        // 服务端主动结束时也会触发 error，交给调用方用连接状态判断
        if (handlers && handlers.connection_error) {
          handlers.connection_error(source);
        }
      };

      return source;
    },
  };

  window.MARGIN_API = MARGIN_API;
})();
