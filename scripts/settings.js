/* ==========================================================================
   Margin · 模型配置中心
   --------------------------------------------------------------------------
   接模型只需要三项：**接口地址 / API Key / 模型名称**。
   协议固定是 OpenAI 兼容 —— 这是业界事实标准，不需要用户再选「提供商」，
   界面也就不提供那个下拉框（提供了反而让人以为选错就会失效）。

   所有字段都是真的：读 `GET /api/config`，存 `POST /api/config`，
   测 `POST /api/config/test`。**没有演示分支** —— 后端没连上就明确说没连上，
   不会给出一个「演示结果，未发出真实请求」的假成功。
   ========================================================================== */

(function () {
  'use strict';

  var EYE_OPEN =
    '<svg width="17" height="17" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path d="M2 9s2.6-4.4 7-4.4S16 9 16 9s-2.6 4.4-7 4.4S2 9 2 9z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="9" cy="9" r="1.9" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';

  var EYE_OFF =
    '<svg width="17" height="17" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path d="M2 9s2.6-4.4 7-4.4S16 9 16 9s-2.6 4.4-7 4.4S2 9 2 9z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="9" cy="9" r="1.9" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3.4 3.4l11.2 11.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';

  var api = window.MARGIN_API;

  /* ---------- DOM ---------- */
  var baseUrl = document.getElementById('baseUrl');
  var apiKey = document.getElementById('apiKey');
  var model = document.getElementById('model');
  var modelOptions = document.getElementById('modelOptions');
  var temperature = document.getElementById('temperature');
  var timeout = document.getElementById('timeout');
  var maxIterations = document.getElementById('maxIterations');
  var contextWindow = document.getElementById('contextWindow');
  var allowCommands = document.getElementById('allowCommands');

  var saveBtn = document.getElementById('saveBtn');
  var saveLabel = document.getElementById('saveLabel');
  var saveState = document.getElementById('saveState');
  var resetBtn = document.getElementById('resetBtn');
  var testBtn = document.getElementById('testBtn');
  var detectBtn = document.getElementById('detectBtn');
  var connState = document.getElementById('connState');
  var badge = document.getElementById('backendBadge');
  var badgeText = document.getElementById('backendBadgeText');
  var persistNotice = document.getElementById('persistNotice');

  var live = !!(api && api.served);

  /* ==========================================================================
     密钥可见性
     ========================================================================== */

  document.querySelectorAll('[data-toggle-password]').forEach(function (button) {
    var target = document.getElementById(button.dataset.togglePassword);
    if (!target) {
      return;
    }
    button.addEventListener('click', function () {
      var showing = target.type === 'text';
      target.type = showing ? 'password' : 'text';
      button.setAttribute('aria-pressed', String(!showing));
      button.setAttribute('aria-label', showing ? '显示密钥' : '隐藏密钥');
      button.innerHTML = showing ? EYE_OPEN : EYE_OFF;
    });
  });

  /* ==========================================================================
     状态显示
     ========================================================================== */

  function setConnState(text, kind) {
    if (!connState) {
      return;
    }
    connState.className = kind ? 'conn-state conn-state--' + kind : 'conn-state';
    connState.textContent = text || '';
  }

  function flashSaveState(text) {
    if (!saveState) {
      return;
    }
    saveState.textContent = text;
    saveState.classList.add('is-on');
    window.clearTimeout(flashSaveState.timer);
    flashSaveState.timer = window.setTimeout(function () {
      saveState.classList.remove('is-on');
    }, 4200);
  }

  function setSaving(busy) {
    if (saveBtn) {
      saveBtn.disabled = busy;
    }
    if (saveLabel) {
      saveLabel.textContent = busy ? '保存中…' : '保存';
    }
  }

  function setBadge(state, text) {
    if (!badge) {
      return;
    }
    badge.hidden = false;
    badge.dataset.state = state;
    if (badgeText) {
      badgeText.textContent = text;
    }
  }

  /* ==========================================================================
     快照（重置用）
     ========================================================================== */

  var fields = [baseUrl, apiKey, model, temperature, timeout, maxIterations, contextWindow, allowCommands].filter(
    Boolean
  );

  function captureSnapshot() {
    return fields.map(function (node) {
      return node.value;
    });
  }

  function restoreSnapshot(snapshot) {
    fields.forEach(function (node, index) {
      if (snapshot[index] !== undefined) {
        node.value = snapshot[index];
      }
    });
  }

  var snapshot = captureSnapshot();

  /* ==========================================================================
     渲染后端配置
     ========================================================================== */

  function hydrate(config) {
    if (!config) {
      return;
    }
    if (config.base_url) {
      baseUrl.value = config.base_url;
    }
    if (config.model) {
      model.value = config.model;
    }

    /* 密钥只回显掩码，输入框本身保持空 —— 空表示「沿用已保存的」。
       把掩码写进 value 再提交上去，会把真密钥替换成掩码字符串。 */
    apiKey.value = '';
    apiKey.placeholder = config.api_key_set
      ? '已保存 ' + config.api_key_masked + '（留空则沿用）'
      : '粘贴密钥';

    var details = config.details || {};
    if (details.temperature != null) {
      temperature.value = details.temperature;
    }
    if (details.timeout != null) {
      timeout.value = details.timeout;
    }
    if (details.max_iterations != null) {
      maxIterations.value = details.max_iterations;
    }
    if (details.context_window != null) {
      contextWindow.value = details.context_window;
    }
    if (Array.isArray(details.allow_commands)) {
      allowCommands.value = details.allow_commands.join(', ');
    }

    renderReadiness(config);
    snapshot = captureSnapshot();
  }

  /* 三项是否齐 —— 直接决定「能不能跑」。缺项时把变量名给出来。 */
  function renderReadiness(config) {
    var missing = config.missing || [];
    if (config.ready) {
      setBadge('ready', '可用 · ' + config.provider);
      setConnState('配置完整：' + config.model + ' @ ' + config.base_url, 'ok');
      return;
    }
    setBadge('incomplete', '缺 ' + missing.length + ' 项');
    var hint = missing.map(function (name) {
      return name === 'LLM_BASE_URL' ? '接口地址' : name === 'LLM_API_KEY' ? 'API Key' : '模型名称';
    });
    setConnState('还缺：' + hint.join('、') + '。填完才能调用真实模型。', 'fail');
  }

  /* ==========================================================================
     拉取 / 保存
     ========================================================================== */

  function load() {
    if (!live) {
      setBadge('error', '未连接');
      setConnState(
        '后端没连上，页面读不到也存不了配置。先用 python -m harness.server 启动服务，再从这个地址打开。',
        'fail'
      );
      if (persistNotice) {
        persistNotice.hidden = true;
      }
      return;
    }

    api
      .config()
      .then(function (config) {
        hydrate(config);
        if (persistNotice) {
          persistNotice.hidden = false;
        }
      })
      .catch(function (error) {
        setBadge('error', '读取失败');
        setConnState('读取配置失败：' + (error && error.message ? error.message : error), 'fail');
      });
  }

  /* 只有用户真的改过的字段才提交 —— 省得把默认值一起写死进 .env，
     以后改默认值时旧文件不会把它钉住。 */
  function changed() {
    var patch = {};
    var initial = snapshot;

    function diff(index, key, cast) {
      var node = fields[index];
      if (!node) {
        return;
      }
      var now = node.value.trim();
      if (now === (initial[index] || '')) {
        return;
      }
      if (now === '' && key !== 'LLM_API_KEY') {
        return;
      }
      patch[key] = cast ? cast(now) : now;
    }

    diff(0, 'LLM_BASE_URL');
    /* 密钥：留空表示沿用，空值不提交 */
    var keyNow = apiKey.value.trim();
    if (keyNow) {
      patch.LLM_API_KEY = keyNow;
    }
    diff(2, 'LLM_MODEL');
    diff(3, 'AUDIT_TEMPERATURE', Number);
    diff(4, 'AUDIT_TIMEOUT', Number);
    diff(5, 'AUDIT_MAX_ITERATIONS', Number);
    diff(6, 'AUDIT_CONTEXT_WINDOW', Number);

    /* 命令白名单：逗号或空格分隔，去空 */
    var cmdNow = allowCommands.value.trim();
    if (cmdNow !== (initial[7] || '')) {
      patch.AUDIT_ALLOW_COMMANDS = cmdNow;
    }
    return patch;
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      if (!live) {
        flashSaveState('后端没连上，改不了');
        return;
      }
      var patch = changed();
      if (!Object.keys(patch).length) {
        flashSaveState('没有改动');
        return;
      }
      setSaving(true);
      api
        .saveConfig(patch)
        .then(function (result) {
          setSaving(false);
          if (!result || result.error) {
            flashSaveState('保存失败：' + ((result && result.error) || '未知错误'));
            return;
          }
          hydrate(result.config);
          flashSaveState(
            '已写入 backend/.env：' +
              (result.updated && result.updated.length ? result.updated.join('、') : '（无变化）')
          );
        })
        .catch(function (error) {
          setSaving(false);
          flashSaveState('保存失败：' + (error && error.message ? error.message : error));
        });
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      restoreSnapshot(snapshot);
      setConnState('', '');
      flashSaveState('已恢复为打开页面时的值（未写盘）');
    });
  }

  /* ==========================================================================
     测试连接 / 探测模型
     ========================================================================== */

  function probePayload() {
    var payload = {
      base_url: baseUrl.value.trim(),
      model: model.value.trim(),
    };
    /* 只在用户真填了新密钥时才带上；空表示沿用已保存的 */
    var key = apiKey.value.trim();
    if (key) {
      payload.api_key = key;
    }
    return payload;
  }

  function runTest(button) {
    if (!live) {
      setConnState('后端没连上，测不了。先启动服务再打开这个页面。', 'fail');
      return Promise.resolve(null);
    }
    var payload = probePayload();
    if (!payload.base_url) {
      setConnState('先填接口地址。', 'fail');
      return Promise.resolve(null);
    }

    button.disabled = true;
    setConnState('正在连接 ' + payload.base_url + ' …', 'testing');

    return api
      .testConfig(payload)
      .then(function (result) {
        button.disabled = false;
        if (result && result.ok) {
          var ids = result.model_ids || [];
          if (ids.length) {
            fillOptions(ids);
          }
          var text = '接口可达 · ' + result.latency_ms + 'ms';
          if (ids.length) {
            text += ' · 可见 ' + ids.length + ' 个模型';
          }
          setConnState(text, 'ok');
          if (result.hint) {
            setConnState(text + '。' + result.hint, 'warn');
          }
        } else {
          setConnState('连接失败：' + ((result && result.error) || '未知错误'), 'fail');
        }
        return result;
      })
      .catch(function (error) {
        button.disabled = false;
        setConnState('连接失败：' + (error && error.message ? error.message : error), 'fail');
        return null;
      });
  }

  function fillOptions(ids) {
    if (!modelOptions) {
      return;
    }
    modelOptions.textContent = '';
    ids.forEach(function (id) {
      var option = document.createElement('option');
      option.value = id;
      modelOptions.appendChild(option);
    });
  }

  if (testBtn) {
    testBtn.addEventListener('click', function () {
      runTest(testBtn);
    });
  }

  /* 探测：连上之后把模型名直接填上 —— 省得用户去翻文档抄名字 */
  if (detectBtn) {
    detectBtn.addEventListener('click', function () {
      runTest(detectBtn).then(function (result) {
        if (!result || !result.ok || !result.model_ids || !result.model_ids.length) {
          return;
        }
        if (!model.value.trim()) {
          model.value = result.model_ids[0];
          setConnState('已填入第一个可用模型：' + model.value + '（可改）', 'ok');
        }
      });
    });
  }

  /* ==========================================================================
     主题跟随工作台
     ========================================================================== */

  function syncTheme() {
    try {
      var saved = window.localStorage.getItem('margin.theme');
      document.documentElement.setAttribute('data-theme', saved === 'light' ? 'light' : 'dark');
    } catch (error) {
      /* 隐私模式下读不到，保持默认深色 */
    }
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      syncTheme();
    }
  });

  load();
})();
