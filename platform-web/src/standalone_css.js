// Styles only the standalone hosts use (the Mana build never includes this file). The build
// writes it, with the shared CSS and GUIDE_CSS, into platform.css, so the page needs no inline
// <style> element under the server's Content-Security-Policy.
const STANDALONE_CSS = `
@font-face{font-family:Manrope;font-style:normal;font-weight:200 800;font-display:swap;src:url('fonts/Manrope-latin.woff2') format('woff2')}
html,body{margin:0;padding:0;background:#fbf9f5}
@media (prefers-color-scheme: dark){html,body{background:#151311}}
#root{width:100%;min-height:100vh}
#root>.eh{min-height:100vh}
.w-full{width:100%}
.eh-host-tools{display:flex;align-items:center;gap:6px;margin-left:8px}
.eh-viewer{font-size:13px;font-weight:600;color:var(--muted);padding:0 4px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.eh-theme-toggle{min-width:56px}
.eh-run-steps ol.eh-steps-list{margin:12px 0 14px;padding-left:22px;display:flex;flex-direction:column;gap:14px}
.eh-run-steps ol.eh-steps-list li{padding-left:4px;line-height:1.55}
.eh-cmd{margin:8px 0 0;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;white-space:pre-wrap;word-break:break-all;user-select:all}
.eh-upload .field input[type=file]{padding:8px 10px;background:var(--surface)}
.eh-signin{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 16px;box-sizing:border-box}
.eh-signin-card{width:min(400px,100%);background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:28px 28px 26px;box-shadow:0 18px 50px var(--shadow);box-sizing:border-box}
.eh-signin-card .eh-brand{margin-bottom:22px;cursor:default}
.eh-signin-card h1{margin:0 0 4px;font-size:24px;font-weight:800;letter-spacing:-0.01em}
.eh-signin-card p{margin:0;color:var(--muted);font-size:14px}
.eh-signin-card .field input{font-size:15px}
.eh-signin-card .btn{width:100%;margin-top:22px}
.eh-signin-error{margin:14px 0 0;padding:10px 12px;border-radius:8px;background:var(--bad-bg,#fbeaea);color:var(--bad-ink,#9b1c1c);font-size:13.5px}
@container (max-width: 560px){.eh-host-tools{margin-left:auto}.eh-viewer{display:none}}
`;
