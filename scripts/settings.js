/* ==========================================================================
   Margin · 模型配置中心
   接入后端时，「测试连接」与「保存更改」打的是真实接口
   （POST /api/config/test 与 POST /api/config）；
   用 file:// 直接打开时退回纯前端演示，不发任何请求。

   会落盘的字段由后端的白名单决定（见 harness/server.py 的 WRITABLE_KEYS），
   界面上有一部分字段目前只是演示，保存时不会写入 —— 这一点在页面上明确标注了。
   ========================================================================== */

(function () {
  'use strict';

  var EYE_OPEN =
    '<svg width="17" height="17" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path d="M2 9s2.6-4.4 7-4.4S16 9 16 9s-2.6 4.4-7 4.4S2 9 2 9z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="9" cy="9" r="1.9" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';

  var EYE_OFF =
    '<svg width="17" height="17" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path d="M2 9s2.6-4.4 7-4.4S16 9 16 9s-2.6 4.4-7 4.4S2 9 2 9z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="9" cy="9" r="1.9" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3.4 3.4l11.2 11.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';

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
      target.focus();
    });
  });

  /* ==========================================================================
     滑块与数值联动
     ========================================================================== */

  function syncRange(range) {
    var out = document.getElementById(range.dataset.out);
    if (out) {
      out.textContent = range.value;
    }
  }

  document.querySelectorAll('.range[data-out]').forEach(function (range) {
    range.addEventListener('input', function () {
      syncRange(range);
    });
    syncRange(range);
  });

  /* ==========================================================================
     提供商 → API 地址联动
     ========================================================================== */

  var provider = document.getElementById('provider');
  var baseUrl = document.getElementById('baseUrl');

  if (provider && baseUrl) {
    provider.addEventListener('change', function () {
      var value = provider.value;
      if (value.indexOf('Anthropic 兼容') !== -1) {
        baseUrl.value = 'https://api.deepseek.com/anthropic';
      } else if (value.indexOf('DeepSeek') !== -1) {
        baseUrl.value = 'https://api.deepseek.com';
      } else if (value.indexOf('Ollama') !== -1) {
        baseUrl.value = 'http://localhost:11434/v1';
      } else if (value.indexOf('阿里云百炼') !== -1) {
        baseUrl.value = 'https://dashscope.aliyuncs.com/compatible-mode/v1';
      }
    });
  }

  /* ==========================================================================
     自定义请求头
     ========================================================================== */

  var headers = document.getElementById('headers');
  var addHeader = document.getElementById('addHeader');

  function addHeaderRow(name, value, shouldFocus) {
    if (!headers) {
      return;
    }

    var row = document.createElement('div');
    row.className = 'header-row';

    var nameInput = document.createElement('input');
    nameInput.className = 'input input--mono';
    nameInput.type = 'text';
    nameInput.placeholder = 'X-Tenant-Id';
    nameInput.value = name || '';
    nameInput.spellcheck = false;
    nameInput.setAttribute('aria-label', '请求头名称');

    var valueInput = document.createElement('input');
    valueInput.className = 'input input--mono';
    valueInput.type = 'text';
    valueInput.placeholder = '值';
    valueInput.value = value || '';
    valueInput.spellcheck = false;
    valueInput.setAttribute('aria-label', '请求头取值');

    var remove = document.createElement('button');
    remove.className = 'header-row__remove';
    remove.type = 'button';
    remove.setAttribute('aria-label', '删除这一行请求头');
    remove.innerHTML =
      '<svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3.5 4.5h9M6.4 4.5V3.3c0-.4.3-.8.8-.8h1.6c.5 0 .8.4.8.8v1.2M5 4.5l.6 8c0 .6.5 1 1 1h2.8c.6 0 1-.4 1-1l.6-8" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    remove.addEventListener('click', function () {
      row.remove();
    });

    row.appendChild(nameInput);
    row.appendChild(valueInput);
    row.appendChild(remove);
    headers.appendChild(row);

    // 只在用户主动新增时才抢焦点。加载时若聚焦末尾的输入框，
    // 浏览器会自动把页面滚到底部，看上去就像页面空了。
    if (shouldFocus) {
      nameInput.focus();
    }

    return row;
  }

  if (addHeader) {
    addHeader.addEventListener('click', function () {
      addHeaderRow('', '', true);
    });
  }

  // 给一个真实感的初始值（不抢焦点）
  addHeaderRow('X-Tenant-Id', 'meridian-labs', false);

  /* ==========================================================================
     初始快照（供重置使用）
     ========================================================================== */

  var fields = Array.prototype.slice.call(
    document.querySelectorAll('.settings input, .settings select')
  );

  function captureSnapshot() {
    return fields.map(function (node) {
      return node.type === 'checkbox' || node.type === 'radio' ? node.checked : node.value;
    });
  }

  var snapshot = captureSnapshot();

  /* ==========================================================================
     后端接入
     ========================================================================== */

  var api = window.MARGIN_API;
  var live = false;

  var apiKeyField = document.getElementById('apiKey');
  var saveBtn = document.getElementById('saveBtn');
  var saveLabel = document.getElementById('saveLabel');
  var saveState = document.getElementById('saveState');
  var resetBtn = document.getElementById('resetBtn');

  function setConnState(text, kind) {
    if (!connState) {
      return;
    }
    connState.className = kind ? 'conn-state conn-state--' + kind : 'conn-state';
    connState.textContent = text || '';
  }

  function setBadge(state, text) {
    var badge = document.getElementById('backendBadge');
    var label = document.getElementById('backendBadgeText');
    if (badge) {
      badge.dataset.state = state;
      badge.hidden = false;
    }
    if (label) {
      label.textContent = text;
    }
  }

  function checkedModel() {
    var node = document.querySelector('input[name="default-model"]:checked');
    return node ? node.value : '';
  }

  function numberOr(node, fallback) {
    if (!node) {
      return fallback;
    }
    var value = parseFloat(node.value);
    return isNaN(value) ? fallback : value;
  }

  /* 用后端配置回填表单。密钥不回显，只在 placeholder 里显示掩码。 */
  function hydrate(config) {
    if (baseUrl && config.base_url) {
      baseUrl.value = config.base_url;
    }
    if (provider && config.provider) {
      // 提供商名会同时命中多个选项（OpenAI 兼容 / Anthropic 兼容），
      // 必须再用 base_url 区分一次，否则会被列表里靠后的那个覆盖掉。
      var want = config.base_url.indexOf("/anthropic") !== -1 ? "Anthropic 兼容" : "OpenAI 兼容";
      var options = Array.prototype.slice.call(provider.options);
      var matches = options.filter(function (option) {
        return option.textContent.indexOf(config.provider) !== -1;
      });
      var exact = matches.filter(function (option) {
        return option.textContent.indexOf(want) !== -1;
      });
      var chosen = exact[0] || matches[0];
      if (chosen) {
        provider.value = chosen.value;
      }
    }
    if (apiKeyField) {
      apiKeyField.value = '';
      apiKeyField.placeholder = config.api_key_set
        ? config.api_key_masked + '　已保存；留空则不改动'
        : 'sk-...';
    }
    if (config.model) {
      var radio = document.querySelector(
        'input[name="default-model"][value="' + config.model + '"]'
      );
      if (radio) {
        radio.checked = true;
      }
    }
    var temperature = document.getElementById('temperature');
    if (temperature && config.temperature != null) {
      temperature.value = String(config.temperature);
    }
    var timeout = document.getElementById('timeout');
    if (timeout && config.timeout != null) {
      timeout.value = String(config.timeout);
    }
    document.querySelectorAll('.range[data-out]').forEach(syncRange);
  }

  async function bootBackend() {
    if (!api || !api.served) {
      setBadge('demo', '演示数据');
      return;
    }
    var config = await api.config();
    if (!config || config.error) {
      setBadge('error', '后端不可达');
      return;
    }
    live = true;
    hydrate(config);
    // 回填之后再取快照，否则「重置」会退回 HTML 里的演示初值
    snapshot = captureSnapshot();
    setBadge(config.ready ? 'live' : 'error', config.provider || '后端已连接');
    setConnState(
      '已读取后端配置 · ' + config.provider + ' · ' +
        (config.api_key_set ? '密钥已配置' : '未配置密钥'),
      config.api_key_set ? 'ok' : 'warn'
    );
    var notice = document.getElementById('persistNotice');
    if (notice) {
      notice.hidden = false;
    }
  }

  /* ==========================================================================
     读取上次的默认模型（演示模式用；实时模式由后端配置决定）
     ========================================================================== */

  try {
    var savedModel = window.localStorage.getItem('margin.demo.model');
    if (savedModel) {
      var radio = document.querySelector('input[name="default-model"][value="' + savedModel + '"]');
      if (radio) {
        radio.checked = true;
      }
    }
  } catch (error) {
    /* file:// 下可能不可用，忽略 */
  }

  /* ==========================================================================
     测试连接
     ========================================================================== */

  var testBtn = document.getElementById('testBtn');
  var connState = document.getElementById('connState');

  if (testBtn && connState) {
    testBtn.addEventListener('click', function () {
      var payload = {
        base_url: baseUrl ? baseUrl.value.trim() : '',
        model: checkedModel(),
      };
      // 只在用户真的填了新密钥时才带上；留空表示沿用已保存的
      if (apiKeyField && apiKeyField.value.trim()) {
        payload.api_key = apiKeyField.value.trim();
      }

      if (!live) {
        testBtn.disabled = true;
        setConnState('正在连接 ' + payload.base_url + ' …', 'testing');
        window.setTimeout(function () {
          testBtn.disabled = false;
          setConnState('连接正常 · 3 个模型已启用（演示结果，未发出真实请求）', 'ok');
        }, 1200);
        return;
      }

      testBtn.disabled = true;
      setConnState('正在连接 ' + payload.base_url + ' …', 'testing');
      api.testConfig(payload).then(function (result) {
        testBtn.disabled = false;
        if (result && result.ok) {
          setConnState(
            '连接正常 · ' + result.latency_ms + 'ms · 可用模型 ' +
              (result.model_ids || []).join('、'),
            'ok'
          );
        } else {
          setConnState('连接失败：' + ((result && result.error) || '未知错误'), 'fail');
        }
      });
    });
  }

  /* ==========================================================================
     保存 / 重置
     ========================================================================== */

  function setSaving(state) {
    if (!saveBtn) {
      return;
    }
    saveBtn.disabled = state;
    saveLabel.textContent = state ? '保存中…' : '保存更改';
    var spinner = saveBtn.querySelector('.spinner');
    if (state && !spinner) {
      saveBtn.insertAdjacentHTML('afterbegin', '<span class="spinner" aria-hidden="true"></span>');
    } else if (!state && spinner) {
      spinner.remove();
    }
  }

  function flashSaveState(text) {
    if (!saveState) {
      return;
    }
    saveState.textContent = text;
    window.setTimeout(function () {
      if (saveState.textContent === text) {
        saveState.textContent = '';
      }
    }, 3200);
  }

  function rememberModel() {
    var model = checkedModel();
    if (!model) {
      return;
    }
    try {
      window.localStorage.setItem('margin.demo.model', model);
    } catch (error) {
      /* 忽略 */
    }
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      setSaving(true);

      if (!live) {
        window.setTimeout(function () {
          setSaving(false);
          rememberModel();
          flashSaveState('已保存（演示，未写入文件）');
        }, 900);
        return;
      }

      var patch = {};
      if (baseUrl && baseUrl.value.trim()) {
        patch.DEEPSEEK_BASE_URL = baseUrl.value.trim();
      }
      if (checkedModel()) {
        patch.DEEPSEEK_MODEL = checkedModel();
      }
      var temperature = numberOr(document.getElementById('temperature'), null);
      if (temperature !== null) {
        patch.AUDIT_TEMPERATURE = temperature;
      }
      var timeout = numberOr(document.getElementById('timeout'), null);
      if (timeout !== null) {
        patch.AUDIT_TIMEOUT = timeout;
      }
      if (apiKeyField && apiKeyField.value.trim()) {
        patch.DEEPSEEK_API_KEY = apiKeyField.value.trim();
      }

      api.saveConfig(patch).then(function (result) {
        setSaving(false);
        if (!result || result.error) {
          flashSaveState('保存失败：' + ((result && result.error) || '未知错误'));
          return;
        }
        rememberModel();
        hydrate(result.config);
        snapshot = captureSnapshot();
        flashSaveState(
          '已写入 backend/.env：' +
            (result.updated && result.updated.length
              ? result.updated.join('、')
              : '（本次没有字段变化）')
        );
      });
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      fields.forEach(function (node, index) {
        if (node.type === 'checkbox' || node.type === 'radio') {
          node.checked = snapshot[index];
        } else {
          node.value = snapshot[index];
        }
      });

      // 值变了，滑块的数值显示要跟着回位
      document.querySelectorAll('.range[data-out]').forEach(syncRange);

      setConnState('', '');

      flashSaveState(live ? '已恢复为后端当前配置' : '已恢复为打开页面时的设置');
    });
  }

  bootBackend();
})();
