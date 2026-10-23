import { extname, isAbsolute, relative, resolve } from 'node:path';
import { DesktopError } from './api-client';

export const APP_DOCUMENT = 'agentgit://app/index.html';
export const CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

export interface SenderInfo { webContentsId: number; mainFrame: boolean; url: string }
export function trustedDocument(url: string, expected = APP_DOCUMENT): boolean {
  try {
    const value = new URL(url), trusted = new URL(expected);
    value.hash = ''; trusted.hash = '';
    return value.href === trusted.href && !value.username && !value.password && !value.search;
  } catch { return false; }
}
export function assertSender(sender: SenderInfo, expectedId: number, document = APP_DOCUMENT): void {
  if (sender.webContentsId !== expectedId || !sender.mainFrame || !trustedDocument(sender.url, document)) {
    throw new DesktopError('forbidden', '仅当前应用受信任窗口的顶层文档可以调用业务接口');
  }
}
export function assetPath(root: string, rawUrl: string): string {
  let url: URL, path: string;
  try { url = new URL(rawUrl); path = decodeURIComponent(url.pathname); }
  catch { throw new DesktopError('not_found', '资源不可访问'); }
  const parts = path.replace(/^\//, '').split('/');
  if (url.protocol !== 'agentgit:' || url.host !== 'app' || url.username || url.password || url.search || url.hash
      || parts.some(part => !/^[a-zA-Z0-9._-]+$/.test(part) || ['.', '..'].includes(part))
      || (path !== '/index.html' && !path.startsWith('/assets/'))
      || !['.html', '.js', '.css', '.svg', '.png', '.woff', '.woff2', '.ttf'].includes(extname(path))) {
    throw new DesktopError('not_found', '资源不可访问');
  }
  const full = resolve(root, ...parts), inside = relative(root, full);
  if (inside.startsWith('..') || isAbsolute(inside)) throw new DesktopError('not_found', '资源不可访问');
  return full;
}
