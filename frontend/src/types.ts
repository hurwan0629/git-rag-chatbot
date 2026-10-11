export type Run = {run_id:string;status:string;commit_sha:string|null;counts:Record<string,number>;created_at:string};
export type Repo = {repo_id:string;repo:string;repo_url:string;active_run_id:string|null;searchable:boolean;counts:Record<string,number>;unavailable_reason:string|null;active_job_id:string|null;runs:Run[]};
export type Job = {job_id:string;run_id:string;repo_id:string;status:string;phase:string;progress:{unit:string|null;current:number;total:number|null;percent:number|null};error:{message:string}|null};
export type FileItem = {path:string;status:string;reason:string|null;reason_code:string|null;document_id:string|null;chunk_count:number;embedded_chunk_count:number};
export type Chunk = {chunk_id:string;chunk_index:number;start_offset:number;end_offset:number;start_line:number;end_line:number;token_count:number;embedding_status:string};
export type Document = {run_id:string;document_id:string;repo:string;commit_sha:string;path:string;text:string;source_url:string;chunks:Chunk[]};
export type Source = Chunk & {source_id:string;run_id:string;commit_sha:string;document_id:string;repo:string;path:string;text:string;source_url:string;score:number};
export type SearchResult = {question:string;question_tokens:number;sources:Source[];timings_ms:Record<string,number>;answer?:string;answer_status?:string;context_source_ids?:string[];cited_source_ids?:string[];invalid_citation_ids?:string[];generation?:{finish_reason:string;input_tokens:number;output_tokens:number}|null};
