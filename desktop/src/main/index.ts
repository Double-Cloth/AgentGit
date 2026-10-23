import { app, BrowserWindow, dialog, ipcMain, protocol, screen, session } from 'electron';
import { readFile, realpath, stat, writeFile } from 'node:fs/promises';
import { extname, isAbsolute, join, relative, resolve } from 'node:path';
import { DesktopError } from './api-client';
import { BusinessController } from './routes';
import { APP_DOCUMENT, CSP, assetPath, assertSender, trustedDocument } from './security';
import { PreferenceStore } from './preferences';
import type { Reply } from '../shared/bridge';

const squirrelStartup: boolean = require('electron-squirrel-startup');
const apiToken = process.env.AGENTGIT_API_TOKEN;
// 在创建渲染子进程前移除环境副本；凭据只保留在主进程客户端闭包。
delete process.env.AGENTGIT_API_TOKEN;
// 供应商凭据只供独立 Worker 使用，桌面与渲染子进程不需要继承。
delete process.env.OPENAI_API_KEY;
if (process.env.AGENTGIT_DESKTOP_PROFILE_DIR) app.setPath('userData', resolve(process.env.AGENTGIT_DESKTOP_PROFILE_DIR));
app.enableSandbox();
protocol.registerSchemesAsPrivileged([{ scheme: 'agentgit', privileges: { standard: true, secure: true, supportFetchAPI: false, corsEnabled: false } }]);
const haveLock = !squirrelStartup && app.requestSingleInstanceLock();
let window: BrowserWindow | null = null;
let controller: BusinessController | null = null;

if (!haveLock) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => controller?.cancelAllReads());
  void app.whenReady().then(start).catch(() => {
    dialog.showErrorBox('AgentGit 无法启动', '桌面初始化失败。请检查包内资源与用户数据目录权限；后端服务不会被停止。');
    app.quit();
  });
}

async function start(): Promise<void> {
  const store = new PreferenceStore(app.getPath('userData'));
  const preferences = await store.load(process.env.AGENTGIT_API_URL);
  const root = await realpath(join(__dirname, '../renderer'));
  const mime: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf' };
  protocol.handle('agentgit', async request => {
    try {
      if (request.method !== 'GET') return new Response(null, { status: 405 });
      const path = await realpath(assetPath(root, request.url));
      const inside = relative(root, path);
      if (inside.startsWith('..') || isAbsolute(inside)) return new Response(null, { status: 404 });
      const info = await stat(path);
      if (!info.isFile() || info.size > 16777216) return new Response(null, { status: 413 });
      const bytes = await readFile(path);
      if (bytes.length > 16777216) return new Response(null, { status: 413 });
      return new Response(new Uint8Array(bytes), { headers: { 'Content-Type': mime[extname(path)]!,
        'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' } });
    } catch { return new Response(null, { status: 404 }); }
  });
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.on('will-download', event => event.preventDefault());
  session.defaultSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    try { const url = new URL(details.url); callback({ cancel: url.protocol !== 'agentgit:' || url.host !== 'app' }); }
    catch { callback({ cancel: true }); }
  });
  controller = new BusinessController(preferences, apiToken,
    value => store.update(value), async (name, bytes) => {
      if (!window || window.isDestroyed()) throw new DesktopError('window_unavailable', '窗口已关闭，请重新打开后保存');
      const chosen = await dialog.showSaveDialog(window, { title: '保存所选版本的产物', defaultPath: name, properties: ['showOverwriteConfirmation'] });
      if (chosen.canceled || !chosen.filePath) return { cancelled: true };
      if (bytes.length > 8388608) throw new DesktopError('output_limit', '产物超过导出上限');
      try { await writeFile(chosen.filePath, bytes, { mode: 0o600 }); }
      catch { throw new DesktopError('save_failed', '无法写入所选文件，请检查位置与权限'); }
      return { cancelled: false, saved: true };
    });
  const bounds = { ...preferences.bounds };
  if (bounds.x !== undefined && bounds.y !== undefined && !screen.getAllDisplays().some(display => {
    const area = display.workArea;
    return bounds.x! + bounds.width > area.x && bounds.x! < area.x + area.width
      && bounds.y! + bounds.height > area.y && bounds.y! < area.y + area.height;
  })) { delete bounds.x; delete bounds.y; }
  const current = new BrowserWindow({ ...bounds, minWidth: 768, minHeight: 600, useContentSize: true,
    title: 'AgentGit', backgroundColor: '#faf9f5', show: false, autoHideMenuBar: true,
    webPreferences: { preload: join(__dirname, '../preload/index.js'), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false, allowRunningInsecureContent: false,
      spellcheck: false, devTools: !app.isPackaged } });
  window = current;
  current.removeMenu();
  const validate = (event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent) => {
    if (current.isDestroyed()) throw new DesktopError('forbidden', '应用窗口已关闭');
    const frame = event.senderFrame;
    assertSender({ webContentsId: event.sender.id, mainFrame: Boolean(frame && frame === event.sender.mainFrame), url: frame?.url ?? '' }, current.webContents.id);
  };
  ipcMain.handle('agentgit:business', async (event, operation: unknown, input: unknown): Promise<Reply<unknown>> => {
    try { validate(event); return { ok: true, data: await controller!.handle(operation, input) }; }
    catch (error) { return { ok: false, error: error instanceof DesktopError
      ? { code: error.code, message: error.message } : { code: 'desktop_error', message: '桌面操作失败，请重新查询关联任务状态' } }; }
  });
  ipcMain.on('agentgit:cancel-reads', event => { try { validate(event); controller?.cancelReads(); } catch { /* 未授权来源不获得业务能力。 */ } });
  current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  current.webContents.on('will-navigate', event => { if (!trustedDocument(event.url)) event.preventDefault(); });
  current.webContents.on('will-frame-navigate', event => { if (!event.isMainFrame || !trustedDocument(event.url)) event.preventDefault(); });
  current.webContents.on('will-attach-webview', event => event.preventDefault());
  current.webContents.on('render-process-gone', () => {
    controller?.cancelAllReads();
    if (current.isDestroyed()) return;
    void dialog.showMessageBox(current, { type: 'error', title: '界面已中断', message: '重新打开界面后会查询现有任务。已提交的后端任务继续运行。',
      buttons: ['重新打开', '关闭窗口'], defaultId: 0, cancelId: 1 }).then(({ response }) => {
      if (!current.isDestroyed()) { if (response === 0) void current.loadURL(APP_DOCUMENT); else current.close(); }
    });
  });
  current.on('close', () => {
    if (!current.isMaximized() && !current.isMinimized()) void store.update({ bounds: current.getContentBounds() }).catch(() => {});
    controller?.cancelAllReads();
  });
  current.on('closed', () => { window = null; });
  current.once('ready-to-show', () => current.show());
  await current.loadURL(APP_DOCUMENT);
}
