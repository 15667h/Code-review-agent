/* ==========================================================================
   Margin · 模型配置中心
   纯前端演示：所有保存动作都只改本地状态，不发出真实请求。
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
  var snapshot = fields.map(function (node) {
    return node.type === 'checkbox' || node.type === 'radio' ? node.checked : node.value;
  });

  /* ==========================================================================
     读取上次的默认模型
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
      testBtn.disabled = true;
      connState.className = 'conn-state conn-state--testing';
      connState.textContent = '正在连接 ' + (baseUrl ? baseUrl.value : '') + ' …';

      window.setTimeout(function () {
        testBtn.disabled = false;
        connState.className = 'conn-state conn-state--ok';
        connState.textContent = '连接正常 · 3 个模型已启用（演示结果，未发出真实请求）';
      }, 1200);
    });
  }

  /* ==========================================================================
     保存 / 重置
     ========================================================================== */

  var saveBtn = document.getElementById('saveBtn');
  var saveLabel = document.getElementById('saveLabel');
  var saveState = document.getElementById('saveState');
  var resetBtn = document.getElementById('resetBtn');

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

  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      setSaving(true);

      window.setTimeout(function () {
        setSaving(false);

        // 把默认模型带回工作台
        var checked = document.querySelector('input[name="default-model"]:checked');
        if (checked && checked.value) {
          try {
            window.localStorage.setItem('margin.demo.model', checked.value);
          } catch (error) {
            /* 忽略 */
          }
        }

        if (saveState) {
          saveState.textContent = '已保存';
          window.setTimeout(function () {
            if (saveState.textContent === '已保存') {
              saveState.textContent = '';
            }
          }, 2600);
        }
      }, 900);
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

      if (connState) {
        connState.className = 'conn-state';
        connState.textContent = '';
      }

      if (saveState) {
        saveState.textContent = '已恢复为打开页面时的设置';
        window.setTimeout(function () {
          if (saveState.textContent === '已恢复为打开页面时的设置') {
            saveState.textContent = '';
          }
        }, 2600);
      }
    });
  }
})();
