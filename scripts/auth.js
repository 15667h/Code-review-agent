/* ==========================================================================
   Margin · 登录 / 注册表单
   纯前端演示：不发任何真实请求，通过校验后跳转到工作台。
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

  var MESSAGES = {
    emailEmpty: '请输入邮箱地址。',
    emailBad: '邮箱格式不对，检查一下 @ 前后是否完整。',
    passwordEmpty: '请输入密码。',
    passwordShort: '密码至少 8 位，现在还不够。',
    nameEmpty: '请填写名字，它会显示在审查记录里。',
    terms: '需要先同意服务条款才能创建账号。',
  };

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

    // 演示：不发请求，短暂延迟后进入工作台
    window.setTimeout(function () {
      try {
        window.sessionStorage.setItem('margin.demo.user', input('email').value.trim());
      } catch (error) {
        /* file:// 下可能不可用，忽略即可 */
      }
      window.location.href = 'index.html';
    }, 1000);
  });

  /* ==========================================================================
     第三方登录与忘记密码（演示环境的行为）
     ========================================================================== */

  form.querySelectorAll('[data-demo-enter]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (busy) {
        return;
      }
      setLoading(true);
      window.setTimeout(function () {
        window.location.href = 'index.html';
      }, 700);
    });
  });

  var forgot = form.querySelector('[data-forgot]');
  if (forgot && note) {
    forgot.addEventListener('click', function () {
      note.className = 'form-note form-note--hint';
      note.textContent = '演示环境不提供重置流程：直接用任意邮箱加 8 位以上密码登录即可。';
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
