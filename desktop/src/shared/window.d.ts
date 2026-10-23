import type { DesktopApi } from './bridge';

declare global {
  const __AGENTGIT_DESKTOP_VERSION__: string;
  interface Window { agentgit: DesktopApi }
}
