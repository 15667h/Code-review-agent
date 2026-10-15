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
        }
        return payload;
      });
    });
  }

  var MARGIN_API = {
    served: served,
    base: served ? origin : '',

    /* ---- 服务与配置 ---- */

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

    repos: function () {
      return jsonFetch('/api/repos').catch(function () {
        return { repos: [] };
      });
    },

    runs: function (repo) {
      var query = repo ? '?repo=' + encodeURIComponent(repo) : '';
      return jsonFetch('/api/runs' + query).catch(function () {
        return { runs: [] };
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

      var TYPES = [
        'run', 'prefix', 'step', 'tool', 'finding',
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
