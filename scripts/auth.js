/* ==========================================================================
   Margin · 登录 / 注册表单

   真调后端：`POST /api/auth/login|register`，成功后后端下发 HttpOnly 会话
   Cookie，前端只负责跳转。校验仍在前端做一遍 —— 那是为了即时反馈，
   后端的校验才是边界（前端能改，服务端不能信）。

   后端不可达时（`file://` 直开或服务没起）**如实报错**，不假装登录成功 ——
   假装成功会让人以为账号真的创建了。
   ========================================================================== */

(function () {
  'use strict';

  var form = document.querySelector('[data-auth-form]');
  if (!form) {
    return;
  }

  var mode = form.dataset.authForm;
  var submitBtn = form.querySelector('[data-submit]');
  var submitLabel = submitBtn.querySelector('[data-submit-label]');
  var note = form.querySelector('[data-form-note]');
  var strength = form.querySelector('[data-strength]');
  var busy = false;

  /* 测试账户：与后端 backend/harness/auth.py 的 DEMO_ACCOUNTS 一致。
     只为了「本机演示能一键进入」，不是任何形式的凭据分发 —— 首次启动时
     后端会把它们写进 .auth/users.json（不入库）。 */
  var DEMO_CREDENTIALS = {
    'demo@margin.dev': 'Margin-demo-2026',
    'reviewer@margin.dev': 'Margin-review-2026',
  };

  /* 后端错误码 → 该把提示挂在哪个字段上。挂错字段比不挂更让人困惑。 */
  var CODE_FIELD = {
    email_empty: 'email',
    email_invalid: 'email',
    email_taken: 'email',
    password_empty: 'password',
    password_short: 'password',
    password_too_long: 'password',
    name_empty: 'name',
    name_too_long: 'name',
  };

  var MESSAGES = {
    emailEmpty: '请输入邮箱地址。',
    emailBad: '邮箱格式不对，检查一下 @ 前后是否完整。',
    passwordEmpty: '请输入密码。',
    passwordShort: '密码至少 8 位，现在还不够。',
    nameEmpty: '请填写名字，它会显示在审查记录里。',
    terms: '需要先同意服务条款才能创建账号。',
  };

  /* 提示里的账号密码从这里渲染 —— 与一键登录用的是同一份常量，
     不会出现「提示写 A、实际用 B」。 */
  var hintBox = form.querySelector('[data-demo-hint]');
  var primary = Object.keys(DEMO_CREDENTIALS)[0];
  if (hintBox && primary) {
    hintBox.textContent = '测试账号：' + primary + ' · 密码 ' + DEMO_CREDENTIALS[primary];
    hintBox.hidden = false;
  }

  /* 已经登录过就别再登一次：直接进工作台。
     会话在 Cookie 里，me 会带回当前用户。 */
  if (window.MARGIN_API && window.MARGIN_API.served) {
    window.MARGIN_API.me().then(function (payload) {
      if (payload && payload.user) {
        window.location.href = 'index.html';
      }
    }).catch(function () {
      /* 后端不在也不影响填写表单 —— 提交时还会再报一次，不在这里刷屏 */
    });
  }

  /* ==========================================================================
     字段工具
     ========================================================================== */

  function box(name) {
    return form.querySelector('[data-field="' + name + '"]');
  }

  function input(name) {
    var node = box(name);
    return node ? node.querySelector('.input') : null;
  }

  function setError(name, message) {
    var node = box(name);
    if (!node) {
      return;
    }
    var field = node.querySelector('.input');
    var error = node.querySelector('.field__error');

    if (message) {
      field.setAttribute('aria-invalid', 'true');
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
      return;
    }

    field.removeAttribute('aria-invalid');
    if (error) {
      error.textContent = '';
      error.hidden = true;
    }
  }

  function clearAllErrors() {
    form.querySelectorAll('[data-field]').forEach(function (node) {
      setError(node.dataset.field, null);
    });
    if (note) {
      note.hidden = true;
    }
  }

  /* ==========================================================================
     校验
     ========================================================================== */

  var EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function checkField(name) {
    var value = input(name) ? input(name).value.trim() : '';

    if (name === 'email') {
      if (!value) {
        setError(name, MESSAGES.emailEmpty);
        return false;
      }
      if (!EMAIL.test(value)) {
        setError(name, MESSAGES.emailBad);
        return false;
      }
      setError(name, null);
      return true;
    }

    if (name === 'password') {
      if (!value) {
        setError(name, MESSAGES.passwordEmpty);
        return false;
      }
      // 登录不校验强度（可能是历史密码），注册才要求长度
      if (mode === 'register' && value.length < 8) {
        setError(name, MESSAGES.passwordShort);
        return false;
      }
      setError(name, null);
      return true;
    }

    if (name === 'name') {
      if (!value) {
        setError(name, MESSAGES.nameEmpty);
        return false;
      }
      setError(name, null);
      return true;
    }

    return true;
  }

  function validate() {
    var names = ['name', 'email', 'password'].filter(function (name) {
      return !!box(name);
    });
    var firstBad = null;

    names.forEach(function (name) {
      if (!checkField(name) && !firstBad) {
        firstBad = name;
      }
    });

    var terms = form.querySelector('[data-terms]');
    if (terms && !terms.checked) {
      if (note) {
        note.textContent = MESSAGES.terms;
        note.hidden = false;
      }
      if (!firstBad) {
        firstBad = 'terms';
      }
    } else if (note) {
      note.hidden = true;
    }

    return firstBad;
  }

  /* ==========================================================================
     密码强度
     ========================================================================== */

  var LEVELS = ['', '太短', '较弱', '一般', '够用'];

  function scorePassword(value) {
    if (!value) {
      return 0;
    }
    var score = 0;
    if (value.length >= 8) {
      score += 1;
    }
    if (value.length >= 12) {
      score += 1;
    }
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) {
      score += 1;
    }
    if (/\d/.test(value)) {
      score += 1;
    }
    if (/[^A-Za-z0-9]/.test(value)) {
      score += 1;
    }
    return Math.max(1, Math.min(4, score));
  }

  /* ==========================================================================
     事件绑定
     ========================================================================== */

  form.querySelectorAll('[data-field]').forEach(function (node) {
    var name = node.dataset.field;
    var field = node.querySelector('.input');
    if (!field) {
      return;
    }

    // 输入时清掉该字段的错误，避免一边打字一边红
    field.addEventListener('input', function () {
      if (field.getAttribute('aria-invalid') === 'true') {
        setError(name, null);
      }
    });

    field.addEventListener('blur', function () {
      if (field.value.trim()) {
        checkField(name);
      }
    });
  });

  // 密码可见性切换
  form.querySelectorAll('[data-toggle-password]').forEach(function (button) {
    var target = input(button.dataset.togglePassword);
    if (!target) {
      return;
    }

    var EYE = button.innerHTML;

    button.addEventListener('click', function () {
      var showing = target.type === 'text';
      target.type = showing ? 'password' : 'text';
      button.setAttribute('aria-pressed', String(!showing));
      button.setAttribute('aria-label', showing ? '显示密码' : '隐藏密码');
      button.innerHTML = showing
        ? EYE
        : '<svg width="17" height="17" viewBox="0 0 18 18" aria-hidden="true"><path d="M2 9s2.6-4.4 7-4.4S16 9 16 9s-2.6 4.4-7 4.4S2 9 2 9z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="9" cy="9" r="1.9" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3.4 3.4l11.2 11.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
      target.focus();
    });
  });

  // 密码强度条
  if (strength) {
    var pw = input('password');
    if (pw) {
      pw.addEventListener('input', function () {
        var level = scorePassword(pw.value);
        strength.hidden = pw.value.length === 0;
        strength.dataset.level = String(level);
        var label = strength.querySelector('.strength__label');
        if (label) {
          label.textContent = level ? '密码强度：' + LEVELS[level] : '';
        }
      });
    }
  }

  /* ==========================================================================
     提交
     ========================================================================== */

  function setLoading(state) {
    busy = state;
    submitBtn.disabled = state;
    if (state) {
      submitLabel.textContent = mode === 'login' ? '登录中…' : '创建中…';
      if (!submitBtn.querySelector('.spinner')) {
        submitBtn.insertAdjacentHTML('afterbegin', '<span class="spinner" aria-hidden="true"></span>');
      }
    } else {
      submitLabel.textContent = mode === 'login' ? '登录' : '创建账号';
      var spinner = submitBtn.querySelector('.spinner');
      if (spinner) {
        spinner.remove();
      }
    }
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (busy) {
      return;
    }

    clearAllErrors();
    var firstBad = validate();

    if (firstBad === 'terms') {
      var termsBox = form.querySelector('[data-terms]');
      if (termsBox) {
        termsBox.focus();
      }
      return;
    }

    if (firstBad) {
      var target = input(firstBad);
      if (target) {
        target.focus();
      }
      return;
    }

    setLoading(true);

    var email = input('email').value.trim();
    var password = input('password').value;
    var nameInput = input('name');

    var api = window.MARGIN_API;
    if (!api || !api.served) {
      // file:// 直开：同源前提不成立，如实说明而不是假装成功
      setLoading(false);
      showNote('登录需要后端服务：在 backend/ 目录运行 python -m harness.server，然后访问 http://127.0.0.1:8765/。');
      return;
    }

    var request = mode === 'register'
      ? api.register(email, nameInput ? nameInput.value.trim() : '', password)
      : api.login(email, password);

    request
      .then(function (payload) {
        if (payload && payload.user) {
          try {
            window.sessionStorage.setItem('margin.user', payload.user.email);
          } catch (error) {
            /* 隐私模式下不可用，忽略即可 —— 登录态本身由 Cookie 维持 */
          }
          window.location.href = 'index.html';
          return;
        }
        setLoading(false);
        reportFailure(payload);
      })
      .catch(function () {
        setLoading(false);
        showNote('连不上后端服务。确认它在运行（backend/ 目录：python -m harness.server）。');
      });
  });

  /* 把后端的失败翻译成字段级提示。 */
  function reportFailure(payload) {
    var data = payload || {};
    var code = data.code || '';
    var message = data.error || '登录失败，请重试。';
    var field = CODE_FIELD[code];

    if (field && box(field)) {
      setError(field, message);
      var target = input(field);
      if (target) {
        target.focus();
      }
      return;
    }
    showNote(message);
  }

  function showNote(text) {
    if (!note) {
      return;
    }
    note.className = 'form-note form-note--error';
    note.textContent = text;
    note.hidden = false;
  }

  /* ==========================================================================
     测试账号一键登录与忘记密码
     ========================================================================== */

  form.querySelectorAll('[data-demo-enter]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (busy) {
        return;
      }
      var email = button.dataset.demoEnter;
      var password = DEMO_CREDENTIALS[email];
      if (!email || !password) {
        return;
      }

      if (!window.MARGIN_API || !window.MARGIN_API.served) {
        showNote('测试账号需要后端服务：在 backend/ 目录运行 python -m harness.server。');
        return;
      }

      // 先填进表单（用户能看见用了哪个账号），再提交
      if (input('email')) {
        input('email').value = email;
      }
      if (input('password')) {
        input('password').value = password;
      }
      if (input('name') && !input('name').value) {
        input('name').value = email === 'demo@margin.dev' ? '演示账号' : '林哲';
      }
      var terms = form.querySelector('[data-terms]');
      if (terms) {
        terms.checked = true;  // 测试账号走的是既有账号，不需要再勾条款
      }

      setLoading(true);
      window.MARGIN_API.login(email, password)
        .then(function (payload) {
          if (payload && payload.user) {
            window.location.href = 'index.html';
            return;
          }
          setLoading(false);
          reportFailure(payload);
        })
        .catch(function () {
          setLoading(false);
          showNote('连不上后端服务。确认它在运行（backend/ 目录：python -m harness.server）。');
        });
    });
  });

  var forgot = form.querySelector('[data-forgot]');
  if (forgot && note) {
    forgot.addEventListener('click', function () {
      note.className = 'form-note form-note--hint';
      note.textContent = '本机服务不发邮件，没有重置流程。忘记密码就用测试账号登录，'
        + '或删掉 backend/.auth/users.json 重启服务（会重新生成测试账户）。';
      note.hidden = false;
    });
  }

  /* ==========================================================================
     记住邮箱（纯本地，演示用）
     ========================================================================== */

  var remember = form.querySelector('[data-remember]');
  var emailField = input('email');

  if (remember && emailField) {
    try {
      var saved = window.localStorage.getItem('margin.demo.email');
      if (saved) {
        emailField.value = saved;
        remember.checked = true;
      }
    } catch (error) {
      /* 忽略 */
    }

    form.addEventListener('submit', function () {
      try {
        if (remember.checked) {
          window.localStorage.setItem('margin.demo.email', emailField.value.trim());
        } else {
          window.localStorage.removeItem('margin.demo.email');
        }
      } catch (error) {
        /* 忽略 */
      }
    });
  }
})();
