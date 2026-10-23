import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopApi, Operation } from '../shared/bridge';

// sandbox preload 不加载业务模块；仅为列出的固定操作建立桥接。
function method(operation: Operation) {
  return async (input?: unknown) => {
    try { return await ipcRenderer.invoke('agentgit:business', operation, input); }
    catch { return { ok: false, error: { code: 'bridge_unavailable', message: '桌面连接已中断，请重新打开窗口并查询任务' } }; }
  };
}
const api: DesktopApi = {
  getConnection: method('getConnection'), configureConnection: method('configureConnection'), selectRepository: method('selectRepository'),
  capabilities: method('capabilities'), getRepository: method('getRepository'), createRepository: method('createRepository'), listRuns: method('listRuns'),
  getRun: method('getRun'), getJob: method('getJob'), getEvents: method('getEvents'), listBranches: method('listBranches'),
  listCommits: method('listCommits'), getCommit: method('getCommit'), getArtifacts: method('getArtifacts'),
  getCommitArtifacts: method('getCommitArtifacts'), getApprovals: method('getApprovals'), listExecutions: method('listExecutions'),
  getExecution: method('getExecution'), getExecutionContent: method('getExecutionContent'), readArtifact: method('readArtifact'),
  exportArtifact: method('exportArtifact'), createAgent: method('createAgent'), createTask: method('createTask'),
  submitRun: method('submitRun'), createBranch: method('createBranch'), replay: method('replay'), getReplay: method('getReplay'),
  getDiff: method('getDiff'), controlRun: method('controlRun'), decideApproval: method('decideApproval'),
  cancelReads: () => ipcRenderer.send('agentgit:cancel-reads'),
};
contextBridge.exposeInMainWorld('agentgit', Object.freeze(api));
