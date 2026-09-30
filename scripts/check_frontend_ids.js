// 校验 app.js 引用的所有 $('id') 是否都在 index.html 中定义
const fs = require('fs');
const js = fs.readFileSync('D:/enterprise-rag-platform/frontend/app.js', 'utf8');
const html = fs.readFileSync('D:/enterprise-rag-platform/frontend/index.html', 'utf8');
const htmlIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const jsIds = [...js.matchAll(/\$\('([a-zA-Z][\w-]*)'\)/g)].map(m => m[1]);
const missing = [...new Set(jsIds)].filter(id => !htmlIds.has(id));
console.log('HTML 定义的 ID 数:', htmlIds.size);
console.log('JS 引用的 ID 数:', new Set(jsIds).size);
console.log(missing.length ? 'JS 引用但 HTML 缺失的 ID:\n' + missing.join('\n') : 'OK: 所有 JS 引用的 ID 都存在于 HTML');
// 动态创建的 ID 白名单（innerHTML 注入的）
