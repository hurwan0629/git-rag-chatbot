// 실제 HTML 스크립트를 작은 DOM에서 실행해 사용자 입력/출처/Unicode 연결을 검증한다.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const html=fs.readFileSync(new URL('./web/index.html',import.meta.url),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.events={};this._text='';this.disabled=false;this.value='';this.checked=false;}
 set textContent(text){this._text=String(text);this.children=[];}
 get textContent(){return this._text+this.children.map(e=>e.textContent).join('');}
 append(...elements){this.children.push(...elements);}
 replaceChildren(...elements){this._text='';this.children=[...elements];}
 addEventListener(type,callback){this.events[type]=callback;}
 querySelectorAll(selector){const found=[];const tag=selector.split(':')[0];for(const child of this.children){if(child.tag===tag&&(!selector.includes(':checked')||child.checked))found.push(child);found.push(...child.querySelectorAll(selector));}return found;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 showModal(){this.open=true;}
 close(){this.open=false;}
 scrollIntoView(){}
}
async function setup(mode='rag'){
 const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element('div')]));
 nodes.get('top-k').value='5';
 const calls=[],source={source_id:'S1',repo:'alice/demo',run_id:'r1',document_id:'d1',chunk_id:'c1',commit_sha:'a'.repeat(40),path:'src/main.py',start_line:1,end_line:1,start_offset:1,end_offset:2,text:'😀',score:.75,vector_row:0,source_url:'https://github.com/alice/demo/blob/main/src/main.py'};
 const response={status:'ok',question_tokens:5,results:[source],timings_ms:{query_encoding:1,search:2,context:3,generation:4,total:10},answer:'설명 [S1] 묶음 인용 [S1, S99]',answer_status:'invalid_citation',context_source_ids:['S1'],cited_source_ids:['S1'],invalid_citation_ids:['S99'],generation:{input_tokens:20,output_tokens:5,finish_reason:'eos',model_id:'mock',revision:'b'.repeat(40)}};
 const catalog={mode,session_id:'s1',repos:[{repo:'alice/demo',run_id:'r1',commit_sha:'a'.repeat(40),documents:1,chunks:1,searchable:true,reason:null}],skipped_runs:[]};
 const context=vm.createContext({console,URL,URLSearchParams,Date,Map,Set,Error,Number,Array,
  document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),createTextNode:text=>{const e=new Element('#text');e.textContent=text;return e;}},
  history:{replaceState(){}},setInterval:()=>1,clearInterval(){},
  fetch:async(path,options)=>{calls.push({path,options});let data=path==='/api/repos'?catalog:path.startsWith('/api/document')?{...source,text:'A😀\r\nB'}:response;return {ok:true,json:async()=>structuredClone(data)};}
 });
 vm.runInContext(script,context);
 await new Promise(setImmediate);
 return {context,nodes,calls,source};
}
test('repository selection is required and search-only disables generation',async()=>{
 const {context,nodes,calls}=await setup('search_only');
 assert.equal(nodes.get('answer').disabled,true);
 nodes.get('question').value='질문';
 await vm.runInContext("run('/api/search')",context);
 assert.match(nodes.get('status').textContent,/저장소/);
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0);
});
test('question is sent raw and source/citation links keep their IDs',async()=>{
 const {context,nodes,calls}=await setup();
 nodes.get('repos').querySelector('input').checked=true;
 nodes.get('question').value='<img src=x onerror=alert(1)> 질문';
 await vm.runInContext("run('/api/answer')",context);
 const payload=JSON.parse(calls.find(c=>c.options?.method==='POST').options.body);
 assert.equal(payload.question,'<img src=x onerror=alert(1)> 질문');
 assert.equal(payload.question.startsWith('query: '),false);
 assert.equal(nodes.get('answer-text').querySelectorAll('a').length,2);
 assert.match(nodes.get('answer-text').querySelector('a').href,/chunk=c1/);
 assert.match(nodes.get('citation-warning').textContent,/S99/);
 assert.equal(nodes.get('search').disabled,false);
 assert.match(nodes.get('metrics').textContent,/전체 10 ms/);
});
test('Python character offsets highlight an emoji without splitting its surrogate pair',async()=>{
 const {context,nodes,source}=await setup();
 context.selectedSource=source;
 await vm.runInContext('openDocument(selectedSource)',context);
 assert.equal(nodes.get('document-dialog').open,true);
 assert.equal(nodes.get('document-source').querySelector('mark').textContent,'😀');
 assert.equal(nodes.get('document-source').textContent,'A😀\r\nB');
 assert.equal(nodes.get('chunk-source').textContent,'😀');
});
test('external links only accept GitHub HTTPS URLs and plain text stays plain text',async()=>{
 const {context}=await setup();
 assert.equal(vm.runInContext("external('x','javascript:alert(1)').href",context),undefined);
 assert.equal(vm.runInContext("external('x','https://example.com/').href",context),undefined);
 assert.equal(vm.runInContext("node('pre','<script>alert(1)</script>').children.length",context),0);
 assert.doesNotMatch(script,/\.innerHTML\s*=/);
});
