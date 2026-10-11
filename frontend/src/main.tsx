import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import type {Repo, Job, FileItem, Document, SearchResult, Source} from './types';
import {unicodeSlice, safeGithubUrl, progressLabel} from './utils.mjs';
import './style.css';

async function api<T>(path:string, body?:unknown, signal?:AbortSignal):Promise<T> {
  const response=await fetch('/api/v1'+path,{method:body===undefined?'GET':'POST',
    headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal});
  const result=await response.json();
  if(!response.ok || result.status!=='ok')throw new Error(`${result.error?.code ?? response.status}: ${result.error?.message ?? '서버 오류'}`);
  return result.data;
}
function GithubLink({url,children}:{url:string;children:React.ReactNode}) {
  const safe=safeGithubUrl(url);
  return safe?<a href={safe} target="_blank" rel="noopener noreferrer">{children} ↗</a>:<span>{children}</span>;
}
function App() {
  const [repos,setRepos]=useState<Repo[]>([]),[selected,setSelected]=useState<string[]>([]);
  const [health,setHealth]=useState<{ready:boolean;mode:string;device:string;generator_dtype:string|null}|null>(null);
  const [url,setUrl]=useState(''),[ref,setRef]=useState(''),[scope,setScope]=useState('');
  const [error,setError]=useState(''),[registering,setRegistering]=useState(false);
  const [jobs,setJobs]=useState<Record<string,Job>>({});
  const [runId,setRunId]=useState(''),[manifest,setManifest]=useState<Record<string,unknown>|null>(null);
  const [files,setFiles]=useState<FileItem[]>([]),[total,setTotal]=useState(0),[offset,setOffset]=useState(0);
  const [filter,setFilter]=useState(''),[prefix,setPrefix]=useState('');
  const [document,setDocument]=useState<Document|null>(null),[chunkId,setChunkId]=useState('');
  const [question,setQuestion]=useState(''),[topK,setTopK]=useState(5),[result,setResult]=useState<SearchResult|null>(null);
  const [busy,setBusy]=useState(''),[seconds,setSeconds]=useState(0),[loadingDoc,setLoadingDoc]=useState(false);
  const docSequence=useRef(0),dialogRef=useRef<HTMLElement>(null),lastFocus=useRef<HTMLElement|null>(null);
  async function refresh() {
    const data=await api<{items:Repo[]}>('/repos');setRepos(data.items);
    setSelected(previous=>previous.filter(id=>data.items.some(r=>r.repo_id===id && r.searchable)));
    for(const r of data.items)if(r.active_job_id) {
      const job=await api<Job>('/jobs/'+r.active_job_id);setJobs(previous=>({...previous,[job.job_id]:job}));
    }
  }
  useEffect(()=>{
    let alive=true;
    const check=async()=>{try {
      const h=await api<typeof health>('/health');if(!alive)return;setHealth(h);await refresh();setError('');
    } catch(e){if(alive)setError('서버 연결을 확인하세요. 모델 로딩 중에는 시간이 걸릴 수 있습니다. '+String(e));}};
    void check();const timer=setInterval(()=>{if(!health)void check();},5000);
    return()=>{alive=false;clearInterval(timer);};
  },[health?.ready]);
  const pending=Object.values(jobs).filter(j=>j.status==='queued'||j.status==='running').map(j=>j.job_id).join(',');
  useEffect(()=>{
    if(!pending)return;
    let alive=true,inflight=false;
    const poll=async()=>{
      if(inflight)return;inflight=true;
      try {for(const id of pending.split(',')) {
        const j=await api<Job>('/jobs/'+id);if(!alive)return;setJobs(p=>({...p,[id]:j}));
        if(!['queued','running'].includes(j.status)){await refresh();setRunId(j.run_id);}
      }}catch(e){if(alive)setError(String(e));}finally{inflight=false;}
    };
    const timer=setInterval(()=>void poll(),1500);void poll();return()=>{alive=false;clearInterval(timer);};
  },[pending]);
  useEffect(()=>{
    if(!runId)return;
    const controller=new AbortController();setFiles([]);setManifest(null);
    const params=new URLSearchParams({limit:'100',offset:String(offset),path_prefix:prefix});if(filter)params.set('status',filter);
    Promise.all([api<{items:FileItem[];total:number}>(`/runs/${runId}/files?${params}`,undefined,controller.signal),api<Record<string,unknown>>('/runs/'+runId,undefined,controller.signal)])
      .then(([list,m])=>{setFiles(list.items);setTotal(list.total);setManifest(m);})
      .catch(e=>{if(!controller.signal.aborted)setError(String(e));});
    return()=>controller.abort();
  },[runId,offset,filter,prefix]);
  useEffect(()=>{if(!busy)return;setSeconds(0);const timer=setInterval(()=>setSeconds(s=>s+1),1000);return()=>clearInterval(timer);},[busy]);
  useEffect(()=>{
    if(!document)return;
    const keys=(e:KeyboardEvent)=>{
      if(e.key==='Escape')setDocument(null);
      if(e.key==='Tab') {
        const controls=Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button,a[href],select,input,textarea')??[]);
        const first=controls[0],last=controls[controls.length-1],focused=window.document.activeElement;
        if(e.shiftKey&&focused===first){e.preventDefault();last?.focus();}
        else if(!e.shiftKey&&focused===last){e.preventDefault();first?.focus();}
      }
    };
    window.addEventListener('keydown',keys);
    return()=>{window.removeEventListener('keydown',keys);if(lastFocus.current?.isConnected)lastFocus.current.focus();};
  },[document]);
  async function collect(repoId:string) {
    const j=await api<Job>(`/repos/${repoId}/ingestions`,{ref:ref.trim()||null,scope_path:scope.trim()});
    setJobs(p=>({...p,[j.job_id]:{...j,phase:'queued',progress:{unit:null,current:0,total:null,percent:null},error:null}}));await refresh();
  }
  async function register(event:React.FormEvent) {
    event.preventDefault();setRegistering(true);setError('');
    try {const r=await api<Repo>('/repos',{repo_url:url});await refresh();await collect(r.repo_id);setUrl('');}
    catch(e){setError(String(e));}finally{setRegistering(false);}
  }
  async function openDocument(run:string,id:string,chunk='') {
    lastFocus.current=window.document.activeElement as HTMLElement;
    const sequence=++docSequence.current;setLoadingDoc(true);setError('');
    try {const d=await api<Document>(`/documents/${run}/${id}`);if(sequence===docSequence.current){setDocument(d);setChunkId(chunk||d.chunks[0]?.chunk_id||'');}}
    catch(e){setError(String(e));}finally{if(sequence===docSequence.current)setLoadingDoc(false);}
  }
  async function ask(generate:boolean) {
    setBusy(generate?'검색 후 답변 생성 중':'유사도 검색 중');setError('');setResult(null);
    try {setResult(await api<SearchResult>(generate?'/answers':'/search',{question,repo_ids:selected,top_k:topK}));}
    catch(e){setError(String(e));}finally{setBusy('');}
  }
  function source(source:Source) {return <article className="source" key={source.source_id} id={'source-'+source.source_id}>
    <div className="row"><span className="tag">{source.source_id}</span><b>{source.repo}</b><span className="muted">cosine {source.score.toFixed(4)}</span></div>
    <button className="file-link" onClick={()=>void openDocument(source.run_id,source.document_id,source.chunk_id)}>{source.path} · L{source.start_line}–{source.end_line}</button>
    <div className="muted small">{source.run_id} · commit {source.commit_sha?.slice(0,10)} · {result?.context_source_ids?.includes(source.source_id)?'생성 입력에 포함':result?.answer?'검색 결과':'검색 근거'}</div>
    <pre>{source.text}</pre><GithubLink url={source.source_url}>GitHub 원문</GithubLink>
  </article>;}
  const chosen=document?.chunks.find(c=>c.chunk_id===chunkId);
  const before=document&&chosen?unicodeSlice(document.text,0,chosen.start_offset):'';
  const active=document&&chosen?unicodeSlice(document.text,chosen.start_offset,chosen.end_offset):'';
  const after=document&&chosen?unicodeSlice(document.text,chosen.end_offset,Array.from(document.text).length):'';
  return <div className="app">
    <header><div><span className="eyebrow">LOCAL WORKSPACE</span><h1>Git RAG</h1></div><div className="row"><span className={'status '+(health?.ready?'ready':'')}>{health?.ready?`${health.device.toUpperCase()} · ${health.mode==='rag'?'Qwen + E5':'E5 검색 전용'}`:'서버 준비 중'}</span><a href="/docs" target="_blank" rel="noreferrer">API 문서 ↗</a></div></header>
    {error&&<div className="notice error" role="alert">{error}<button onClick={()=>setError('')} aria-label="오류 닫기">×</button></div>}
    <div className="layout"><aside>
      <section className="panel"><h2>저장소 수집</h2><p className="muted">GitHub URL을 등록하고 원문과 벡터를 저장합니다.</p>
        <form onSubmit={e=>void register(e)}><label>Repository URL<input required type="url" placeholder="https://github.com/owner/repo" value={url} onChange={e=>setUrl(e.target.value)}/></label>
          <div className="fields"><label>ref <span className="muted">선택</span><input placeholder="기본 브랜치" value={ref} onChange={e=>setRef(e.target.value)}/></label><label>폴더 범위 <span className="muted">선택</span><input placeholder="전체 저장소" value={scope} onChange={e=>setScope(e.target.value)}/></label></div>
          <button className="primary wide" disabled={registering||!health?.ready}>{registering?'등록 중…':'등록 + 수집 시작'}</button></form>
      </section>
      <section className="panel"><div className="row"><h2>검색할 저장소</h2><button className="quiet" onClick={()=>void refresh().catch(e=>setError(String(e)))}>새로고침</button></div>
        {!repos.length&&<p className="muted">저장소를 등록하거나 기존 output을 import하세요.</p>}
        {repos.map(r=><article className="repo" key={r.repo_id}><label className="check"><input type="checkbox" checked={selected.includes(r.repo_id)} disabled={!r.searchable} onChange={e=>setSelected(p=>e.target.checked?[...p,r.repo_id]:p.filter(id=>id!==r.repo_id))}/><b>{r.repo}</b></label>
          <p className="muted small">{r.searchable?`${r.counts.documents} 문서 · ${r.counts.chunks} 청크 · ${r.counts.vectors} 벡터`:r.unavailable_reason==='no_active_run'?'수집 완료 후 검색 가능':'활성 데이터 확인 필요'}</p>
          <div className="row"><button className="quiet" disabled={!!r.active_job_id} onClick={()=>void collect(r.repo_id).catch(e=>setError(String(e)))}>{r.active_job_id?'수집 중':'다시 수집'}</button><GithubLink url={r.repo_url}>repo</GithubLink></div>
          {r.runs.length>0&&<label className="small">실행 결과<select value={r.runs.some(x=>x.run_id===runId)?runId:''} onChange={e=>{setRunId(e.target.value);setOffset(0);setFilter('');setPrefix('');}}><option value="">run 선택…</option>{r.runs.map(run=><option key={run.run_id} value={run.run_id}>{run.status} · {run.run_id}{run.run_id===r.active_run_id?' · ACTIVE':''}</option>)}</select></label>}
        </article>)}
      </section>
      {Object.values(jobs).length>0&&<section className="panel"><h2>수집 진행도</h2>{Object.values(jobs).map(j=><div className="job" key={j.job_id}><b>{repos.find(r=>r.repo_id===j.repo_id)?.repo??j.repo_id}</b><p>{j.status} · {j.phase}</p><progress max="100" value={j.progress.percent??undefined}/><p className="muted small">{progressLabel(j.progress)}</p>{j.error&&<p className="error-text">{j.error.message}</p>}<button className="quiet" onClick={()=>{setRunId(j.run_id);setOffset(0);}}>run 확인</button></div>)}</section>}
    </aside><main>
      <section className="panel"><div className="row"><h2>저장소에 질문하기</h2><span className="muted small">{selected.length}개 저장소 선택</span></div><label>질문<textarea rows={4} placeholder="이 저장소의 실행 방법과 관련 파일을 알려줘." value={question} onChange={e=>setQuestion(e.target.value)}/></label><div className="row wrap"><span className="muted small">E5 본문 500 token 이하 · 서버에서 최종 검증</span><label className="inline">Top K<select value={topK} onChange={e=>setTopK(Number(e.target.value))}>{[1,3,5,7,10].map(v=><option key={v}>{v}</option>)}</select></label><button disabled={!!busy||!question.trim()||!selected.length} onClick={()=>void ask(false)}>검색</button><button className="primary" disabled={!!busy||!question.trim()||!selected.length||health?.mode!=='rag'} onClick={()=>void ask(true)}>답변 생성</button></div>
        {busy&&<p className="loading" role="status">{busy} · {seconds}초</p>}
        {result&&<div className="results"><div className="muted small">질문 {result.question_tokens} token · {Math.round(result.timings_ms.total)} ms</div>
          {result.answer!==undefined&&<div className="answer"><h3>답변</h3><pre>{result.answer}</pre><div className="row wrap">{result.cited_source_ids?.map(id=><a key={id} href={'#source-'+id}>{id} 근거 보기 ↓</a>)}</div>{result.answer_status==='invalid_citation'&&<p className="error-text">유효하지 않은 인용: {result.invalid_citation_ids?.join(', ')}</p>}{result.generation?.finish_reason==='length'&&<p className="muted">출력 512 token 제한으로 답변이 끝났습니다.</p>}<p className="muted small">생성 답변은 아래 원문과 함께 확인하세요.</p></div>}
          <h3>검색 근거 · {result.sources.length}</h3>{result.sources.map(source)}
        </div>}
      </section>
      <section className="panel"><div className="row"><h2>파일과 청크 탐색</h2>{manifest&&<span className="tag">{String(manifest.status)}</span>}</div>
        {!runId?<p className="empty">왼쪽 저장소에서 실행 결과를 선택하세요.</p>:<>
          <p className="muted small">{String(manifest?.repo??'')} · {runId}</p>
          <div className="row wrap"><label className="inline">상태<select value={filter} onChange={e=>{setFilter(e.target.value);setOffset(0);}}><option value="">전체</option><option value="collected">수집</option><option value="excluded">제외</option><option value="failed">실패</option></select></label><label className="inline">폴더<input placeholder="예: src" value={prefix} onChange={e=>{setPrefix(e.target.value);setOffset(0);}}/></label><span className="muted small">{total}개 파일</span></div>
          <div className="table-scroll"><table><thead><tr><th>파일</th><th>상태 / 사유</th><th>청크 / 벡터</th></tr></thead><tbody>{files.map(f=><tr key={f.path}><td>{f.document_id?<button className="file-link" onClick={()=>void openDocument(runId,f.document_id!)}>{f.path}</button>:f.path}</td><td><span className={'tag '+f.status}>{f.status}</span>{f.reason_code&&<small title={f.reason??''}>{f.reason_code}</small>}</td><td>{f.chunk_count} / {f.embedded_chunk_count}</td></tr>)}</tbody></table></div>
          <div className="row pagination"><button disabled={offset===0} onClick={()=>setOffset(p=>Math.max(0,p-100))}>이전</button><span>{total?offset+1:0}–{Math.min(offset+100,total)} / {total}</span><button disabled={offset+100>=total} onClick={()=>setOffset(p=>p+100)}>다음</button></div>
          {manifest&&<details><summary>Manifest · 모델·필터·오류·실행 상태</summary><pre>{JSON.stringify(manifest,null,2)}</pre></details>}
        </>}
      </section>
    </main></div>
    {loadingDoc&&<div className="toast" role="status">원문 불러오는 중…</div>}
    {document&&<div className="overlay" onClick={()=>setDocument(null)}><section ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-labelledby="document-title" onClick={e=>e.stopPropagation()}>
      <div className="row"><div><span className="eyebrow">{document.repo} · {document.commit_sha.slice(0,10)}</span><h2 id="document-title">{document.path}</h2></div><button autoFocus onClick={()=>setDocument(null)} aria-label="원문 닫기">닫기 ×</button></div>
      <div className="row wrap"><GithubLink url={document.source_url}>고정 커밋 원문</GithubLink><label className="inline">청크<select value={chunkId} onChange={e=>setChunkId(e.target.value)}><option value="">전체 원문</option>{document.chunks.map(c=><option key={c.chunk_id} value={c.chunk_id}>#{c.chunk_index} · L{c.start_line}–{c.end_line} · {c.token_count} tokens · {c.embedding_status}</option>)}</select></label></div>
      {chosen&&<p className="muted small">Python 문자 offset [{chosen.start_offset}, {chosen.end_offset}) · chunk_id {chosen.chunk_id}</p>}
      <pre className="document-text">{chosen?<>{before}<mark>{active}</mark>{after}</>:document.text||'(빈 파일)'}</pre>
    </section></div>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
