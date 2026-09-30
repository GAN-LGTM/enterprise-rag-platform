/* ============================================================================
 * antd-bridge.js —— 在零构建原生 JS 前端中接入 Ant Design 5（本地 vendor UMD）
 *
 * 职责：
 *   1. 用 ConfigProvider(项目主题) + App 组件建立受主题控制的 holder，
 *      导出与 antd 主题一致的 message / Modal.confirm 实例（避免静态方法的默认蓝主题）。
 *   2. 提供 formModal()：基于 antd Modal + Form 的承诺式输入弹窗，
 *      替代原生 window.prompt / window.confirm。
 *
 * 对外接口（挂到 window.AntdUI）：
 *   AntdUI.message.success/error/info/warning(text)   → antd message
 *   AntdUI.confirm({ title, message, okText, cancelText, danger })  → Promise<boolean>
 *   AntdUI.formModal({ title, width, okText, cancelText, fields, validate }) → Promise<values|null>
 *     fields: [{ name, label, type: 'text'|'password'|'textarea'|'select',
 *                placeholder, initialValue, required, requiredMsg,
 *                rules: [{required, min, max, pattern, message, validator}],
 *                options: [{value,label}], extra }]
 *     validate(values) → 返回错误文案字符串则阻止提交（弹窗保持打开）
 * ========================================================================== */
(function () {
  'use strict';
  if (!window.React || !window.ReactDOM || !window.dayjs || !window.antd) {
    console.error('[antd-bridge] 缺少 vendor 依赖（react / react-dom / dayjs / antd）');
    return;
  }
  var h = React.createElement;
  var Cfg = antd.ConfigProvider, App = antd.App, Modal = antd.Modal, Form = antd.Form;
  var Input = antd.Input, Select = antd.Select;

  /* ---- 项目主题令牌：与 styles.css :root 保持一致 ---- */
  var THEME = {
    token: {
      colorPrimary: '#2b84a6',
      colorInfo: '#2b84a6',
      colorError: '#dc2626',
      colorWarning: '#b45309',
      colorSuccess: '#15803d',
      borderRadius: 8,
      colorBorder: '#d8e2e6',
      fontFamily: "'PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif",
    }
  };

  /* ---- 主题化 holder：挂一次，message/modal 都从这里取实例 ---- */
  var api = null; // { message, modal, notification }
  function Bridge() {
    var inst = App.useApp();
    api = { message: inst.message, modal: inst.modal, notification: inst.notification };
    return null;
  }
  var holder = document.createElement('div');
  holder.id = 'antd-bridge-holder';
  document.body.appendChild(holder);
  /* 全局提示统一位置：顶部居中、页头下方（页头高约 60px），3 秒自动消失。
     注意：App 包裹的 message 实例不读全局 message.config，必须通过 App 的 message prop 传 */
  ReactDOM.createRoot(holder).render(
    h(Cfg, { theme: THEME },
      h(App, { message: { top: 76, duration: 3 } }, h(Bridge, null)))
  );

  function ready(cb) {
    if (api) return cb(api);
    var n = 0;
    var t = setInterval(function () {
      if (api || ++n > 100) { clearInterval(t); cb(api); }
    }, 50);
  }

  /* ==================== ① message（替代原生 alert / 自制 toast） ==================== */
  var TYPE_MAP = { ok: 'success', success: 'success', no: 'error', error: 'error', warn: 'warning' };
  function message(text, type, duration) {
    ready(function (a) {
      if (!a) return;
      (a.message[TYPE_MAP[type] || type || 'info'] || a.message.info)(text, duration || 3);
    });
  }

  /* ==================== ② confirm（替代原生 confirm / 自制 uiConfirm） ==================== */
  function confirm(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      ready(function (a) {
        if (!a) { resolve(false); return; }
        var inst = a.modal.confirm({
          title: opts.title || '确认操作',
          content: opts.message || '',
          okText: opts.okText || '确定',
          cancelText: opts.cancelText || '取消',
          okButtonProps: { danger: !!opts.danger },
          maskClosable: false,
          centered: true,
          width: opts.width || 420,
          onOk: function () { resolve(true); },
          onCancel: function () { resolve(false); }
        });
        if (opts.danger) inst.update({ okButtonProps: { danger: true } });
      });
    });
  }

  /* ==================== ③ formModal（替代原生 prompt） ==================== */
  function renderField(f) {
    if (f.type === 'password') return h(Input.Password, { placeholder: f.placeholder || '', autoComplete: 'new-password' });
    if (f.type === 'textarea') return h(Input.TextArea, { placeholder: f.placeholder || '', rows: f.rows || 3 });
    if (f.type === 'select') {
      return h(Select, {
        placeholder: f.placeholder || '请选择',
        options: (f.options || []).map(function (o) {
          return typeof o === 'string' ? { value: o, label: o } : o;
        }),
        showSearch: !!f.showSearch,
        optionFilterProp: 'label'
      });
    }
    return h(Input, { placeholder: f.placeholder || '' });
  }

  function toRules(f) {
    var rules = (f.rules || []).slice();
    if (f.required) {
      rules.unshift({ required: true, message: f.requiredMsg || ('请输入' + (f.label || '')) });
    }
    return rules;
  }

  function formModal(opts) {
    opts = opts || {};
    var fields = opts.fields || [];
    return new Promise(function (resolve) {
      ready(function (a) {
        if (!a) { resolve(null); return; }
        var host = document.createElement('div');
        document.body.appendChild(host);
        var root = ReactDOM.createRoot(host);
        var closed = false;
        function destroy(result) {
          if (closed) return;
          closed = true;
          root.render(null);
          setTimeout(function () { try { root.unmount(); } catch (e) {} host.remove(); }, 80);
          resolve(result);
        }
        function ModalShell() {
          var form = Form.useForm()[0];
          var submitting = React.useState(false);
          var setSubmitting = submitting[1];
          function handleOk() {
            form.validateFields().then(function (vals) {
              if (typeof opts.validate === 'function') {
                var err = opts.validate(vals);
                if (err) { message(err, 'warning'); return; }
              }
              // select 空值且必填已由 rules 保证；这里兜底把 undefined 转 null
              destroy(vals);
            }).catch(function () { /* 校验失败，表单内已显示红字，弹窗保持打开 */ });
          }
          return h(Cfg, { theme: THEME },
            h(Modal, {
              open: true, title: opts.title || '请输入', width: opts.width || 440,
              okText: opts.okText || '确定', cancelText: opts.cancelText || '取消',
              maskClosable: false, centered: true, destroyOnClose: true,
              confirmLoading: submitting[0],
              onCancel: function () { destroy(null); },
              onOk: function () {
                setSubmitting(true);
                setTimeout(function () { handleOk(); setSubmitting(false); }, 0);
              }
            }, h(Form, { form: form, layout: 'vertical', initialValues: initialValues() },
              fields.map(function (f, i) {
                return h(Form.Item, {
                  key: f.name || i, name: f.name, label: f.label,
                  rules: toRules(f), extra: f.extra, initialValue: undefined
                }, renderField(f));
              })
            ))
          );
        }
        function initialValues() {
          var v = {};
          fields.forEach(function (f) { if (f.initialValue !== undefined) v[f.name] = f.initialValue; });
          return v;
        }
        root.render(h(ModalShell, null));
      });
    });
  }

  window.AntdUI = { message: message, confirm: confirm, formModal: formModal, ready: ready };
})();
