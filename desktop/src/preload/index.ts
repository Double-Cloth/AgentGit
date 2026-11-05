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
  bindA2A: method('bindA2A'), submitA2A: method('submitA2A'), listA2A: method('listA2A'), getA2ASubmission: method('getA2ASubmission'), getA2AProgram: method('getA2AProgram'),
  listCollaborations: method('listCollaborations'), getCollaboration: method('getCollaboration'), prepareCollaboration: method('prepareCollaboration'),
  startCollaboration: method('startCollaboration'), recordCollaboration: method('recordCollaboration'), controlCollaboration: method('controlCollaboration'),
  getCollaborationMessages: method('getCollaborationMessages'), getCollaborationShared: method('getCollaborationShared'),
  listMergeProposals: method('listMergeProposals'), getMergeBases: method('getMergeBases'), createMergeProposal: method('createMergeProposal'),
  getMergeProposal: method('getMergeProposal'), getMergeReview: method('getMergeReview'), replaceMergeRevision: method('replaceMergeRevision'),
  validateMergeRevision: method('validateMergeRevision'), beginMergeReview: method('beginMergeReview'), approveMergeProposal: method('approveMergeProposal'),
  rejectMergeProposal: method('rejectMergeProposal'), forkMergedBranch: method('forkMergedBranch'), submitMergedRun: method('submitMergedRun'),
  getConnection: method('getConnection'), configureConnection: method('configureConnection'), selectRepository: method('selectRepository'),
  capabilities: method('capabilities'), getRepository: method('getRepository'), createRepository: method('createRepository'), listRuns: method('listRuns'),
  listRepositories: method('listRepositories'),
  getRun: method('getRun'), getJob: method('getJob'), getEvents: method('getEvents'), listBranches: method('listBranches'),
  listCommits: method('listCommits'), getCommit: method('getCommit'), getArtifacts: method('getArtifacts'),
  getCommitArtifacts: method('getCommitArtifacts'), getApprovals: method('getApprovals'), listExecutions: method('listExecutions'),
  getExecution: method('getExecution'), getExecutionContent: method('getExecutionContent'), readArtifact: method('readArtifact'),
  exportArtifact: method('exportArtifact'), createAgent: method('createAgent'), createTask: method('createTask'),
  submitRun: method('submitRun'), createBranch: method('createBranch'), replay: method('replay'), getReplay: method('getReplay'),
  freezeValidator: method('freezeValidator'), getValidator: method('getValidator'),
  createExperiment: method('createExperiment'), getExperiment: method('getExperiment'),
  listExperiments: method('listExperiments'),
  getInterventionContext: method('getInterventionContext'), createIntervention: method('createIntervention'), getIntervention: method('getIntervention'),
  getDiff: method('getDiff'), controlRun: method('controlRun'), decideApproval: method('decideApproval'),
  getBehavioralDiff: method('getBehavioralDiff'), listDebugReports: method('listDebugReports'),
  createDebugReport: method('createDebugReport'), getDebugReport: method('getDebugReport'),
  getDebugCandidateContext: method('getDebugCandidateContext'), getDebugCandidateExperiment: method('getDebugCandidateExperiment'),
  createPreparedRepairSearch: method('createPreparedRepairSearch'), getRepairSearch: method('getRepairSearch'),
  getRepairWorkflow: method('getRepairWorkflow'),
  listRepairSearches: method('listRepairSearches'), cancelRepairSearch: method('cancelRepairSearch'),
  createRepairStudy: method('createRepairStudy'), getRepairStudy: method('getRepairStudy'), cancelRepairStudy: method('cancelRepairStudy'),
  getRepairValidationSuite: method('getRepairValidationSuite'), openRepairValidation: method('openRepairValidation'),
  getRepairValidation: method('getRepairValidation'), createRepairProposal: method('createRepairProposal'),
  getRepairProposal: method('getRepairProposal'), beginRepairReview: method('beginRepairReview'), decideRepairReview: method('decideRepairReview'),
  cancelReads: () => ipcRenderer.send('agentgit:cancel-reads'),
};
contextBridge.exposeInMainWorld('agentgit', Object.freeze(api));
