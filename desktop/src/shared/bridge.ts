export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type Reply<T> = { ok: true; data: T } | { ok: false; error: { code: string; message: string } };
export type Page = { afterId?: string; limit?: number };
export type ExecutionResource = 'command' | 'code' | 'stdin' | 'stdout' | 'stderr' | 'metadata' | 'input_manifest' | 'output_manifest';
export interface BinaryChunk {
  base64: string;
  objectRef: string;
  offset: number;
  nextOffset: number | null;
  totalBytes: number;
}
export interface ConnectionState {
  baseUrl: string;
  tokenConfigured: boolean;
  repositoryId: string | null;
  api: 'unknown' | 'reachable' | 'unreachable';
  database: 'unknown' | 'ready' | 'unavailable';
  identity: 'missing' | 'accepted' | 'rejected' | 'unknown';
  compatibility: 'unknown' | 'compatible' | 'incompatible';
  worker: 'unknown';
  sandbox: 'unknown';
  backendVersion: string | null;
  message: string;
}
export interface Inputs {
  getConnection: undefined;
  configureConnection: { baseUrl: string };
  selectRepository: { repositoryId: string };
  capabilities: undefined;
  createRepository: { name: string; key: string };
  listRuns: Page;
  getRun: { runId: string };
  getJob: { jobId: string };
  getEvents: { runId: string; afterSeq?: number; limit?: number };
  listBranches: { afterName?: string; limit?: number };
  listCommits: { branchName?: string; afterCommit?: string; limit?: number };
  getCommit: { commitId: string };
  getArtifacts: { runId: string };
  getCommitArtifacts: Page & { commitId: string };
  getApprovals: { runId: string; afterSeq?: number; limit?: number };
  listExecutions: Page & { runId: string };
  getExecution: { runId: string; callId: string };
  getExecutionContent: { runId: string; callId: string; resource: ExecutionResource; offset?: number; limit?: number };
  readArtifact: { artifactId: string; offset?: number; limit?: number };
  exportArtifact: { artifactId: string };
  createAgent: { config: JsonObject; expectedVersion: number; key: string };
  createTask: { configId: string; configVersion: number; goal: JsonObject; key: string };
  submitRun: { taskId: string; configVersion: number; expectedBranchVersion: number; expectedHead: string | null; branchName: string; budget?: JsonObject; key: string };
  createBranch: { sourceRunId: string; fromCommit: string; branchName: string; key: string };
  replay: { request: JsonObject; key: string };
  getReplay: { jobId: string };
  getDiff: { base: string; head: string; after?: number; limit?: number };
  controlRun: { runId: string; action: 'pause' | 'resume' | 'cancel'; expectedVersion: number; key: string };
  decideApproval: { approvalId: string; decision: 'approve' | 'reject'; expectedVersion: number; key: string };
}
export type Operation = keyof Inputs;
export type Output<K extends Operation> = K extends 'getConnection' | 'configureConnection' | 'selectRepository'
  ? ConnectionState : K extends 'getExecutionContent' | 'readArtifact' ? BinaryChunk : JsonObject;
export type Method<K extends Operation> = Inputs[K] extends undefined
  ? () => Promise<Reply<Output<K>>> : (input: Inputs[K]) => Promise<Reply<Output<K>>>;
export type DesktopApi = { [K in Operation]: Method<K> } & { cancelReads: () => void };
