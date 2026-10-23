import type { DesktopApi } from './bridge';

declare global { interface Window { agentgit: DesktopApi } }
